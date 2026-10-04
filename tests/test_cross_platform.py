r"""跨平台兼容性测试。

## 诚实声明：本文件哪些是实测，哪些是静态推理

当前开发环境是 **Linux**。因此：

- **实测**：保留设备名判定、非法字符判定、换行行为、原子写的清理逻辑
- **静态推理（未经 Windows 实测）**：`fsync` 在 FAT32 上失败、
  `os.replace` 遇占用文件抛 `PermissionError`、junction 与 reparse point

后者在代码注释里逐处标注了。**不得把静态推理当成实测结论对外陈述。**

## 为什么 Linux 上要测 Windows 规则

保留设备名这类缺陷**在 Linux 上永远测不出来**——
因为它们在 Linux 上全部合法，一测就"通过"。
要抓出这类 bug，只能**在 Linux 上按 Windows 的规则去测**。

## 末节：本文件自己不许有平台假设

本文件曾经在一处 `tmp_path` 上断言 `.is_absolute()`（无害），
但真正的教训来自**别的文件**：有四处硬编码了 `/tmp`。

其中最坏的一处**不会报错，只会变成必绿的假判据**——
在 Linux 上「导出内容不含 `/tmp/`」成立，
换到 Windows 上导出的是 `C:\...`，那条断言**永远通过**。
一个永远通过的判据比没有判据更糟：它让人以为已经查过了。

故本文件加了自查：**任何测试文件都不得再出现硬编码的平台路径**。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from zhengtong.adapters import atomic, winname
from zhengtong.adapters.atomic import read_text, write_atomic
from zhengtong.adapters.guard import ensure_within
from zhengtong.adapters.paths import Workspace, ensure_dirs
from zhengtong.domain.errors import ErrorCode

# ═══ 测试文件不许硬编码平台路径 ═══

#: **写死的平台路径**。查的是字符串**字面量**，不是文件文本——
#: 写在文档字符串里讲解「为什么不能这么写」是应该的，��能算违规。
#:
#: `Path("/tmp/x")` 在 Windows 上会变成 `\tmp\x`——不是报错，是**静默变形**，
#: 于是判据换了平台就换了含义，而**没有一行会红**。这类退化最难发现。
#:
#: 盘符那个前瞻是为了不把 `https://` 里的 `s:/` 当成 `C:\`。
平台硬编码 = re.compile(
    r"""^[/\\](?:tmp|var|Users|home)[/\\]"""
    r"""|^[A-Za-z]:[/\\]""",
)


def _字符串字面量(文本: str) -> list[str]:
    """取文件里所有字符串字面量，**跳过文档字符串**。

    ## 为什么不能用正则扫全文

    一开始就是这么干的，结果一片误报：
    `https://example.invalid` 里的 `s:/` 被当成了 Windows 盘符、
    `PATH": "/usr/bin:/bin"` 同理、连本文件自己的说明文字都被算成违规。
    **判据一误报就没人敢信它**，比没有判据更糟。
    """
    import ast

    树 = ast.parse(文本)
    文档位置: set[int] = set()
    for 节 in ast.walk(树):
        if (
            isinstance(节, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and ast.get_docstring(节, clean=False) is not None
        ):
            # **显式收窄而不是 `cast`**：有 docstring 的节点，
            # `body[0]` 按构造必然是 `Expr(Constant)`，
            # 但类型系统不知道——那就不让它假装知道。
            首 = 节.body[0]
            assert isinstance(首, ast.Expr), f"{节.__class__.__name__} 的首句不是表达式"
            assert isinstance(首.value, ast.Constant), "docstring 不是字符串常量"
            文档位置.add(id(首.value))
    return [
        节.value
        for 节 in ast.walk(树)
        if isinstance(节, ast.Constant) and isinstance(节.value, str) and id(节) not in 文档位置
    ]


@pytest.mark.parametrize("名字", sorted(路径.name for 路径 in Path("tests").glob("test_*.py")))
def test_测试文件没有硬编码平台路径(名字: str) -> None:
    """**含本文件自己**——它自己也持有那几个字符串当反例。"""
    for 字面量 in _字符串字面量((Path("tests") / 名字).read_text(encoding="utf-8")):
        命中 = 平台硬编码.search(字面量)
        # 本文件里那几条是**故意的反例**，必须留着：自查被证伪时要靠它们。
        if 名字 == Path(__file__).name and 命中:
            continue
        assert 命中 is None, (
            f"{名字} 里硬编码了平台路径 {字面量!r}，换到 Windows 会静默变形——改用 tmp_path fixture"
        )


def test_本自查能抓住它要抓的东西() -> None:
    """**自查本身也得被证伪一次。**

    构造几条假的违规，若这条判据不红，说明它压根没在工作——
    一个永远绿的「反硬编码检查」是最讽刺的东西。
    """
    assert 平台硬编码.search("/tmp/x"), "漏了 Unix 临时目录"
    assert 平台硬编码.search("C:\\Users\\x"), "漏了 Windows 盘符"
    # 下面这些**不该**被算成违规
    assert not 平台硬编码.search("https://example.invalid/v1/chat"), "URL 被误判成盘符"
    assert not 平台硬编码.search("/usr/bin:/bin"), "PATH 里的冒号被误判成盘符"
    assert not 平台硬编码.search("data/output/主件.txt"), "普通相对路径被误判"


# ═══ 保留设备名（Linux 上也必须按 Windows 规则判）═══


@pytest.mark.parametrize(
    "name",
    ["CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9"],
)
def test_保留设备名被判不可用(name: str) -> None:
    """MS-DOS 保留名，Windows 上无法使用。"""
    assert winname.is_reserved(name)


def test_保留名大小写不敏感() -> None:
    """Windows 本身大小写不敏感，`nul.txt` 同样是设备。"""
    assert winname.is_reserved("nul")
    assert winname.is_reserved("NUL")
    assert winname.is_reserved("CoM1")


@pytest.mark.parametrize("name", ["NUL.txt", "con.docx", "LPT1.md"])
def test_保留名带扩展名仍然保留(name: str) -> None:
    """**必须连扩展名一起比对**——Windows 忽略扩展名，
    `NUL.txt` 指向的仍是空设备。任何"去掉扩展名就不算"的实现都是错的。"""
    assert winname.is_reserved(name)


@pytest.mark.parametrize("name", ["报告", "state", "data.json", "COM10", "LPT0"])
def test_非保留名判为可用(name: str) -> None:
    """`COM10` 超出保留范围（只有 1–9），**必须放行**。

    这条是反向测试：不测它，判定逻辑很容易写成前缀匹配，
    把 `COM10`、`LPT22` 这类正常文件名全误杀。
    """
    assert not winname.is_reserved(name)


# ═══ 非法字符 ═══


@pytest.mark.parametrize("ch", ["<", ">", ":", '"', "|", "?", "*"])
def test_windows禁止字符被判不可用(ch: str) -> None:
    assert winname.has_illegal_chars(f"报告{ch}最终稿")


@pytest.mark.parametrize("ch", ["\x00", "\x01", "\x1f"])
def test_控制字符被判不可用(ch: str) -> None:
    """控制字符在 Windows 上不可用，且会破坏日志与终端输出。"""
    assert winname.has_illegal_chars(f"报告{ch}")


def test_正常中文名不含非法字符() -> None:
    assert not winname.has_illegal_chars("关于XX工作的通知（定稿）.txt")


@pytest.mark.parametrize("name", ["报告.", "报告 ", "报告..."])
def test_结尾点或空格被判不可用(name: str) -> None:
    """Windows 会**静默剥掉**结尾的空格与点，
    于是 `报告.txt ` 和 `报告.txt` 指向同一个文件。与其让用户困惑，不如拒绝。"""
    assert winname.why_rejected(name) is not None


def test_报错原因须是中文且能指导行动() -> None:
    """用户看到的是这个字符串。"invalid name" 无从下手。"""
    r = winname.why_rejected("报告:v2.txt")
    assert r is not None
    assert ":" in r
    assert "Windows" in r


# ═══ 守卫集成 ═══


def test_守卫拦下保留设备名(tmp_path: Path) -> None:
    """`user/NUL/x.txt` 在 Windows 上会指向设备。

    守卫若只查文件名而不查每个路径分量，这个路径就会被放行，
    AI 写进去的内容**人间蒸发且不报错**。
    """
    _ = ensure_dirs(tmp_path)
    ws = Workspace(tmp_path)
    root = ws.dir("user")
    got = ensure_within(root, root / "NUL" / "x.txt")
    assert got.is_error
    assert got.error is not None
    assert "NUL" in got.error.detail


def test_守卫拦下非法字符(tmp_path: Path) -> None:
    _ = ensure_dirs(tmp_path)
    ws = Workspace(tmp_path)
    root = ws.dir("user")
    got = ensure_within(root, root / "报告*v2.txt")
    assert got.is_error


def test_守卫放行正常路径(tmp_path: Path) -> None:
    _ = ensure_dirs(tmp_path)
    ws = Workspace(tmp_path)
    root = ws.dir("user")
    assert ensure_within(root, root / "背景材料" / "上级文件.docx").is_ok


def test_守卫仍拦越出(tmp_path: Path) -> None:
    """加 winname 检查**不能把越出检查顶掉**——两件事都要做。"""
    _ = ensure_dirs(tmp_path)
    ws = Workspace(tmp_path)
    got = ensure_within(ws.dir("user"), ws.dir("system") / "config.toml")
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.PATH_ESCAPE


# ═══ 换行 ═══


def test_换行默认跟随平台(tmp_path: Path) -> None:
    """**不硬写 LF**。

    写死的 `newline="\\n"` 会在 Windows 上产出 LF-only 文件：
    记事本能开，但 Word 打开时行尾不认，排版可能整篇挤成一段。
    """
    p = tmp_path / "a.txt"
    assert write_atomic(p, "第一行\n第二行").is_ok
    raw = p.read_bytes()
    if os.linesep == "\r\n":
        assert b"\r\n" in raw, "Windows 上应用 CRLF"
    else:
        assert b"\r\n" not in raw


def test_换行可显式指定(tmp_path: Path) -> None:
    """调用方确知目标格式时可显式传入。"""
    p = tmp_path / "b.txt"
    assert write_atomic(p, "甲\n乙", newline="\n").is_ok
    assert b"\r\n" not in p.read_bytes()


# ═══ 原子写 ═══


def test_写入失败不留临时文件(tmp_path: Path) -> None:
    """失败路径必须清理——否则用户在自己目录里看到一堆 `.tmp`。"""
    got = write_atomic(tmp_path / "不存在" / "a.txt", "内容")
    assert got.is_error
    assert list(tmp_path.iterdir()) == [], "失败后残留了文件"


def test_连续写入不累积临时文件(tmp_path: Path) -> None:
    """反复写同一文件，临时文件不得堆积。"""
    p = tmp_path / "c.txt"
    for i in range(5):
        assert write_atomic(p, f"第{i}版").is_ok
    assert [q.name for q in tmp_path.iterdir()] == ["c.txt"]


def test_读回内容与写入一致(tmp_path: Path) -> None:
    p = tmp_path / "d.txt"
    文本 = "关于XX工作的通知\n\n一、总体要求\n（一）指导思想"
    assert write_atomic(p, 文本).is_ok
    got = read_text(p)
    assert got.is_ok
    assert got.unwrap() == 文本, "换行参数改过之后内容被动了"


def test_中文内容往返无损(tmp_path: Path) -> None:
    p = tmp_path / "e.txt"
    文本 = "党政公文写作，从源头控制质量。〇一二三四五六七八九十"
    assert write_atomic(p, 文本).is_ok
    got = read_text(p)
    assert got.is_ok
    assert got.unwrap() == 文本


def test_gb18030往返无损(tmp_path: Path) -> None:
    """D-044：GB18030 是超集，生僻字不丢。"""
    p = tmp_path / "f.txt"
    文本 = "生僻字：镕、龘、犇"
    assert write_atomic(p, 文本, encoding="gb18030").is_ok
    got = read_text(p, encoding="gb18030")
    assert got.is_ok
    assert got.unwrap() == 文本


def test_cleanup对空路径安全() -> None:
    """`tmp` 未被赋值时（如 `open` 就抛错）不能炸。"""
    assert atomic.cleanup(None) is None


def test_cleanup对不存在文件安全(tmp_path: Path) -> None:
    """清不掉就算了——它只是垃圾文件，不值得把结果变成另一类错误。"""
    assert atomic.cleanup(tmp_path / "根本没有.tmp") is None
