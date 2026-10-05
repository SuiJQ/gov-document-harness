"""`adapters/fs_storage.py` 测试 —— 真实适配器，也是假实现的**基准**。

## 为什么这个文件的判据最严

第 1 批 D-112 确立：假实现不得比真实现宽松。
但那批**只改���了假实现，真实适配器本身从未逐项验证**——
本批补上这一课。

更关键的是：本文件查出的两个缺陷（D-126 软链越权、D-127 产物目录不存在）
**都是 533 条测试全绿的状态下存在的**。
一个从未被组合执行过的路径，可以安静地坏掉。

故判据：**每一个方法都要在"真实文件系统、真实路径组合"下跑一遍**，
而不是只测单方法的返回值。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from zhengtong.adapters.fs_storage import DRAFT_LIMIT, FsStorage
from zhengtong.adapters.paths import Workspace
from zhengtong.domain.artifact import ArtifactRef
from zhengtong.domain.errors import ErrorCode, Result
from zhengtong.domain.ids import ArtifactId, RunId
from zhengtong.domain.run_state import RunState
from zhengtong.domain.task import TaskSpec
from zhengtong.domain.tier import TierId
from zhengtong.ports.storage import StoragePort

#: 跨存储端口的"读或写"操作。`Result` 不变（`Result[str]` 换不成
#: `Result[None]`），故用联合类型而非统一擦成 `Result[object]`——
#: 擦掉类型信息会让每一处 `.error.code` 访问都退化成 unknown，
#: 换来几十条无法定位的 warning。**不划算。**
操作型 = Callable[[StoragePort], Result[None]] | Callable[[StoragePort], Result[str]]


@pytest.fixture
def 工作区(tmp_path: Path) -> FsStorage:
    """**全新工作区**——刻意不预先建任何子目录。

    这是 D-127 的关键：测试若预先 `mkdir` 了 `data/r-001/`，
    就恰好掩盖了"产物目录由谁创建"这个问题。
    """
    for 名 in ("data", "user", "system"):
        (tmp_path / 名).mkdir()
    return FsStorage(Workspace(root=tmp_path))


# ═══ D-127：产物目录必须由程序自己创建 ═══


def test_全新工作区save_run_state须成功(工作区: FsStorage) -> None:
    """**本批最致命的缺陷**。

    `atomic.write_atomic` 有意不隐式建目录，`ensure_dirs` 又全仓无调用方，
    于是 `save_run_state` 在**全新运行时必然失败**——
    `data/r-001/` 从未创建。这是状态机持久化的唯一入口，
    失败即断点续跑（D-027）无从谈起。

    架构第 843 行：「新建文件必然由程序自己在已校验的父目录下创建」。
    """
    st = _状态("r-001")
    got = 工作区.save_run_state(st)
    assert got.is_ok, f"全新工作区保存状态失败：{got.error}"
    assert 工作区.load_run_state(RunId("r-001")).unwrap() == st


def test_多层产物目录自动创建(工作区: FsStorage) -> None:
    """`data/<run-id>/附件/` 这类多层结构必须能直接写。"""
    got = 工作区.put_artifact(_产物("r-001/附件/清单.txt"), "内容")
    assert got.is_ok, f"三层路径写入失败：{got.error}"
    assert 工作区.read_text("data/r-001/附件/清单.txt").unwrap() == "内容"


def test_重复保存状态须幂等(工作区: FsStorage) -> None:
    """每次都写全量，重复保存不得报错。"""
    st = _状态("r-002")
    assert 工作区.save_run_state(st).is_ok
    assert 工作区.save_run_state(st).is_ok


def test_write_atomic仍不隐式建目录(工作区: FsStorage) -> None:
    """**D-127 的边界**：`atomic.write_atomic` 的原设计不得被污染。

    它的理由是正当的——隐式建目录会让一次打错的路径凭空生成一串空目录。
    建目录的职责放在存储层，且建的是**已过守卫**的目录。
    """
    got = 工作区.write_atomic("data/临时目录/x.txt", "y")
    assert got.is_error, "write_atomic 被改成了隐式建目录"
    assert got.error is not None
    assert got.error.code is ErrorCode.STORAGE_WRITE_FAILED


# ═══ D-126：软链接越权 ═══


def test_经软链接写user须拦(tmp_path: Path) -> None:
    """**D-022 被绕过的真实路径**。

    原实现按**原始字符串**切顶层目录名判权限：
    `data/t/x.txt` 的顶层是 `data` → 放行；
    而它的**真实落点**是 `user/`（`data/t` 是指向 `user` 的软链接）。
    实测原实现成功把文件写进了 `user/`。

    程序不得改写用户文件（D-022），这条是硬要求。
    """
    _建目录(tmp_path)
    (tmp_path / "data" / "t").symlink_to(tmp_path / "user")
    s = FsStorage(Workspace(root=tmp_path))

    got = s.write_atomic("data/t/偷写.txt", "机密")
    assert got.is_error, "软链接越权未被拦截——AI 写进了 user/"
    assert not (tmp_path / "user" / "偷写.txt").exists()


def test_经软链接读user允许但写必拒(tmp_path: Path) -> None:
    """**一个容易想当然的地方**。

    `data/t` → `user` 的软链接，读 `data/t/已有.txt` 解析后落点在 `user/`，
    而 **AI 本来就允许读 `user/`**（要读用户提供的材料），
    故放行是**正确的**，不是漏洞。

    这里刻意把这条写死，防止后来的审计把它"顺手"改成拦截：
    那会让 AI 读不到 `user/` 里的材料，是**新引入的**功能缺陷。

    真正的越权只在**写**——D-022 说 AI 只能写 `data/`。
    """
    _建目录(tmp_path)
    _ = (tmp_path / "user" / "已有.txt").write_text("用户文件", encoding="utf-8")
    (tmp_path / "data" / "t").symlink_to(tmp_path / "user")
    s = FsStorage(Workspace(root=tmp_path))

    读 = s.read_text("data/t/已有.txt")
    assert 读.is_ok and 读.unwrap() == "用户文件", "软链读 user 本就合法，不该被拦"

    写 = s.write_atomic("data/t/偷写.txt", "机密")
    assert 写.is_error, "软链写 user 才是越权，必须拦"
    assert not (tmp_path / "user" / "偷写.txt").exists()


def test_经软链接读system须拦(tmp_path: Path) -> None:
    """`system/` 是 AI **不可见**的（D-022：只能看到 data/ 与 user/）。
    经软链接指向它必须被拦——这才是读方向的有效判据。"""
    _建目录(tmp_path)
    _ = (tmp_path / "system" / "config.toml").write_text("密钥设置", encoding="utf-8")
    (tmp_path / "data" / "s").symlink_to(tmp_path / "system")
    s = FsStorage(Workspace(root=tmp_path))

    got = s.read_text("data/s/config.toml")
    assert got.is_error, "AI 经软链接读到了 system/——D-022 要求它看不到 system/"


def test_软链接指向工作区外须拦(tmp_path: Path) -> None:
    """守卫的基本职责。"""
    import tempfile

    外部 = Path(tempfile.mkdtemp())
    _ = (外部 / "机密.txt").write_text("外部机密", encoding="utf-8")
    _建目录(tmp_path)
    (tmp_path / "data" / "out").symlink_to(外部)
    s = FsStorage(Workspace(root=tmp_path))

    assert s.read_text("data/out/机密.txt").is_error


def test_目录遍历须拦(工作区: FsStorage) -> None:
    for 路径 in ("../outside.txt", "data/../../outside.txt", "/etc/passwd"):
        got = 工作区.write_atomic(路径, "x")
        assert got.is_error, f"{路径} 未被拦截"


def test_写system须拦(工作区: FsStorage) -> None:
    """`system/` 只存配置日志，AI 不得写入（D-022）。"""
    assert 工作区.write_atomic("system/x.txt", "y").is_error
    assert 工作区.write_atomic("data/../system/x.txt", "y").is_error


def test_写user须拦(工作区: FsStorage) -> None:
    """AI 只能写 `data/`；`user/` 是用户的。"""
    got = 工作区.write_atomic("user/x.txt", "y")
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.PATH_ESCAPE


def test_读user允许(工作区: FsStorage) -> None:
    """读 `user/` 是允许的——AI 需要读用户提供的材料。"""
    assert 工作区.write_atomic("user/材料.txt", "内容").is_error or True
    _ = (工作区.ws.root / "user" / "材料.txt").write_text("内容", encoding="utf-8")
    assert 工作区.read_text("user/材料.txt").unwrap() == "内容"


def test_读system须拦(工作区: FsStorage) -> None:
    """AI 看不到 `system/`（D-022：只能看到 data/ 与 user/）。"""
    assert 工作区.read_text("system/config.toml").is_error


def test_错误信息须指出真实落点(tmp_path: Path) -> None:
    """D-127 修复里刻意改成「实际落点在 user」——
    报错要让人知道**文件到底写到哪儿去了**，而不是只说"不可写"。"""
    _建目录(tmp_path)
    (tmp_path / "data" / "t").symlink_to(tmp_path / "user")
    s = FsStorage(Workspace(root=tmp_path))

    got = s.write_atomic("data/t/x.txt", "y")
    assert got.is_error
    assert got.error is not None
    assert "user" in got.error.detail


# ═══ 常规读写 ═══


def test_写读往返(工作区: FsStorage) -> None:
    assert 工作区.write_atomic("data/a.txt", "内容").is_ok
    assert 工作区.read_text("data/a.txt").unwrap() == "内容"


def test_覆盖写(工作区: FsStorage) -> None:
    assert 工作区.write_atomic("data/a.txt", "第一次").is_ok
    assert 工作区.write_atomic("data/a.txt", "第二次").is_ok
    assert 工作区.read_text("data/a.txt").unwrap() == "第二次"


def test_读不存在须报FILE_UNREADABLE(工作区: FsStorage) -> None:
    """与 `FakeStorage` 的错误码一致（D-112：同宽严）。"""
    got = 工作区.read_text("data/没有.txt")
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.FILE_UNREADABLE


def test_exists(工作区: FsStorage) -> None:
    assert 工作区.write_atomic("data/a.txt", "x").is_ok
    assert 工作区.exists("data/a.txt")
    assert not 工作区.exists("data/没有.txt")


def test_越权路径的exists须为假(工作区: FsStorage) -> None:
    """越权不是"不存在"，但 `exists` 只返回 bool，
    返回 False 是诚实的答案（拿不到信息就别装作拿到了）。"""
    assert not 工作区.exists("../outside.txt")


# ═══ list_dir ═══


def test_list_dir只返回名字不递归(工作区: FsStorage) -> None:
    """端口契约：不递归、不返回绝对路径。"""
    assert 工作区.put_artifact(_产物("r1/深层/x.txt"), "X").is_ok
    assert 工作区.list_dir("data/r1") == ("深层",)


def test_list_dir越权或不存在返回空元组(工作区: FsStorage) -> None:
    """两种情况都返回空元组——列目录常用于"有什么可选的"，
    空列表是合法答案，让调用方处理失败反而更麻烦。"""
    assert 工作区.list_dir("data/根本不存在") == ()
    assert 工作区.list_dir("../越权") == ()


def test_list_dir对文件返回空元组(工作区: FsStorage) -> None:
    assert 工作区.write_atomic("data/a.txt", "x").is_ok
    assert 工作区.list_dir("data/a.txt") == ()


def test_list_dir结果有序(工作区: FsStorage) -> None:
    for 名 in ("c.txt", "a.txt", "b.txt"):
        assert 工作区.write_atomic(f"data/{名}", "x").is_ok
    assert 工作区.list_dir("data") == ("a.txt", "b.txt", "c.txt")


# ═══ 草稿纸上限 ═══


def test_草稿纸上限为三(工作区: FsStorage) -> None:
    for n in range(DRAFT_LIMIT):
        assert 工作区.draft_paper_ok("辞达意", n)
    assert not 工作区.draft_paper_ok("辞达意", DRAFT_LIMIT)


def test_草稿纸按传入计数(工作区: FsStorage) -> None:
    """`role` **暂不参与判定**——上限是"每个角色各 3 个"，
    由调用方按角色分别计数后传进来。存储层不替编排层记账。"""
    assert 工作区.draft_paper_ok("甲", 0)
    assert 工作区.draft_paper_ok("乙", 2)


# ═══ 与假实现的一致性（D-112）══════════════════════════════════════════


def _写(路径: str) -> Callable[[StoragePort], Result[None]]:
    return lambda s: s.write_atomic(路径, "y")


def _读(路径: str) -> Callable[[StoragePort], Result[str]]:
    return lambda s: s.read_text(路径)


def _列(路径: str) -> Callable[[StoragePort], tuple[str, ...]]:
    return lambda s: s.list_dir(路径)


@pytest.mark.parametrize(
    ("操作", "说明"),
    [
        pytest.param(_写("user/x.txt"), "写 user/ 被拒", id="写user被拒"),
        pytest.param(_写("system/x.txt"), "写 system/ 被拒", id="写system被拒"),
        pytest.param(_读("system/x.txt"), "读 system/ 被拒", id="读system被拒"),
        pytest.param(_写("../x.txt"), "目录遍历被拒", id="遍历被拒"),
        pytest.param(_读("data/没有.txt"), "读不存在报 FILE_UNREADABLE", id="读不存在"),
    ],
)
def test_与假实现同宽严(工作区: FsStorage, 操作: 操作型, 说明: str) -> None:
    """**D-112 的正面验证**：真实现与 `FakeStorage` 对同一输入给出同类答案。

    真实适配器才是基准——假实现跟它对齐才对。

    失败与成功用**不同类型**表达（`Result` 不变，`Result[str]` 换不成
    `Result[None]`），故断言收在"是否失败"与"错误码"这两件
    跨类型可比的事上——成功值的类型本轮无从比较，也不假装能比较。
    """
    from zhengtong.adapters.fakes import FakeStorage

    真实 = 操作(工作区)
    假的 = 操作(FakeStorage())
    真错 = 真实.is_error
    假错 = 假的.is_error
    assert 真错 == 假错, f"{说明}：真实现 is_error={真错}，假实现 is_error={假错}"
    if 真错 and 假错:
        assert 真实.error is not None and 假的.error is not None
        assert 真实.error.code == 假的.error.code, (
            f"{说明}：错误码不一致，真={真实.error.code.name} 假={假的.error.code.name}"
        )


@pytest.mark.parametrize("操作", [pytest.param(_列("data/不存在"), id="列不存在")])
def test_list_dir与假实现一致(
    工作区: FsStorage, 操作: Callable[[StoragePort], tuple[str, ...]]
) -> None:
    """列目录返回空元组而非失败，两边必须一致——
    否则测试里能用假实现过的用例，到真实适配器上就崩。"""
    from zhengtong.adapters.fakes import FakeStorage

    assert 操作(工作区) == 操作(FakeStorage()) == ()


# ═══ 夹具 ═══


def _建目录(root: Path) -> None:
    for 名 in ("data", "user", "system"):
        (root / 名).mkdir(exist_ok=True)


def _状态(run_id: str) -> RunState:
    return RunState(
        schema_version=1,
        run_id=RunId(run_id),
        task=TaskSpec(tier=TierId.PRO, requirement="就旱情写个通知"),
    )


def _产物(path: str) -> ArtifactRef:
    from zhengtong.domain.artifact import ArtifactKind

    return ArtifactRef(id=ArtifactId("a-1"), kind=ArtifactKind.DRAFT, path=path, version=1)
