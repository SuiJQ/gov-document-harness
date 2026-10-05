"""咨询档首跑暴露的 5 个真缺陷的判据。

## 为什么单独一个文件

这 5 条里**有 4 条在 1905 条判据全绿的情况下就已经存在**。
不是判据写得不够多，是**判据根本没看那个地方**：

- 产物目录：判据查的是 `FakeStorage.files`（`write_atomic` 的账），
  而产物根本不走那条路——查了一个空账
- 工具清单：提示词侧与 API 侧各自测都绿，**没人在两层之间对账**
- DebugProjection 字段：判据只测 `StandardProjection`
- 咨询档免方向锁：声明了 `requires_user_questions=False`，
  没有任何判据查过它有没有被消费

故本文件每一类都先写**退化测试**（把修复删掉，看判据会不会红），
再写真判据。写完立刻证伪一次。
"""

from __future__ import annotations

import inspect
from dataclasses import fields as dc_fields

import pytest

from zhengtong.adapters.fakes import FakeStorage
from zhengtong.adapters.tui import ZhengTongApp
from zhengtong.domain.artifact import ArtifactKind, ArtifactRef
from zhengtong.domain.event import Event, EventKind, Level
from zhengtong.domain.ids import ArtifactId, RunId
from zhengtong.domain.phase import PhaseId
from zhengtong.domain.role import RoleId
from zhengtong.domain.tier import TIER_BY_ID, TierId
from zhengtong.kernel.projection import DebugProjection, StandardProjection
from zhengtong.orchestration import 咨询序列
from zhengtong.orchestration.orchestrate import _工具声明, _本档可调工具
from zhengtong.presentation.tui import 事件桥接


def _事件(kind: EventKind, data: dict[str, object]) -> Event:
    """构造一个最小事件。字段照抄 `test_projection.py` 的 `_ev`。"""
    return Event(
        seq=1,
        kind=kind,
        run_id=RunId("r-缺陷判据"),
        phase=PhaseId.INTAKE,
        level=Level.INFO,
        data=data,
    )


# ═══ 1 产物目录 ═══════════════════════════════════════════════


class Test产物目录由程序创建:
    """`data/<run-id>/` 必须由程序自己创建（D-127 补漏）。

    真实故障：咨询档第一次完整运行，`intake.md` 报
    `Errno 2 No such file or directory`，**每一跑必死**，
    而退出码是 0。根因是编排器走公开 `write_atomic`
    （有意不建目录），建目录的 `_写产物` 只有 `put_artifact` 用。
    """

    def test_编排器写产物走会建目录的那条路(self) -> None:
        import zhengtong.orchestration.orchestrate as 编排模块

        源 = inspect.getsource(编排模块)
        assert "storage.put_artifact" in 源, "编排器没走 put_artifact"
        # **不许再直接调 write_atomic 写产物**：那个方法不建父目录
        直调 = [行 for 行 in 源.splitlines() if "storage.write_atomic" in 行]
        assert not 直调, f"编排器仍在直调不建目录的 write_atomic：{直调}"

    def test_全新运行产物真落盘(self) -> None:
        """真适配器 + 真实文件系统的判据。假实现测不出目录问题。"""
        import shutil
        import tempfile
        from pathlib import Path

        from zhengtong.adapters.fakes import FakeLlm
        from zhengtong.adapters.fs_storage import FsStorage
        from zhengtong.adapters.paths import Workspace, ensure_dirs
        from zhengtong.domain.ids import RunId
        from zhengtong.orchestration.orchestrate import 编排器

        根 = Path(tempfile.mkdtemp(prefix="落盘判据-"))
        try:
            ensure_dirs(根)
            仓 = FsStorage(Workspace(根))
            编 = 编排器(llm=FakeLlm(), storage=仓, 运行号=RunId("r-0001"), 模型名="假模型")
            结果 = 编.跑到终(档位=TierId.CONSULT, 需求="咨询老旧小区改造")
            assert 结果.is_ok, f"全新运行失败：{结果.error}"
            落盘 = sorted(p.name for p in (根 / "data" / "r-0001").glob("*"))
            assert 落盘, "data/r-0001 下一个文件都没有"
            assert (根 / "data" / "r-0001" / "final.md").is_file(), f"终稿没落盘，现有：{落盘}"
        finally:
            shutil.rmtree(根, ignore_errors=True)


# ═══ 2 工具清单与 API 同源 ═══════════════════════════════════


class Test工具清单与API同源:
    """提示词里的工具清单与 API 实发必须一致（D-233 补漏）。

    真实故障：`_工具清单` 是一个 return 字面量，6 个工具，
    其中 `kb.read` **根本不存在**（真名 `kb.read_pack` /
    `kb.read_user_file`）；而 API 实发 17 个。模型要么不敢用工具，
    要么去调不存在的工具。
    """

    def test_清单里的每个工具都真的存在(self) -> None:
        名 = {t.name for t in _本档可调工具(RoleId.TEAM_LEAD, TierId.PRO)}
        assert "kb.read" not in 名, "清单里仍列着不存在的 kb.read"
        assert "kb.read_pack" in 名

    @pytest.mark.parametrize("角色", list(RoleId))
    @pytest.mark.parametrize("档", list(TierId))
    def test_提示词清单与API声明逐字一致(self, 角色: RoleId, 档: TierId) -> None:
        """**两侧同源**——不是「碰巧一样」，是构造方式保证一样。

        真实故障的根源就是两侧各有来源：提示词侧是字面量，
        API 侧查白名单，两者可以各改各的而没人发现。
        """
        声明 = [t.name for t in _工具声明(角色, 档)]
        清单 = [t.name for t in _本档可调工具(角色, 档)]
        assert 声明 == 清单, f"{角色}/{档} 两侧不一致"

    @pytest.mark.parametrize("角色", list(RoleId))
    def test_清单不小于三个(self, 角色: RoleId) -> None:
        """防退化：别把清单写成常量 6 个就过。"""
        assert len(_本档可调工具(角色, TierId.PRO)) >= 10, (
            f"{角色} 的工具清单只剩 {len(_本档可调工具(角色, TierId.PRO))} 个，像是退回了硬编码"
        )

    def test_清单函数体里没有字面量(self) -> None:
        """**直接掐死「改回 return 字面量」这条路**。"""
        from zhengtong.orchestration.orchestrate import _工具清单

        # **只取真正 return 的代码行**：文档字符串里提到 "return 字面量"
        # 那句话本身含 return，得排除掉——否则判据会红在注释上。
        体 = inspect.getsource(_工具清单)
        分行 = [行.strip() for 行 in 体.splitlines() if 行.strip().startswith("return")]
        assert 分行, "取不到 _工具清单 的 return 行——判据本身失效了"
        assert all("join" in 行 or "_本档可调工具" in 行 for 行 in 分行), (
            f"_工具清单 里有硬编码 return：{分行}"
        )


# ═══ 4b 退出码要真走到进程 ═══════════════════════════════════


class Test退出码真能到达进程:
    """`tui_app.启动` 返回了非零，`main` 会不会如实传出去？

    ## 为什么这条单独列

    修缺陷 4 时我改了 `启动` 的返回值，而 `cli/main.py:_进tui`
    是 `return 启动(...)`——**看起来是通的**。

    但这类「看起来通」的接线此前已经栽过：
    `启动` 恒返回 0，而 `main` 老老实实往上传，
    于是缺陷 4 的根因根本不在返回值上，在**返回值永远是 0**。
    改对一处不等于链路通，故这里从 `main` 那一端验。
    """

    def test_main原样传出启动的退出码(self) -> None:
        from zhengtong.cli.main import _进tui

        源 = inspect.getsource(_进tui)
        assert "return 启动(" in 源, "main 没把启动的退出码往上传"

    def test退出码约定里有1(self) -> None:
        """约定写明 1＝运行失败。文档与实现必须对上。"""
        from zhengtong.cli.main import main as _主入口

        文档 = inspect.getdoc(_主入口) or ""
        assert "运行失败" in 文档, "main 的退出码约定里没有「运行失败」这一档"


# ═══ 3 DebugProjection 字段 ══════════════════════════════════


class Test两种投影字段一致:
    """`DebugProjection` 此前缺 7 个字段，`--debug` 一跑就崩。

    真实故障：`AttributeError: 'DebugProjection' object has no
    attribute 'errors'` / `'artifacts'`。**调试模式完全不可用**，
    而 1905 条判据全绿——`test_projection.py` 只测
    `StandardProjection` 的字段，没人碰过调试模式这一侧。
    """

    @pytest.mark.parametrize("字段", [f.name for f in dc_fields(StandardProjection)])
    def test_调试投影有该字段(self, 字段: str) -> None:
        assert hasattr(DebugProjection(), 字段), f"DebugProjection 缺 {字段}"

    def test_调试投影字段是标准投影的超集(self) -> None:
        标 = {f.name for f in dc_fields(StandardProjection)}
        调 = {f.name for f in dc_fields(DebugProjection)}
        assert 标 <= 调, f"调试投影缺：{sorted(标 - 调)}"

    def test_调试模式能收成果事件(self) -> None:
        """字段在还得**真被填**——有字段但恒为空，界面照样空着。"""
        p = DebugProjection()
        p.accept(
            _事件(EventKind.ARTIFACT_READY, {"name": "写作方向确认单", "content": "正文一二三"})
        )
        assert p.artifacts, "调试模式收成果事件却没填 artifacts——界面成果卡会空"
        assert p.records, "调试模式该同时留下全量记录行"

    def test_调试模式仍全量透传(self) -> None:
        """继承之后**不能把「全量透传」这个本职丢了**。"""
        p = DebugProjection()
        for kind in EventKind:
            p.accept(_事件(kind, {"x": 1}))
        assert len(p.records) == len(list(EventKind)), "调试模式漏了某些事件"


# ═══ 4 退出码 ═══════════════════════════════════════════════


class Test退出码反映失败:
    """失败必须让退出码非零。

    真实故障：intake 落盘失败 + 界面连崩两处 AttributeError，
    `returncode` 仍是 **0**。用户和脚本都以为跑通了。
    """

    def test_桥接记下失败(self) -> None:
        桥 = 事件桥接()
        assert not 桥.末次失败, "刚建好就带失败——默认值反了"

    def test_编排失败会写进末次失败(self) -> None:
        """编排返回错误时，桥接必须记下来给退出码用。"""
        from zhengtong.domain.errors import Result

        桥 = 事件桥接()

        class _失败的编排:
            #: 协议 `可跑编排` 要求的两项之一。**原替身没有这个属性**，
            #: 桥接靠 `getattr(编排器, "事件", [])` 兜底才通过——
            #: 而兜底正是本轮要清掉的写法。替身不完整被兜底遮住，
            #: 是同一个病：接线缺了也不报。
            事件: list[Event]

            def __init__(self) -> None:
                self.事件 = []

            def 跑到终(self, *, 档位: TierId, 需求: str) -> Result[None]:
                from zhengtong.domain.errors import ErrorCode

                return Result.fail(ErrorCode.STORAGE_WRITE_FAILED, "落盘失败")

        桥.在worker里跑(_失败的编排(), 档位=TierId.CONSULT, 需求="x")
        assert 桥.末次失败 == "落盘失败", "编排失败了却没记——退出码会报成功"

    def test_界面把失败转成退出码(self) -> None:
        """界面读得到失败（`启动` 据此返回非零）。"""
        桥 = 事件桥接()
        桥.末次失败 = "出事了"
        界面 = ZhengTongApp(桥=桥, 根=None, 取编排器=None, 闸=None)  # type: ignore[arg-type]
        assert 界面.运行失败 == "出事了", "界面读不到失败，退出码会恒为 0"


# ═══ 5 requires_user_questions 真被消费 ═══════════════════════


class Test档位提问豁免真被消费:
    """`requires_user_questions=False` 此前**零消费**。

    真实故障：声明咨询档免提问，却照旧把 `flow.ask_user`
    发给模型，「免方向锁」只存在于 `team-lead.md` 里的一句
    自然语言，靠模型自觉。字段写了、没人读——又一个死代码。
    """

    def test_咨询档不发提问工具(self) -> None:
        名 = {t.name for t in _本档可调工具(RoleId.TEAM_LEAD, TierId.CONSULT)}
        assert "flow.ask_user" not in 名, "咨询档仍在发提问工具——豁免没兑现"

    @pytest.mark.parametrize("档", [TierId.FLASH, TierId.PRO])
    def test_成文档照发提问工具(self, 档: TierId) -> None:
        名 = {t.name for t in _本档可调工具(RoleId.TEAM_LEAD, 档)}
        assert "flow.ask_user" in 名, f"{档} 该问却没发提问工具"

    def test_豁免不是把工具全砍光(self) -> None:
        """别把「咨询档能力更少」做成「什么工具都不给」。"""
        咨询 = {t.name for t in _本档可调工具(RoleId.TEAM_LEAD, TierId.CONSULT)}
        闪 = {t.name for t in _本档可调工具(RoleId.TEAM_LEAD, TierId.FLASH)}
        assert 咨询 < 闪, "咨询档工具应是成文档的真子集"
        assert len(咨询) >= 10, f"咨询档工具过少：{咨询}"
        assert "kb.search" in 咨询 and "shared.write" in 咨询, "检索与公共区必须留"

    def test_咨询档不发成文与门禁工具(self) -> None:
        """**D-384**：咨询档不写稿、门禁不适用。

        早先档位只过滤了「提问工具」一个，于是 `draft.submit`
        与 `validate.run` 照样发给主理人——模型看得到、调得动，
        却永远用不上。被调用时工具层会拒绝，用户在调试模式里
        看到的是「模型说能调用却调不动」。
        """
        咨询 = {t.name for t in _本档可调工具(RoleId.TEAM_LEAD, TierId.CONSULT)}
        assert "draft.submit" not in 咨询, "咨询档不产出成文，不该发提交正文的工具"
        assert "validate.run" not in 咨询, "咨询档门禁不适用，不该发校验器工具"
        assert "flow.ask_user" not in 咨询, "咨询档免提问"

    def test_成文档两个成文工具都在(self) -> None:
        """反过来也锁死：Flash/Pro 是成文档，成文与门禁工具不能缺。"""
        for 档 in (TierId.FLASH, TierId.PRO):
            工具 = {t.name for t in _本档可调工具(RoleId.TEAM_LEAD, 档)}
            assert {"draft.submit", "validate.run", "flow.ask_user"} <= 工具, f"{档} 缺成文工具"


class Test咨询档阶段线符合架构:
    """架构 §8.4：`TierSpec(roles=["范正章"], phases=[ADVICE])`。

    此前咨询序列是**成文序列删掉 GATING**——于是咨询档照样走
    DIRECTION（方向确认）、OUTLINE（大纲）、DRAFTING（起草）。
    实测第一跑就死在 DIRECTION 的硬约束校验上：咨询档按定义
    没有 D-Card，校验必然不通过——**从设计上就跑不完**。
    """

    def test_咨询档不走方向确认(self) -> None:
        from zhengtong.domain.phase import PhaseId

        assert PhaseId.DIRECTION not in 咨询序列, "咨询档不该有 DIRECTION"

    def test_咨询档不走成文段(self) -> None:
        from zhengtong.domain.phase import PhaseId

        for 段 in (PhaseId.OUTLINE, PhaseId.DRAFTING, PhaseId.GATING):
            assert 段 not in 咨询序列, f"咨询档不该走 {段.value}"

    def test_咨询档仍要交付(self) -> None:
        """减段不等于不交付——`FINAL` 必须留着，交付物从那儿取。"""
        from zhengtong.domain.phase import PhaseId

        assert PhaseId.FINAL in 咨询序列, "咨询档没有 FINAL 段，交付物无处可取"
        assert 咨询序列[-1].value == "done", "咨询序列必须以 DONE 收尾"

    def test_咨询档免门禁是真的(self) -> None:
        assert not TIER_BY_ID[TierId.CONSULT].gates_apply


# ═══ 5b 假实现不许比真实现宽松（D-112） ═══════════════════════


class TestFakeStorage产物账与真实现一致:
    """`FakeStorage.put_artifact` 原先存 `str(ref)` 当键。

    后果不只是键难看：**产物目录建没建、路径守不守，
    在假实现上完全测不出来**。而这正是本次真实缺陷的藏身处。

    判据原来查 `仓.files`（`write_atomic` 的账），
    产物根本不走那条路——**查了一个空账**。
    """

    def test_键是ref的path(self) -> None:
        仓 = FakeStorage()
        ref = ArtifactRef(
            id=ArtifactId("r-0001:final.md"),
            kind=ArtifactKind.NOTE,
            path="r-0001/final.md",
        )
        仓.put_artifact(ref, "内容")
        assert 仓.artifacts == {"r-0001/final.md": "内容"}, f"键不是 ref.path：{list(仓.artifacts)}"

    def test_越权路径被拒(self) -> None:
        """假实现必须和真实现在同一输入上给同一答案（D-112）。

        真实现实测：`put_artifact(path="../逃逸.txt")` →
        `PATH_ESCAPE`（守卫拦下）。假实现原先存 `str(ref)` 当键，
        **连路径都不看**，比真实实现宽松。
        """
        from zhengtong.domain.errors import ErrorCode

        仓 = FakeStorage()
        ref = ArtifactRef(id=ArtifactId("x"), kind=ArtifactKind.NOTE, path="../逃逸.txt")
        got = 仓.put_artifact(ref, "内容")
        assert got.is_error, "越权路径照收——假实现比真实现宽松"
        assert got.error is not None
        assert got.error.code is ErrorCode.PATH_ESCAPE

    def test_ref_path是相对data的(self) -> None:
        """`ref.path` 相对 `data/<run-id>/`，故任意名字都在 `data/` 下。

        真实现 `put_artifact` 内部拼 `data/<ref.path>` 再查权限，
        所以 `path="user/x.txt"` 落在 `data/user/x.txt`——**合法**。
        判据要写对，否则会去禁一个真实现允许的输入。
        """
        仓 = FakeStorage()
        ref = ArtifactRef(id=ArtifactId("y"), kind=ArtifactKind.NOTE, path="user/x.txt")
        got = 仓.put_artifact(ref, "内容")
        assert got.is_ok, f"ref.path 相对 data/，不该被拒：{got.error}"

    def test_判据查的是产物账不是files账(self) -> None:
        """防退化：别把判据改回去查 `files`。"""
        import zhengtong.orchestration.orchestrate as 编排模块

        assert "put_artifact" in inspect.getsource(编排模块)
