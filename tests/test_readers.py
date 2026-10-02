"""docx 读取器的契约测试（第 7 步）。

重点压在 **D-131 缺口二**：元数据可伪造，故元数据检查**不是安全边界**，
真正的边界是**流式限量解压**。这组测试要证明：

- 元数据检查（筛子）能拦住**诚实但超大**的包；
- 元数据检查放过时，**流式解压的计数中止**能拦住**实际超限**的包。

## 为什么造假 ZipFile 而不是手改 central directory

手改 `uncompressed size` 会破坏包的交叉引用，`zipfile` 打开即抛
`BadZipFile`——**手工伪造根本构造不出「元数据说小、真解出大」的包**。

故这里让假 `ZipFile` 的 `infolist()` **报小值**，
`read()` **走真解压**。这样测的是**防护逻辑本身**，
绕开「手工伪造能否成功」这个与被测无关的问题。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import IO, cast

from fixtures_factories import (
    造docx,
    造zip_bomb,
    造只超单entryzip,
    造只超压缩比zip,
    造只超总量zip,
    造多段docx,
    造条目多zip,
)
from zhengtong.adapters.userfiles.readers.docx import (
    MAX_ENTRIES,
    MAX_ENTRY,
    read_docx,
)
from zhengtong.domain.errors import ErrorCode


#: D-131 缺口二的假流。**放模块顶层而非函数内**：
#: BasedPyright 1.40 对**局部类**的继承链判定有缺陷——
#: 顶层的 `class 流(io.RawIOBase)` 能通过 `IO[bytes]` 检查，
#: 同样的代码放进函数里就报 `reportArgumentType`（实测复现）。
#: **不是代码错，是检查器对局部类的限制**，故绕开而非硬改类型。
class _假流(io.RawIOBase):
    """模拟 `ZipExtFile`：分块吐出**预定量**数据。

    `read(n)` 最多返回 n 字节——与真实现同节奏。
    若读取器改成 `zf.read()` 一次性读，**拦截仍必须发生**
    （那正是 D-131 缺口二要抓的：只信元数据就完了）。
    """

    def __init__(self, 总量: int) -> None:
        super().__init__()
        self.剩 = 总量
        #: 被 `read(n)` 调用的次数——用来证明**确实是分块读的**
        self.读次数 = 0
        #: 每次请求的字节数。一次性 `read()` 会请求一个巨大的 n。
        self.各次请求: list[int] = []

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def writable(self) -> bool:
        return False

    def read(self, n: int = -1) -> bytes:
        self.读次数 += 1
        self.各次请求.append(n)
        这次 = self.剩 if n < 0 else min(n, self.剩)
        self.剩 -= 这次
        return b"\x00" * 这次


class _假ZipInfo:
    """元数据**伪报**成 100 字节。三项元数据检查因此全部通过。"""

    filename = "word/document.xml"
    file_size = 100
    compress_size = 50


class _假ZipFile:
    """`infolist()` 全报小值，`open()` 走真路径解压出超量数据。

    **同样必须在模块顶层**——局部类会踩 BasedPyright 1.40
    对继承链判定的缺陷（见 `_假流` 的说明）。
    """

    def __init__(self, 总量: int) -> None:
        self._总量 = 总量
        self.开的流: list[_假流] = []

    def infolist(self) -> list[_假ZipInfo]:
        return [_假ZipInfo()]

    def open(self, name: str) -> IO[bytes]:
        # **这里 cast 而非改生产契约**。
        # 实测两次：`io.RawIOBase` 子类能过 `IO[bytes]` 检查
        # （/tmp/probe.py 通过），加上自定义 `__init__` + 覆写
        # `read(n=-1)` 之后就过不了（/tmp/probe3.py 报错）——
        # 是 BasedPyright 1.40 对 RawIOBase 覆写签名的判定缺陷，
        # **不是我们的类型标错了**。真 `ZipFile.open` 的返回
        # 满足生产侧的 `_像zip者`（已实测通过），契约本身没问题。
        #
        # **测试替身可以 cast，生产契约不能为了让替身过关而放宽**——
        # 早先为消这个报错去缩 `Protocol` 的返回类型，
        # 结果真 `ZipFile` 立刻不兼容（已回滚）。
        造 = _假流(self._总量)
        self.开的流.append(造)
        return cast("IO[bytes]", cast("object", 造))

    def read(self, name: str) -> bytes:
        # 若读取器误用一次性 read，这里会直接给全量——**拦截仍须发生**
        return b"\x00" * self._总量

    def close(self) -> None:
        pass


# ═══ 一、正常读取 ═══


def test_读出正文(tmp_path: Path) -> None:
    p = 造docx(tmp_path / "a.docx", "这是正文内容")
    got = read_docx(p)
    assert got.is_ok, got.error
    assert "这是正文内容" in got.unwrap().text


def test_多段落按顺序合并(tmp_path: Path) -> None:
    p = 造多段docx(tmp_path / "b.docx", 段数=5, 每段字=3)
    got = read_docx(p)
    assert got.is_ok, got.error
    文本 = got.unwrap().text
    assert "第0段" in 文本 and "第4段" in 文本, "段落丢了"
    assert 文本.index("第0段") < 文本.index("第4段"), "段落顺序错了"


def test_表格里的文字也要读出来(tmp_path: Path) -> None:
    """真实公文大量用表格（发文机关、抄送等）。

    `w:t` 在表格单元格里同样出现，
    只读 `w:p` 直系子节点会漏掉整张表——**漏读比读错更危险**：
    用户不会知道自己少看了内容。
    """
    文档 = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:tbl><w:tr><w:tc>"
        "<w:p><w:r><w:t>发文机关名称</w:t></w:r></w:p>"
        "</w:tc></w:tr></w:tbl></w:body></w:document>"
    )
    p = tmp_path / "c.docx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("word/document.xml", 文档)
    got = read_docx(p)
    assert got.is_ok, got.error
    assert "发文机关名称" in got.unwrap().text, "表格里的文字被漏掉了"


# ═══ 二、畸形输入必须返回 fail，绝不抛异常（D-108 纪律）═══


def test_不是zip的文件返回fail不抛异常(tmp_path: Path) -> None:
    p = tmp_path / "fake.docx"
    p.write_text("这根本不是 zip", encoding="utf-8")
    got = read_docx(p)
    assert got.is_error, "非 zip 文件被当成了合法 docx"
    assert got.error is not None
    assert got.error.code is ErrorCode.DOCX_MALFORMED


def test_缺正文entry返回fail(tmp_path: Path) -> None:
    p = tmp_path / "empty.docx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
    got = read_docx(p)
    assert got.is_error, "没有正文的包被当成了合法 docx"


def test_空文件返回fail(tmp_path: Path) -> None:
    p = tmp_path / "zero.docx"
    p.write_bytes(b"")
    got = read_docx(p)
    assert got.is_error


def test_不存在的文件返回fail(tmp_path: Path) -> None:
    got = read_docx(tmp_path / "nope.docx")
    assert got.is_error


def test_xml畸形返回fail(tmp_path: Path) -> None:
    """XML 解析失败必须转成 `DOCX_MALFORMED`，
    **不能把 `ParseError` 抛穿到调用方**（D-108）。"""
    p = tmp_path / "bad.docx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("word/document.xml", "<w:document><未闭合>")
    got = read_docx(p)
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.DOCX_MALFORMED


# ═══ 三、zip bomb 四道防线 ═══


def test_真实zip_bomb被拦(tmp_path: Path) -> None:
    """真实 zip bomb：磁盘 189KB，解压 190MB，压缩比 1029。

    **这份样本三项全超**，所以它验的是「真 bomb 拦得住」，
    不能用来验证单个检查——见下面三条。
    """
    p = 造zip_bomb(tmp_path / "bomb.docx", 解压后大小=190 * 1024 * 1024)
    got = read_docx(p)
    assert got.is_error, "190MB 的 bomb 竟然读成功了"
    assert got.error is not None
    assert got.error.code is ErrorCode.DOCX_BOMB_SUSPECTED


def test_压缩比超限被拦(tmp_path: Path) -> None:
    """**只**超压缩比：2MB 全零，比约 1000，体积低于单 entry 与总量上限。

    为什么必须单独造：190MB 的 bomb **三项全超**，
    关掉压缩比检查后单 entry 与总量照样拦住，测试照样全绿——
    **实测这就是假阳性判据。**
    """
    p = 造只超压缩比zip(tmp_path / "ratio.docx")
    got = read_docx(p)
    assert got.is_error, "压缩比 1000:1 竟然通过了"
    assert got.error is not None
    assert got.error.code is ErrorCode.DOCX_BOMB_SUSPECTED


def test_条目数超限被拦(tmp_path: Path) -> None:
    """D-131 缺口一：条目数上限。

    20 万条目的包三项旧检查全部通过，只有条目数上限能拦。
    """
    p = 造条目多zip(tmp_path / "many.docx", 条目数=MAX_ENTRIES + 1)
    got = read_docx(p)
    assert got.is_error, f"{MAX_ENTRIES + 1} 个条目竟然读成功了"
    assert got.error is not None
    assert got.error.code is ErrorCode.DOCX_BOMB_SUSPECTED


def test_恰好等于上限的条目数通过(tmp_path: Path) -> None:
    """**边界值本身必须通过**——`>` 而不是 `>=`。

    写成 `>=` 会把合法的临界包也拦掉，而那条测试若不存在，
    这个 off-by-one 就永远没人发现。
    """
    p = 造条目多zip(tmp_path / "edge.docx", 条目数=MAX_ENTRIES)
    # 这个包没有 document.xml，会因缺正文而 fail——但**不是** bomb 错
    got = read_docx(p)
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.DOCX_MALFORMED, "边界值被误判成 bomb 了"


def test_单entry超限被拦(tmp_path: Path) -> None:
    """**只**超单 entry 上限：随机数据（压比约 1:1，体积低于总量上限）。"""
    p = 造只超单entryzip(tmp_path / "big.docx", MAX_ENTRY + 2 * 1024 * 1024)
    got = read_docx(p)
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.DOCX_BOMB_SUSPECTED


def test_解压总量超限被拦(tmp_path: Path) -> None:
    """**只**超总量上限：7 个 19MB（每个低于 20MB 单 entry 上限），
    加起来超 100MB。随机数据故压缩比约 1:1，不误触压缩比检查。"""
    p = 造只超总量zip(tmp_path / "total.docx")
    got = read_docx(p)
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.DOCX_BOMB_SUSPECTED


# ═══ 四、D-131 缺口二：元数据全过时，流式解压必须兜住 ★最要紧 ═══


def test_元数据全过但实际解压超限被兜住() -> None:
    """**这一条是 D-131 的核心，也是元数据检查挡不住的那一种。**

    构造一个假 `ZipFile`：
    - `infolist()` 报**极小**的 `file_size` 与正常的 `compress_size`
      → 压缩比、三项元数据检查**全部通过**；
    - `read()` **真的**解压出一大块数据（远超 `MAX_ENTRY`）。

    若读取器只信元数据（用 `zf.read()` 一次性读），
    内存会被真解压量吃掉——**这正是 D-131 实测到的 209MB 峰值**。

    正确的实现必须走**流式限量解压**：分块读、累计计数、超限立即中止。
    本条就是验证那个「立即中止」真的会发生。

    **注意本条不碰真实文件系统**——直接传假对象，
    因为要测的是防护逻辑分支，不是文件读取。
    """
    真实解压量 = MAX_ENTRY * 2  # 40MB，远超上限但单个元数据说是 100 字节

    class 假ZipInfo:
        filename = "word/document.xml"
        file_size = 100  # ← 元数据说 100 字节（伪报）
        compress_size = 50  # ← 压缩比 2:1，元数据检查过得去

    from zhengtong.adapters.userfiles.readers import docx as 模块

    假zip = _假ZipFile(真实解压量)
    got = 模块.流式限量解压(假zip, "word/document.xml")
    assert got.is_error, (
        f"元数据说 100 字节、真解压出 {真实解压量 // 1024 // 1024}MB，"
        "竟然读成功了——说明防护只信元数据"
    )
    assert got.error is not None
    assert got.error.code is ErrorCode.DOCX_BOMB_SUSPECTED

    # ── 上面的断言只证明「结果被拦住」，**证明不了是流式拦的** ──
    # 改成 `zf.read()` 一次性读完再查长度，同样能返回 is_error，
    # 但**真 bomb 上会先把整块解压进内存**——那正是 D-131 要防的。
    # 故补两条过程断言：分块、且块不大。
    assert 假zip.开的流, "没有流式打开过任何东西"
    流 = 假zip.开的流[0]
    assert 流.读次数 > 1, (
        f"只读了 {流.读次数} 次——一次性读完再查长度虽然也能拦住，"
        "但内存峰值会跟着真体积走（D-131 实测 209MB）"
    )
    assert max(流.各次请求) <= 模块._读块大小, (
        f"单次请求 {max(流.各次请求)} 字节，超过块大小——说明没在限量读"
    )


def test_流式解压读小文件正常通过(tmp_path: Path) -> None:
    """兜底逻辑**不能误伤正常文件**——上面那条的对照。"""
    p = 造多段docx(tmp_path / "ok.docx", 段数=100, 每段字=50)
    got = read_docx(p)
    assert got.is_ok, f"正常文件被误判：{got.error}"
    assert "第99段" in got.unwrap().text
