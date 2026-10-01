r"""`adapters/guard.py` 测试 —— 路径守卫，**整个系统唯一允许碰路径的地方**。

## 这个模块的判据为什么特殊

D-096 规定守卫必须用 `realpath + normcase + relative_to`，并覆盖
符号链接、`..`、大小写、Windows junction/reparse point。

但第 2 批 D-126 的教训在这里格外适用：
**平台相关的行为，在错误的平台上永远测不出来**。

本文件的测试刻意包含大量**在 Linux 上看起来"无害"的输入**——
`r"data\NUL\x.txt"` 在 Linux 上只是一个含反斜杠的普通文件名，
不构成任何风险。但程序要跑在 Windows 上（D-003），
而 Windows 会把它拆成三段，`NUL` 是保留设备名。

于是这里有一条测试，在修复前是**失败的**，在 Linux 上跑也能失败——
因为 D-133 让检查不再依赖运行平台的路径解析。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zhengtong.adapters.guard import ensure_within, is_within, resolve
from zhengtong.domain.errors import ErrorCode


@pytest.fixture
def 根(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "user").mkdir()
    return tmp_path


# ═══ D-133：设备名检查不得依赖运行平台的路径解析 ═══


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("data\\NUL\\x.txt", id="反斜杠分隔的NUL"),
        pytest.param("data\\CON\\y.txt", id="反斜杠分隔的CON"),
        pytest.param("data\\LPT9\\z.txt", id="反斜杠分隔的LPT9"),
        pytest.param("data/NUL/x.txt", id="正斜杠分隔的NUL"),
        pytest.param("data\\COM1.txt", id="反斜杠分隔的COM1"),
    ],
)
def test_两种分隔符下的保留设备名都须拦(根: Path, raw: str) -> None:
    """**本批最关键的测试**。

    修复前：`Path(r"data\\NUL\\x.txt").parts` 在 POSIX 上是
    **一个整体**（反斜杠不是分隔符），`why_rejected` 返回 `None`，
    守卫放行，文件被真的写了出来。搬到 Windows 上，
    `NUL` 被当成设备名——「写进去、找不回来，且没有任何报错」。

    这条测试**在 Linux 上也能失败**，因为修复让检查不再依赖平台。
    这正是它存在的意义：此前整套防护的反斜杠分支在 Linux 上永不执行。
    """
    got = ensure_within(根, 根 / raw)
    assert got.is_error, f"{raw!r} 未被拦截——反斜杠分支在当前平台未被执行"
    assert got.error is not None
    assert got.error.code is ErrorCode.PATH_ESCAPE


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("data/我的NUL文件/x.txt", id="名字里含NUL但不是NUL"),
        pytest.param("data/CON报告/y.txt", id="名字里含CON但不是CON"),
        pytest.param("data/NULL/x.txt", id="NULL不是保留设备名"),
        pytest.param("data/COM0/x.txt", id="COM0不存在"),
        pytest.param("data/COM10/x.txt", id="COM10超出COM1-9"),
        pytest.param("data/正常.txt", id="普通名"),
        pytest.param("data/子目录/深层/文件.txt", id="多层"),
    ],
)
def test_不得误伤含保留名的正常路径(根: Path, raw: str) -> None:
    """**D-133 修复的另一半：不能简单用「名字里含保留名」来判断**。

    `我的NUL文件` 含 `NUL` 但不是保留名，Windows 上完全合法。
    若图省事写成 `if "NUL" in 分量`，这类正常路径会被全部误杀——
    而 D-012 说 `user/` 里的文件名由用户自己定，程序不得阻拦。
    """
    assert ensure_within(根, 根 / raw).is_ok, f"{raw!r} 被误伤了"


# ═══ 越权拦截 ═══


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("../x.txt", id="父目录"),
        pytest.param("../../etc/passwd", id="多级父目录"),
        pytest.param("/etc/passwd", id="绝对路径"),
        pytest.param("data/../../x.txt", id="中途折返"),
    ],
)
def test_越权路径须拦(根: Path, raw: str) -> None:
    got = ensure_within(根, 根 / raw)
    assert got.is_error, f"{raw!r} 越权未被拦截"
    assert got.error is not None
    assert got.error.code is ErrorCode.PATH_ESCAPE


def test_data_evil不被误判(根: Path) -> None:
    """`data-evil/` 不该被判为在 `data/` 下。

    这是 `relative_to` 的价值：它按路径分量比，
    不做字符串前缀匹配。
    """
    (根 / "data-evil").mkdir()
    assert ensure_within(根 / "data", 根 / "data-evil" / "x.txt").is_error


# ═══ 符号链接 ═══


def test_软链接指向工作区外须拦(根: Path) -> None:
    """模块开头那个例子：`user/evil.txt -> /etc/passwd`。

    按字面路径看它在允许的根下，但 `realpath` 之后它指向系统文件。
    """
    外部 = 根.parent / "外部文件.txt"
    _ = 外部.write_text("外部机密", encoding="utf-8")
    _ = (根 / "user" / "evil.txt").symlink_to(外部)

    assert ensure_within(根 / "user", 根 / "user" / "evil.txt").is_error


def test_软链接指向工作区内须按真实落点判(根: Path) -> None:
    """软链接指向**工作区内的另一目录**时，要按真实落点判权限。

    这正是第 2 批 D-126 在 fs_storage 里踩的坑——
    同一类错误出现在守卫的调用方。
    """
    (根 / "data" / "t").symlink_to(根 / "user")
    # 落点在 user/ 下，判 user/ 的权限 → 通过（AI 可以读 user/）
    assert ensure_within(根, 根 / "data" / "t" / "材料.txt").is_ok
    # 但同一路径按 data/ 的权限判也「通过」——这说明
    # **权限必须在解析后判**（D-126 的结论），守卫只保证不出工作区


def test_不存在的路径可通过(根: Path) -> None:
    """**「先创建再校验」不是前提**，写入前即可判定。

    这条对 D-127 有意义：产物目录常常还不存在，
    守卫必须能对它未来的路径做出判定。
    """
    got = ensure_within(根 / "data", 根 / "data" / "从未存在" / "深层" / "新文件.txt")
    assert got.is_ok


def test_不存在的越权路径须拦(根: Path) -> None:
    """不存在的路径若越权，同样要拦——
    不能因为「反正还不存在」就放行。"""
    assert ensure_within(根 / "data", 根 / "不存在" / ".." / ".." / "x.txt").is_error


# ═══ 根是软链接 ═══


def test_根为软链接时以其真实目标为基准(tmp_path: Path) -> None:
    """**否则一个指向 `/` 的软链根会让所有路径都「合法」**。

    这是模块文档明确声称的性质，必须测。
    """
    真根 = tmp_path / "真实根"
    _ = (真根 / "data").mkdir(parents=True)
    链根 = tmp_path / "链根"
    链根.symlink_to(真根)

    # 真实目标下的文件 → 通过
    assert ensure_within(链根, 真根 / "data" / "a.txt").is_ok
    # 工作区外的文件 → 须拦（若基准用了软链字面路径，这会通过）
    assert ensure_within(链根, tmp_path / "别处" / "b.txt").is_error


# ═══ resolve 与 is_within ═══


def test_resolve做归一(根: Path) -> None:
    assert resolve(根 / "data" / ".." / "user") == resolve(根 / "user")


def test_is_within只返回布尔(根: Path) -> None:
    assert is_within(根, 根 / "data" / "a.txt")
    assert not is_within(根, 根 / ".." / "outside.txt")


def test_ensure_within返回解析后的路径(根: Path) -> None:
    """通过时返回的是**解析后**的绝对路径，不是原样的入参。

    调用方拿它去写文件，必须已经是解析过的——
    否则等于又用字面路径去操作。
    """
    got = ensure_within(根, 根 / "user" / "m.txt")
    assert got.is_ok
    assert got.unwrap() == resolve(根 / "user" / "m.txt")


# ═══ 拒绝是返回不是抛异常（D-076）═══


def test_越权须返回失败而非抛异常(根: Path) -> None:
    """**D-076：拒绝是正常返回**。

    抛异常会把整个流程带崩，而崩掉对「换个工具再试」毫无帮助——
    模型需要的是一句「你没权限」，不是一个 traceback。
    """
    got = ensure_within(根, "/etc/passwd")
    assert got.is_error, "越权应返回失败，不该抛异常"


def test_错误说明须能指导行动(根: Path) -> None:
    """报错要让人知道**为什么**、**是哪个分量**。"""
    got = ensure_within(根, 根 / "data" / "NUL" / "x.txt")
    assert got.error is not None
    assert "NUL" in got.error.detail
    assert "设备名" in got.error.detail
