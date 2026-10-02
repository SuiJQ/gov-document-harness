"""docx 读取器。**零第三方依赖**（D-032）：`zipfile` + `xml.etree` + `zlib`。

## 这是整个项目安全要求最硬的一个文件

`.docx` 是 zip。`zipfile.read(entry)` 会把整个 entry 解压进内存，
而 zip bomb（磁盘几百 KB、解压几百 MB）会直接 OOM。

**四道防线**（D-131，逐条都有测试）：

1. **条目数上限** `MAX_ENTRIES` —— D-022 三重要求之一，
   架构 §6.3 原先漏了，20 万条目的包三项旧检查全部通过（D-131 缺口一）。
2. **元数据筛子**：单 entry / 总量 / 压缩比（D-078）。
   **它只是筛子，不是边界**——见下面第 3 条。
3. **流式限量解压**（★真正的边界）：`ZipExtFile` 的 `.read(n)`
   每次最多解出 n 字节，**累计计数超限立即中止**。
   元数据可伪造（D-131 缺口二：改 central directory 报 100 字节，
   真解出 10MB），所以**不能只信 `file_size` 就 `read()` 一次**——
   那正是 D-131 实测到 209MB 内存峰值的原因。
4. **不依赖 CRC 兜底**：`zipfile` 的 CRC 校验发生在整个 entry
   解压**完之后**，太晚。

**为什么必须有第 3 条而不能只有第 2 条**：
第 2 条比的是元数据，而元数据在 central directory 里、**可被篡改**。
第 3 条比的是**真实解出的字节数**，篡改元数据绕不过它。
**内存占用与真实体积解耦**才是真边界。
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import IO, Protocol
from xml.etree import ElementTree

from ....domain.errors import ErrorCode, Result
from ....domain.task import 单文件上限, 读到的文件

#: Word 正文所在的 entry 名。
_正文ENTRY = "word/document.xml"

#: 流式读取的单块上限。**不是安全上限，是节奏**——
#: 真正的上限是 `MAX_ENTRY`，这个只决定一次解多少。
_读块大小: int = 64 * 1024

#: XML 里所有命名空间（WordprocessingML 的 `w:` 前缀）。
#: 用 `iter()` 抓 `w:t` 能同时覆盖正文段落**与表格单元格**——
#: 公文大量用表格（发文机关、抄送），只读段落会漏掉整张表。
#: **漏读比读错更危险**：用户不会知道自己少看了内容。
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_文本标签 = f"{_W_NS}t"


#: 各项上限。**导出为模块级常量供测试引用**——
#: 测试里写死 `20*1024*1024` 而实现改了某个数，测试不会红，
#: 那这条判据就形同虚设。
#: 单 entry 解压后上限（D-078）。
MAX_ENTRY: int = 20 * 1024 * 1024
#: 全包解压后总量上限（D-078）。
MAX_TOTAL: int = 100 * 1024 * 1024
#: 压缩比上限，防 zip bomb（D-078）。
MAX_RATIO: int = 100
#: 条目数上限（D-131 缺口一 / D-022 第三重）。
MAX_ENTRIES: int = 2000

#: 单文件读取上限（D-053）。**从 domain 取**，与 txt/md 同一来源——
#: 第 7 步复查实测过这个漏洞：原先只有 txt/md 截断，docx 不截断，
#: 同一个「单文件 2 万字」变成两种行为。zip bomb 防护（20MB）
#: 拦的是**解压字节**，不是**字数**——20MB 的 XML 解析出十几万字很平常。
MAX_CHARS: int = 单文件上限


class _像zip者(Protocol):
    """`流式限量解压` 只需要 `open(name)` —— 用 Protocol 而非 `zipfile.ZipFile`
    是为了让**假 zip 对象在类型检查层面合法**（D-131 缺口二要测的正是
    「元数据全过但真解出巨量」这个分支，真实文件构造不出）。

    返回类型照抄 `ZipFile.open` 的真实签名 `IO[bytes]`——
    自造一个更窄的流类型反而会把真 `ZipFile` 挡在门外
    （协议返回类型协变，标窄了真调用方就不合格）。

    只声明实际用到的方法，不多写——多写一个没用的方法，
    假对象就得多实现一个，白增耦合。
    """

    def open(self, name: str) -> IO[bytes]: ...


class 限额超出(Exception):
    """**我们自己抛的限额异常**，不复用 `zipfile.BadZipFile`。

    复用标准库异常做内部控制流会丢语义：`BadZipFile` 既可能是
    「用户给的文件根本不是 zip」，也可能是「我们自己判定它超限」，
    两者要报的 `ErrorCode` 完全不同（`DOCX_MALFORMED` vs
    `DOCX_BOMB_SUSPECTED`）。混在一起用户看到的是
    「您的文件损坏」——而文件其实没坏，只是太大。

    **这是 D-107 的同型错误：错误码不是标签，是给用户的判断依据。**
    """


def read_docx(路径: Path) -> Result[读到的文件]:
    """读一个 docx 的正文纯文本。

    **任何畸形输入都返回 fail，绝不抛异常穿透**（D-108）。
    """
    if not 路径.is_file():
        return Result.fail(ErrorCode.FILE_UNREADABLE, f"文件不存在或不是文件：{路径.name}")
    try:
        with zipfile.ZipFile(路径) as zf:
            元数据检查(zf)
            if _正文ENTRY not in zf.namelist():
                return Result.fail(
                    ErrorCode.DOCX_MALFORMED,
                    f"docx 缺少正文 {_正文ENTRY}，不是有效的 Word 文档",
                )
            拿到 = 流式限量解压(zf, _正文ENTRY)
    except 限额超出 as exc:
        return Result.fail(ErrorCode.DOCX_BOMB_SUSPECTED, str(exc))
    except zipfile.BadZipFile:
        return Result.fail(ErrorCode.DOCX_MALFORMED, "不是合法的 zip/docx 文件")
    except zipfile.LargeZipFile:
        return Result.fail(ErrorCode.DOCX_BOMB_SUSPECTED, "zip 结构超限")
    except OSError as exc:
        return Result.fail(ErrorCode.FILE_UNREADABLE, f"读取失败：{type(exc).__name__}")
    if 拿到.is_error:
        错 = 拿到.error
        if 错 is None:  # 同上：不在这里压类型错误
            return Result.fail(ErrorCode.DOCX_MALFORMED, "docx 读取失败")
        return Result.fail(错.code, 错.detail)

    转换 = _正文转文本(拿到.unwrap())
    if 转换.is_error:
        错 = 转换.error
        # **不用 `# type: ignore` 压过去**：`error` 在 `is_error` 分支
        # 静态类型上仍是 `None`，直接取 `.code` 会在运行期崩。
        # 压类型错误的那条注释当初就是这么写的，检查器把它揪了出来。
        if 错 is None:
            return Result.fail(ErrorCode.DOCX_MALFORMED, "正文解析失败")
        return Result.fail(错.code, 错.detail)
    return Result.ok(_截断(转换.unwrap()))


def 元数据检查(zf: zipfile.ZipFile) -> None:
    """**元数据筛子**（D-078 + D-131 缺口一）。

    在 central directory 里读，**一个字节都不解压**。超限抛
    `zipfile.BadZipFile`——复用标准库的异常类型，
    由 `read_docx` 统一转成 `DOCX_BOMB_SUSPECTED`。

    **这只是一道筛子**：元数据可伪造（D-131 缺口二），
    真正的边界是 `_从zip读文本` 的流式限量解压。
    """
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        msg = f"docx 条目数 {len(infos)} 超限 {MAX_ENTRIES}"
        raise 限额超出(msg)

    total = 0
    for info in infos:
        if info.file_size > MAX_ENTRY:
            msg = f"{info.filename} 解压后 {info.file_size >> 20}MB 超单条目上限"
            raise 限额超出(msg)
        if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
            msg = f"{info.filename} 压缩比 {info.file_size / info.compress_size:.0f}:1 超限"
            raise 限额超出(msg)
        total += info.file_size
        if total > MAX_TOTAL:
            msg = f"解压总量超 {MAX_TOTAL >> 20}MB"
            raise 限额超出(msg)


def 流式限量解压(zf: _像zip者, name: str) -> Result[str]:
    """**流式限量解压**——D-131 的真边界。

    逐块读、累计计数、超 `MAX_ENTRY` 立即中止。
    **与真实体积解耦**：哪怕元数据伪报成 100 字节，
    真解出 40MB 也会在解到 20MB 时被拦下，内存峰值不随之失控。

    用 `zf.open(name)` 拿流而不是 `zf.read(name)`——
    后者一次性解出整个 entry，正是要避开的。
    """
    try:
        with zf.open(name) as 流:
            已读 = bytearray()
            while True:
                块 = 流.read(_读块大小)
                if not 块:
                    break
                已读.extend(块)
                if len(已读) > MAX_ENTRY:
                    msg = f"实际解压超 {MAX_ENTRY >> 20}MB（元数据可能伪报，D-131 缺口二）"
                    raise 限额超出(msg)
    except 限额超出 as exc:
        return Result.fail(ErrorCode.DOCX_BOMB_SUSPECTED, str(exc))
    except zipfile.BadZipFile as exc:
        return Result.fail(ErrorCode.DOCX_MALFORMED, f"docx 损坏：{exc}")
    except RuntimeError as exc:
        # 加密条目 / 非 seekable 流
        return Result.fail(ErrorCode.DOCX_MALFORMED, f"docx 读取失败：{type(exc).__name__}")
    return Result.ok(已读.decode("utf-8", errors="replace"))


def _截断(文本: str) -> 读到的文件:
    """截断到上限并**标明**（D-053 的两条）。"""
    if len(文本) <= MAX_CHARS:
        return 读到的文件(text=文本, 已截断=False, 完整字数=len(文本), 原始字数=len(文本))
    return 读到的文件(text=文本[:MAX_CHARS], 已截断=True, 完整字数=MAX_CHARS, 原始字数=len(文本))


def _正文转文本(xml_text: str) -> Result[str]:
    """把 WordprocessingML 转成纯文本：每个 `w:p`（段落）一行。

    **用 `iter()` 抓所有 `w:t`**（含表格内），段落边界用 `w:p` 判定。
    """
    try:
        根 = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        return Result.fail(ErrorCode.DOCX_MALFORMED, f"正文 XML 解析失败：{exc}")
    return Result.ok(_拼接(根))


def _拼接(根: ElementTree.Element) -> str:
    """按段落边界拼接文本。

    遍历全部 `w:p`，每个段落内部收集其 `w:t`；段落之间补换行。
    表格里的段落同样是 `w:p`，故自然被覆盖——
    **不需要单独处理表格**。
    """
    各段: list[str] = []
    for 段 in 根.iter(f"{_W_NS}p"):
        文本 = "".join(节点.text or "" for 节点 in 段.iter(_文本标签))
        各段.append(文本.strip())
    return "\n".join(段 for 段 in 各段 if 段).strip()
