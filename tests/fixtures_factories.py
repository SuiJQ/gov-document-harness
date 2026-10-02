"""第 7 步测试用的**样本与攻击样本构造器**。

不是测试本身——那些在 `test_readers.py` / `test_user_files.py` 里。
这里只负责「造得出畸形文件」。

## 为什么自己造 docx 而不用 python-docx

python-docx **不在依赖里**（运行时依赖只有 textual / httpx / tomli-w，
D-032 要求 docx 走 `zipfile` + `xml.etree` 零第三方依赖）。
测试若引入它，就等于给「依赖精简」开了个后门。

## 为什么必须造 zip bomb 而不是照抄架构里的数字

架构 §6.3 与 D-131 给的实测数据（190KB→190MB、10MB→23.4MB 峰值）
是**别人的机器上的测量**。本模块重新造一遍并实测，
是为了确认**在这个 Python 版本、这个平台下数字仍成立**——
`zlib` 的行为跨版本会变，而我们的防护依赖它。

**照抄数字不叫验证，重新造一遍并实测才叫。**
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

# docx 的最小合法结构：一个正文段落。
#: 注意 `w:` 前缀——WordprocessingML 的真命名空间。
#: 用假命名空间的话，解出来是空段落，测不出「正文能不能读对」。
段落 = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body><w:p><w:r><w:t>{文本}</w:t></w:r></w:p></w:body></w:document>"
)

内容类型 = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/>'
    "</Types>"
)


def 造docx(路径: Path, 文本: str = "这是正文。") -> Path:
    """造一个最小但**合法**的 docx。"""
    with zipfile.ZipFile(路径, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", 内容类型)
        zf.writestr("word/document.xml", 段落.format(文本=文本))
    return 路径


def 造多段docx(路径: Path, 段数: int, 每段字: int = 10) -> Path:
    """造一个含多段落的 docx——用来测**段落合并**而不是只测单段。"""
    各段 = "".join(
        f"<w:p><w:r><w:t>{'第' + str(i) + '段' + '字' * 每段字}</w:t></w:r></w:p>"
        for i in range(段数)
    )
    文档 = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{各段}</w:body></w:document>"
    )
    with zipfile.ZipFile(路径, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", 内容类型)
        zf.writestr("word/document.xml", 文档)
    return 路径


def 造zip_bomb(路径: Path, 解压后大小: int = 190 * 1024 * 1024) -> Path:
    """造一个**高压缩比**的 zip bomb。

    磁盘上只有几百 KB，解压后是 `解压后大小`。
    全部字节相同，故 deflate 能压到极小——**这正是 bomb 的原理**。

    **分块写入**而不是 `writestr(巨大字符串)`：
    `writestr` 会先在内存里造出完整的解压后数据，
    那测试自己就先 OOM 了，测不出防护是否生效。
    """
    块 = b"\x00" * (1024 * 1024)
    with (
        zipfile.ZipFile(路径, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf,
        zf.open("word/document.xml", "w") as 流,
    ):
        for _ in range(解压后大小 // len(块)):
            流.write(块)
    return 路径


def 造条目多zip(路径: Path, 条目数: int = 200_000) -> Path:
    """造一个条目数超限的包（D-131 缺口一）。

    **用 STORED 而非 DEFLATED**：条目少、体积小，构造快，
    且 `file_size` 与 `compress_size` 相等故压缩比检查不会误报——
    **只让条目数这一项触发**，否则测不出该项是否真在起作用。
    """
    with zipfile.ZipFile(路径, "w", zipfile.ZIP_STORED) as zf:
        for i in range(条目数):
            zf.writestr(f"f{i}.bin", b"x")
    return 路径


def 伪造file_size(路径: Path, 目标: str, 伪报大小: int = 100) -> Path:
    """**弃用**——手改 central directory 会破坏包的完整性。

    实测：单独改 `uncompressed size` 会让 `zipfile` 打开即抛
    `BadZipFile`（交叉引用校验不过），不是「元数据说小、真解出大」。

    D-131 缺口二的等价测法（更诚实）见 `test_readers.py`：
    **用假 `ZipFile` 让 `infolist()` 全报小值，`read()` 走真解压**，
    直接验证防护逻辑本身，绕开「手工伪造能否成功」这个与被测无关的问题。
    """
    msg = "伪造 file_size 已弃用：会破坏包完整性。改用 test_readers 里的假 ZipFile 路径。"
    raise NotImplementedError(msg)


def 造只超压缩比zip(路径: Path) -> Path:
    """**只**触发压缩比检查的样本（2MB 全零，比约 1000）。

    **为什么每个检查都要单独造样本**：原来的 190MB bomb 同时超
    单 entry（190>20MB）、总量（190>100MB）、压缩比（1029>100）三项——
    **关掉任何一项，另外两项照样拦住，测试全绿**。
    实测三个「单项检查」的判据全是假阳性：把压缩比、单 entry、
    总量检查分别关掉，测试仍然 15 passed。

    **一个判据必须只有它自己在起作用**，否则测不出「是谁拦的」。
    """
    with zipfile.ZipFile(路径, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("word/document.xml", b"\x00" * (2 * 1024 * 1024))
    return 路径


def 造只超单entryzip(路径: Path, 大小: int) -> Path:
    """**只**触发单 entry 上限的样本。

    用随机数据：deflate 压不动，故压缩比约 1:1（不超压缩比），
    而大小超过 `MAX_ENTRY`。体积小于 `MAX_TOTAL` 故不触发总量检查。
    """
    with zipfile.ZipFile(路径, "w", zipfile.ZIP_DEFLATED) as zf:
        zi = zipfile.ZipInfo("word/document.xml")
        zi.compress_type = zipfile.ZIP_DEFLATED
        with zf.open(zi, "w") as 流:
            已写 = 0
            while 已写 < 大小:
                块 = min(1024 * 1024, 大小 - 已写)
                流.write(os.urandom(块))
                已写 += 块
    return 路径


def 造只超总量zip(路径: Path) -> Path:
    """**只**触发总量上限的样本。

    多个 entry，**每个都低于 `MAX_ENTRY`**（19MB），
    加起来超过 `MAX_TOTAL`（100MB）。每项用随机数据以避开压缩比检查。
    """
    with zipfile.ZipFile(路径, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(7):
            zi = zipfile.ZipInfo(f"f{i}.xml")
            zi.compress_type = zipfile.ZIP_DEFLATED
            with zf.open(zi, "w") as 流:
                已写 = 0
                while 已写 < 19 * 1024 * 1024:
                    块 = min(1024 * 1024, 19 * 1024 * 1024 - 已写)
                    流.write(os.urandom(块))
                    已写 += 块
    return 路径
