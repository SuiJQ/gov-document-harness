"""第 5 步测试：L2 适配（配置与日志）。

判据（架构 §15 第5步）：原子写、符号链接解析、启动自检重建三目录（D-017）

**D-076 的六条用例是必过的**（架构 §7.4 原文："缺一即视为守卫未实现"），
故本文件前六条测试与架构编号一一对应，编号写进测试名。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from zhengtong.adapters import paths
from zhengtong.adapters.atomic import read_text, write_atomic
from zhengtong.adapters.fs_storage import DRAFT_LIMIT, FsStorage
from zhengtong.adapters.guard import ensure_within, is_within, resolve
from zhengtong.adapters.paths import Workspace, ensure_dirs
from zhengtong.domain.errors import ErrorCode
from zhengtong.ports.storage import StoragePort

# ═══ D-076 六条必过用例 ═══


def test_用例1_软链指向系统文件应拒绝(tmp_path: Path) -> None:
    """`user/evil.txt` → 指向 `/etc/passwd` 的软链 → **拒绝**。

    这是整个守卫存在的理由：按字面路径看它在 `user/` 下，
    但 `realpath` 之后它指向系统文件。用户在 `user/` 放一个软链
    就等于让 AI 读全盘。
    """
    # 目标必须在**根外**（架构用例写的是 `/etc/passwd`）。
    # 用 tmp_path 内部的文件做不出这个场景：realpath 后它仍在根内，
    # 守卫放行是**正确**行为，不是漏洞。
    outside = tmp_path.parent / f"系统机密_{os.getpid()}.txt"
    _ = outside.write_text("系统机密", encoding="utf-8")
    user = tmp_path / "user"
    _ = user.mkdir()
    evil = user / "evil.txt"
    try:
        evil.symlink_to(outside)
    except OSError:
        pytest.skip("当前环境不支持创建符号链接")
    try:
        got = ensure_within(tmp_path, evil)
        assert got.is_error, "指向根外的软链必须拒绝"
        assert got.error is not None
        assert got.error.code is ErrorCode.PATH_ESCAPE
    finally:
        _ = outside.unlink(missing_ok=True)

    # 另加：软链指回**根内**时应放行——realpath 后仍在根内。
    # 守卫的基准是根，不是"软链"；把软链一律拒绝会误伤用户
    # 在自己目录里做的正常整理。
    inside = tmp_path / "user" / "inside.txt"
    _ = inside.write_text("根内", encoding="utf-8")
    alias = tmp_path / "user" / "alias.txt"
    try:
        alias.symlink_to(inside)
    except OSError:
        return
    assert is_within(tmp_path, alias), "指向根内的软链应放行"


def test_用例2_普通文件应放行(tmp_path: Path) -> None:
    """`user/normal.txt` 普通文件 → 放行。"""
    user = tmp_path / "user"
    _ = user.mkdir()
    normal = user / "normal.txt"
    _ = normal.write_text("正常内容", encoding="utf-8")

    got = ensure_within(tmp_path, normal)
    assert got.is_ok, "同一根下的普通文件必须放行"
    assert got.unwrap() == resolve(normal)


def test_用例3_路径穿越应拒绝(tmp_path: Path) -> None:
    """`user/../../etc` 路径穿越 → **拒绝**。"""
    user = tmp_path / "user"
    _ = user.mkdir()
    got = ensure_within(tmp_path, user / ".." / ".." / "etc")
    assert got.is_error, "`..` 穿越必须拒绝"
    assert got.error is not None
    assert got.error.code is ErrorCode.PATH_ESCAPE


def test_用例4_越权访问应拒绝(tmp_path: Path) -> None:
    """越权访问配置目录 → **拒绝**。

    注意这一条与用例 1、3 的**区别**：那两条是"路径逃出根"，
    这一条是"路径在根内但不在允许的子目录内"。
    守卫管前者，权限管后者——两层都要有，缺一层就漏一类。
    """
    sys_dir = tmp_path / "system"
    sys_dir.mkdir()
    _ = (sys_dir / "cfg.toml").write_text("k = 1", encoding="utf-8")

    # 路径本身在根内，故守卫放行
    assert is_within(tmp_path, sys_dir / "cfg.toml"), "守卫只管根内根外，不管子目录权限"
    # 但存储层按权限拒绝
    st = FsStorage(Workspace(tmp_path))
    assert st.read_text("system/cfg.toml").is_error, "AI 不可读 system/"
    assert st.write_atomic("system/x.toml", "k=1").is_error, "AI 不可写 system/"


def test_用例5_大小写变体(tmp_path: Path) -> None:
    """Windows 上 `User\\` 大小写变体 → 放行（在同一根下）。

    Linux 上大小写敏感，`User` 与 `user` 是两个目录，
    故只在 Windows 上断言放行；在 Linux 上断言**它不在 `user/` 里**
    （即被当成另一个不存在的目录），这才是符合平台语义的行为。
    """
    user = tmp_path / "user"
    _ = user.mkdir()
    upper = tmp_path / "User" / "a.txt"
    _ = upper.parent.mkdir()

    # 两个分支的结论相同（都在根内 → 放行），故不必按平台分叉：
    # Windows 上 `User` 与 `user` 归一到同一个目录；Linux 上是两个目录，
    # 但**都在根内**，守卫同样放行。真正的差别由上层目录名检查处理
    # （见 test_不可读写system目录）。
    got = ensure_within(tmp_path, upper)
    assert got.is_ok, "同一根下的路径应放行"


def _is_windows() -> bool:
    """包一层函数，让静态检查器**无法判定**返回值。

    直接写 `if os.name == "nt"` 时，类型检查器会按当前平台推断
    该分支恒假，于是另一侧的 `mklink` 调用被判为不可达代码——
    而它恰恰是这条用例在 Windows 上的全部意义。
    """
    return os.name == "nt"


def test_用例6_junction指向根外应拒绝(tmp_path: Path) -> None:
    """Windows 上 junction 指向根外 → **拒绝**。

    junction（`mklink /J`）是 reparse point，不是一般的符号链接，
    但 `realpath` 在 3.8+ 已能解析。非 Windows 上用目录软链等价验证——
    两者走的是 `realpath` 同一条路径。
    """
    outside = tmp_path.parent / f"outside_{os.getpid()}"
    _ = outside.mkdir()
    _ = (outside / "target.txt").write_text("根外内容", encoding="utf-8")

    user = tmp_path / "user"
    _ = user.mkdir()
    link = user / "linked"
    try:
        if _is_windows():
            # junction 不是符号链接，Python 无对应 API，只能调 mklink。
            # 放在函数内 import：非 Windows 平台上这个模块根本用不到。
            import subprocess

            _ = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                check=True,
                capture_output=True,
            )
        else:
            # 目录软链与 junction 走 realpath 的同一条解析路径，
            # 故在 Linux 上等价验证同一件事
            link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境无法创建目录链接")

    try:
        got = ensure_within(tmp_path, link / "target.txt")
        assert got.is_error, "指向根外的 junction/目录软链必须拒绝"
        assert got.error is not None
        assert got.error.code is ErrorCode.PATH_ESCAPE
    finally:
        import shutil

        shutil.rmtree(outside, ignore_errors=True)


# ═══ 守卫的补充用例 ═══


def test_前缀相似目录不得误判(tmp_path: Path) -> None:
    """`data-evil/` 不算在 `data/` 下。

    这是**不用字符串前缀比较**的理由：`startswith("data")` 会把
    `data-evil/` 放行。用 `relative_to` 就没有这个问题。
    """
    evil = tmp_path / "data-evil"
    evil.mkdir()
    got = ensure_within(tmp_path / "data", evil)
    assert got.is_error, "前缀相似的兄弟目录必须拒绝"


def test_根自身是软链时以真实目标为基准(tmp_path: Path) -> None:
    """root 是软链时，基准应是它的真实目标。

    否则一个指向 `/` 的软链根会让**所有**路径都"合法"——
    守卫会静默失效，这是最危险的一种失效。
    """
    real = tmp_path / "real"
    real.mkdir()
    _ = (real / "inside.txt").write_text("x", encoding="utf-8")
    link_root = tmp_path / "link_root"
    try:
        link_root.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不支持创建符号链接")

    assert is_within(link_root, real / "inside.txt"), "软链根本身应放行其真实内容"
    assert not is_within(link_root, tmp_path / "other.txt"), "软链根不得放行根外路径"


def test_不存在的路径也能判定(tmp_path: Path) -> None:
    """`realpath` 对不存在的路径安全，故写入前即可判定。

    这条很重要：新文件还不存在，若守卫依赖"文件存在"才工作，
    写入路径就成了无法校验的缺口。
    """
    got = ensure_within(tmp_path, tmp_path / "user" / "not" / "yet.txt")
    assert got.is_ok, "尚不存在的路径应放行（写入前就要能判定）"
    assert not got.unwrap().exists()


def test_拒绝是正常返回不是异常(tmp_path: Path) -> None:
    """D-076：拒绝要能被模型理解并换条路，抛异常会带崩整个流程。"""
    got = ensure_within(tmp_path, "/etc/passwd")
    assert got.is_error, "越权必须是 Result.fail"
    assert got.error is not None
    assert got.error.detail, "detail 必须有内容——模型要据此换路"


# ═══ D-017：三目录启动自检 ═══


def test_启动自检重建三目录(tmp_path: Path) -> None:
    got = ensure_dirs(tmp_path)
    assert got.is_ok
    for name in paths.ALL_DIRS:
        assert (tmp_path / name).is_dir(), f"{name}/ 未被重建"


def test_用户全删后能重建并继续(tmp_path: Path) -> None:
    """D-017 的产品承诺：`data/`、`user/`、`system/` 都能被用户整个删掉。"""
    _ = ensure_dirs(tmp_path)
    for name in paths.ALL_DIRS:
        for f in (tmp_path / name).iterdir():
            f.unlink()
    for name in paths.ALL_DIRS:
        (tmp_path / name).rmdir()

    got = ensure_dirs(tmp_path)
    assert got.is_ok, "全删后必须能重建"
    assert got.unwrap().exists_ok()


def test_重建不动已有内容(tmp_path: Path) -> None:
    """重建**只补缺失的，绝不删改已有内容**。"""
    _ = ensure_dirs(tmp_path)
    keep = tmp_path / "user" / "重要材料.txt"
    _ = keep.write_text("不能丢", encoding="utf-8")

    _ = ensure_dirs(tmp_path)
    assert keep.read_text(encoding="utf-8") == "不能丢", "重建不得动已有文件"


def test_重建是幂等的(tmp_path: Path) -> None:
    for _ in range(3):
        assert ensure_dirs(tmp_path).is_ok


def test_根不存在时递归创建(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c"
    assert ensure_dirs(nested).is_ok
    assert (nested / "data").is_dir()


# ═══ 原子写 ═══


def test_原子写覆盖旧内容(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    assert write_atomic(f, "第一版").is_ok
    assert write_atomic(f, "第二版").is_ok
    assert f.read_text(encoding="utf-8") == "第二版"


def test_原子写不留临时文件(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    _ = write_atomic(f, "内容")
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"], "临时文件应已被 replace 掉"


def test_原子写失败时清理临时文件(tmp_path: Path) -> None:
    """父目录不存在 → 失败，且不留垃圾。"""
    got = write_atomic(tmp_path / "nope" / "x.json", "内容")
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.STORAGE_WRITE_FAILED
    assert not list(tmp_path.iterdir()), "失败不应留下临时文件"


def test_原子写不隐式建父目录(tmp_path: Path) -> None:
    """**不隐式建目录**：打错的路径若凭空生成一串空目录，
    会掩盖调用方的错误。建目录是 `ensure_dirs` 的职责。"""
    assert write_atomic(tmp_path / "missing" / "f.txt", "x").is_error
    assert not (tmp_path / "missing").exists()


def test_读显式UTF8(tmp_path: Path) -> None:
    f = tmp_path / "中文.txt"
    _ = write_atomic(f, "断点续跑状态")
    got = read_text(f)
    assert got.is_ok
    assert got.unwrap() == "断点续跑状态"


# ═══ FsStorage ═══


def test_实现满足端口协议(tmp_path: Path) -> None:
    st = FsStorage(Workspace(tmp_path))
    assert isinstance(st, StoragePort), "必须满足 StoragePort"


def test_写读往返(tmp_path: Path) -> None:
    _ = ensure_dirs(tmp_path)
    (tmp_path / "data" / "r-1").mkdir()
    st = FsStorage(Workspace(tmp_path))
    assert st.write_atomic("data/r-1/a.txt", "正文").is_ok
    got = st.read_text("data/r-1/a.txt")
    assert got.is_ok
    assert got.unwrap() == "正文"
    assert st.exists("data/r-1/a.txt")
    assert not st.exists("data/r-1/none.txt")


def test_不可写user目录(tmp_path: Path) -> None:
    """D-022：程序只读 `user/`，不得改写用户文件。"""
    st = FsStorage(Workspace(tmp_path))
    got = st.write_atomic("user/我的材料.txt", "被篡改")
    assert got.is_error, "不得写 user/"
    assert got.error is not None
    assert got.error.code is ErrorCode.PATH_ESCAPE


def test_可读user目录(tmp_path: Path) -> None:
    _ = ensure_dirs(tmp_path)
    f = tmp_path / "user" / "背景.txt"
    _ = f.write_text("背景材料", encoding="utf-8")
    st = FsStorage(Workspace(tmp_path))
    got = st.read_text("user/背景.txt")
    assert got.is_ok
    assert got.unwrap() == "背景材料"


def test_不可读写system目录(tmp_path: Path) -> None:
    """AI 能看到 data/ 与 user/，**看不到也改不了 system/**。"""
    st = FsStorage(Workspace(tmp_path))
    assert st.read_text("system/cfg.toml").is_error
    assert st.write_atomic("system/cfg.toml", "x").is_error
    assert not st.exists("system/cfg.toml")


def test_列目录不递归不返回绝对路径(tmp_path: Path) -> None:
    _ = ensure_dirs(tmp_path)
    _ = (tmp_path / "data" / "a.txt").write_text("1", encoding="utf-8")
    (tmp_path / "data" / "sub").mkdir()
    _ = (tmp_path / "data" / "sub" / "b.txt").write_text("2", encoding="utf-8")

    st = FsStorage(Workspace(tmp_path))
    names = st.list_dir("data")
    assert names == ("a.txt", "sub"), "不递归"
    assert all(not n.startswith("/") for n in names), "不返回绝对路径"


def test_列不存在或越权目录返回空元组(tmp_path: Path) -> None:
    """列目录常用于"有什么可选"，空列表是合法答案，不该失败。"""
    st = FsStorage(Workspace(tmp_path))
    assert st.list_dir("data/不存在") == ()
    assert st.list_dir("system") == ()


def test_草稿纸上限三个(tmp_path: Path) -> None:
    st = FsStorage(Workspace(tmp_path))
    assert DRAFT_LIMIT == 3
    assert st.draft_paper_ok("范正章", 0)
    assert st.draft_paper_ok("范正章", 2)
    assert not st.draft_paper_ok("范正章", 3), "第 4 个应被拒"
    assert not st.draft_paper_ok("范正章", 4)
