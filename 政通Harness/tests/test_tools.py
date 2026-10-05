"""L4 工具层测试（第 10 步判据）。

判据四条：
1. 工具清单与架构 §7.4 表格逐行相符
2. **双重白名单**：不在名单里**调不动**，而不是"提示模型别用"
3. 审计留痕（含**拒绝**也留痕）
4. 草稿纸 3 文件上限（D-042）

## 判据 4 为什么用**真适配器**而不是假实现

`FakeStorage.draft_paper_ok` 直接返回 `draft_ok` 布尔标志、
**完全忽略 `current_count`**；`FsStorage` 才是 `current_count < 3`。
用假实现测这条，测的是那个标志，不是上限本身——
而上限正是这一条要守的东西（D-112：假实现比真实现宽松最危险）。

同理，D-165 的教训是「零件都测过、测试全绿，真跑起来那条路没接」。
故判据 4 直接用 `FsStorage` 落真文件。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from zhengtong.adapters import paths
from zhengtong.adapters.fakes import (
    FakeContentPack,
    FakeRetriever,
    FakeStorage,
    FakeUserFiles,
    FakeValidators,
)
from zhengtong.adapters.fs_storage import FsStorage
from zhengtong.domain.errors import ErrorCode
from zhengtong.domain.ids import RunId
from zhengtong.domain.role import ROLES, RoleId
from zhengtong.services.tools import (
    TOOL_SPECS,
    Effect,
    取工具,
    审计器,
    工具依赖,
    工具运行器,
    该角色可调,
)
from zhengtong.services.tools.handlers import 处理器表

_RUN = RunId("r-test")


# ── 夹具 ──


def _依赖(tmp_path: Path, *, 真存储: bool = False) -> tuple[工具依赖, FsStorage | None]:
    """构造依赖。`真存储=True` 时用 `FsStorage`（草稿纸判据必须走真适配器）。"""
    存储: FakeStorage | FsStorage
    真: FsStorage | None = None
    if 真存储:
        建 = paths.ensure_dirs(tmp_path)
        assert 建.is_ok, 建.error
        真 = FsStorage(ws=建.unwrap())
        存储 = 真
    else:
        存储 = FakeStorage()
    d = 工具依赖(
        storage=存储,
        user_files=FakeUserFiles(),
        retriever=FakeRetriever(),
        content_pack=FakeContentPack(),
        validators=FakeValidators(),
        run_id=_RUN,
    )
    return d, 真


def _运行器(d: 工具依赖) -> 工具运行器:
    return 工具运行器(依赖=d, 审计=审计器(时钟=lambda: 0.0))


# ═══ 判据 1：清单与架构逐行相符 ═══


class Test工具清单:
    def test_共十七个(self) -> None:
        """架构 §7.4 正文说「14 个」是**表格行数**——逐行落实是 16 个（D-190）。

        第 14.5 步新增 `tool.result`（对话序列只记工具动作不记结果，
        模型需要一条查回途径）→ 17 个。**架构表已同步**。
        """
        assert len(TOOL_SPECS) == 17

    def test_名字与架构表格一致(self) -> None:
        期望 = {
            "shared.write",
            "shared.read",
            "shared.list",
            "kb.search",
            "kb.read_pack",
            "kb.read_user_file",
            "msg.report_to_lead",
            "msg.request_from_lead",
            "draft.submit",
            "validate.run",
            "flow.status",
            "flow.sleep",
            "flow.wake_on",
            "flow.ask_user",
            "flow.rollback",
            "flow.inject_directive",
            "tool.result",
        }
        assert {s.name for s in TOOL_SPECS} == 期望

    def test_已砍除的fs工具不存在(self) -> None:
        """D-042：`fs.list`/`fs.write`/`fs.delete` **代码里不允许复活**。"""
        for 名字 in ("fs.list", "fs.write", "fs.delete"):
            assert 取工具(名字) is None, f"{名字} 已被 D-042 砍除，不该存在"

    def test_每个工具都有实现(self) -> None:
        """注册表加了工具却忘写实现 = 编码错误。"""
        for s in TOOL_SPECS:
            assert s.name in 处理器表, f"{s.name} 没有实现"

    def test_实现表没有多余项(self) -> None:
        """反向：实现表多出的工具等于没进注册表，模型永远看不到。"""
        assert set(处理器表) == {s.name for s in TOOL_SPECS}

    def test_名字无重复(self) -> None:
        名字 = [s.name for s in TOOL_SPECS]
        assert len(名字) == len(set(名字))

    def test_每个工具都有描述(self) -> None:
        """描述是模型唯一的说明书，空描述等于工具不可用。"""
        for s in TOOL_SPECS:
            assert s.description.strip(), f"{s.name} 缺描述"

    def test_每个工具都有参数schema(self) -> None:
        for s in TOOL_SPECS:
            assert s.parameters.get("type") == "object", f"{s.name} 参数 schema 不对"


# ═══ 判据 2：双重白名单 ═══


class Test角色白名单:
    def test_仅主理人工具只有四个(self) -> None:
        仅主理人 = {s.name for s in TOOL_SPECS if s.仅主理人可调}
        assert 仅主理人 == {
            "flow.status",
            "flow.ask_user",
            "flow.rollback",
            "flow.inject_directive",
        }
        # `tool.result` **不是**仅主理人工具：全体都要能查回自己那次调用的
        # 结果（D-226），把它划进主理人专属等于让成员查不回自己的东西。

    @pytest.mark.parametrize(
        "工具名",
        ["flow.status", "flow.ask_user", "flow.rollback", "flow.inject_directive"],
    )
    def test_成员调仅主理人工具被拒(self, 工具名: str, tmp_path: Path) -> None:
        """**判据本身**：调用被拒，不只是"清单里没有它"。"""
        d, _ = _依赖(tmp_path)
        运行 = _运行器(d)
        for 角色 in ROLES:
            if 角色.id is RoleId.TEAM_LEAD:
                continue
            r = 运行.调用(工具名, 角色=角色.id, 参数={"step": "s", "fields": [], "text": "t"})
            assert r.is_error, f"{角色.id.value} 竟能调用 {工具名}"
            assert r.error is not None
            assert r.error.code == ErrorCode.TOOL_NOT_ALLOWED

    def test_主理人可调多数工具(self, tmp_path: Path) -> None:
        """主理人**不是**全部工具的调用者（D-384）。

        `msg.report_to_lead` / `msg.request_from_lead` 的定义是
        「**成员**与主理人之间」的通道。主理人调它就是自己向自己说话，
        语义上不存在对应端点——咨询档只有主理人一人，
        于是这两个工具在那档**一个合法调用者都没有**，却照样发给了模型。
        本条判据原先经 `工具运行器.可调清单(role)` 取名。两点问题：

        1. 那个方法**生产零消费**，且只按角色过滤、不含档位，
           而 docstring 自称「**这就是给模型看的那份清单**」——
           名字在骗人（真正发给模型的是编排层 `_本档可调工具`，
           提示词与 API 共用同源，D-233 补漏就是为消灭那份字面量）。
           **一个自称「给模型看」却没人看的方法，比没有更糟。**
        2. 它只是 `registry.该角色可调(role)` 的多余包装。

        故判据直接用 registry 这个真源，包装方法已删除。

        注：这里**验不到**真正发给模型的那份——`_本档可调工具`
        要读真实内容包，本判据环境里没有（实测 `KeyError`）。
        「提示词与 API 同源」由内容包判据守着。
        """
        from zhengtong.services.tools.registry import 该角色可调

        名字 = {s.name for s in 该角色可调(RoleId.TEAM_LEAD)}
        assert "msg.report_to_lead" not in 名字, "主理人不该能向自己汇报"
        assert "msg.request_from_lead" not in 名字, "主理人不该能向自己请示"
        # 其余（检索、公共区、控制、成文、门禁、提问）一个不少
        assert "draft.submit" in 名字 and "kb.search" in 名字
        assert "flow.ask_user" in 名字

    def test_成员可调子集不含仅主理人工具(self) -> None:
        for 角色 in ROLES:
            if 角色.id is RoleId.TEAM_LEAD:
                continue
            清单 = {s.name for s in 该角色可调(角色.id)}
            assert "flow.ask_user" not in 清单
            assert len(清单) == 13  # 12 + tool.result（D-226，全体可调）

    def test_白名单为空意味着无人可调(self) -> None:
        """**默认方向**：空集是"没人能调"，不是"所有人都能调"。
        把空集解释成通配，会让「删掉一条白名单」变成「放开给所有人」。"""
        from zhengtong.services.tools.spec import ToolSpec

        无人 = ToolSpec(name="x.y", description="d", allowed_roles=frozenset())
        assert not 无人.可被全员调用
        assert 无人.allowed_roles == frozenset()
        assert len([r for r in ROLES if r.id in 无人.allowed_roles]) == 0

    def test_未知工具被拒且与越权分开报错(self, tmp_path: Path) -> None:
        """两个码必须分开：打错名字与越权排障方向完全不同。"""
        d, _ = _依赖(tmp_path)
        运行 = _运行器(d)
        r = 运行.调用("fs.list", 角色=RoleId.TEAM_LEAD, 参数={})
        assert r.is_error
        assert r.error is not None
        assert r.error.code == ErrorCode.TOOL_UNKNOWN


# ═══ 判据 3：审计留痕 ═══


class Test审计:
    def test_成功调用留痕(self, tmp_path: Path) -> None:
        d, _ = _依赖(tmp_path)
        审计 = 审计器(时钟=lambda: 123.0)
        运行 = 工具运行器(依赖=d, 审计=审计)
        _ = 运行.调用(
            "shared.write",
            角色=RoleId.STRUCTURE,
            参数={
                "name": "大纲",
                "kind": "outline",
                "content": "点",
            },
        )
        assert len(审计.条目) == 1
        条 = 审计.条目[0]
        assert 条.工具名 == "shared.write"
        assert 条.角色 is RoleId.STRUCTURE
        assert 条.允许
        assert 条.时间戳 == 123.0

    def test_拒绝也留痕(self, tmp_path: Path) -> None:
        """**越权尝试恰恰最该被看见**。只记成功等于让越权凭空消失。"""
        d, _ = _依赖(tmp_path)
        审计 = 审计器()
        运行 = 工具运行器(依赖=d, 审计=审计)
        _ = 运行.调用("flow.ask_user", 角色=RoleId.LANGUAGE, 参数={"fields": []})
        assert 审计.拒绝数 == 1
        assert not 审计.条目[0].允许
        assert 审计.条目[0].错误码 == str(ErrorCode.TOOL_NOT_ALLOWED)

    def test_参数不进敏感值(self, tmp_path: Path) -> None:
        """审计记录最终进 journal。记内容等于把材料正文抄进日志（D-161）。"""
        d, _ = _依赖(tmp_path)
        审计 = 审计器()
        运行 = 工具运行器(依赖=d, 审计=审计)
        机密 = "绝密内容"
        _ = 运行.调用(
            "shared.write",
            角色=RoleId.STRUCTURE,
            参数={
                "name": "n",
                "kind": "note",
                "content": 机密,
            },
        )
        assert 机密 not in 审计.条目[0].参数摘要

    def test_审计只追加(self, tmp_path: Path) -> None:
        d, _ = _依赖(tmp_path)
        审计 = 审计器()
        运行 = 工具运行器(依赖=d, 审计=审计)
        for _ in range(3):
            _ = 运行.调用("shared.list", 角色=RoleId.STRUCTURE, 参数={})
        assert len(审计.条目) == 3
        快照 = 审计.条目
        _ = 运行.调用("shared.list", 角色=RoleId.STRUCTURE, 参数={})
        assert len(快照) == 3, "已取得的快照不该被后续调用改动"

    def test_按角色可查(self, tmp_path: Path) -> None:
        d, _ = _依赖(tmp_path)
        审计 = 审计器()
        运行 = 工具运行器(依赖=d, 审计=审计)
        _ = 运行.调用("shared.list", 角色=RoleId.STRUCTURE, 参数={})
        _ = 运行.调用("shared.list", 角色=RoleId.LANGUAGE, 参数={})
        assert len(审计.该角色的条目(RoleId.STRUCTURE)) == 1

    def test_未接线的编排工具必须失败而非静默成功(self, tmp_path: Path) -> None:
        """D-165 同型：静默成功就是「测试全绿、真跑起来什么也没做」。"""
        d, _ = _依赖(tmp_path)
        运行 = _运行器(d)
        r = 运行.调用("flow.sleep", 角色=RoleId.STRUCTURE, 参数={"reason": "等条件"})
        assert r.is_error, "编排回调未接线却报告成功"

    def test_接线后行为正确(self, tmp_path: Path) -> None:
        d, _ = _依赖(tmp_path)
        收到: list[str] = []
        d.挂起 = 收到.append
        运行 = _运行器(d)
        r = 运行.调用("flow.sleep", 角色=RoleId.STRUCTURE, 参数={"reason": "等门禁"})
        assert r.is_ok
        assert 收到 == ["等门禁"]


# ═══ 判据 4：草稿纸 3 文件上限（用真适配器）═══


class Test草稿纸上限:
    def test_第四个被拒(self, tmp_path: Path) -> None:
        """D-042：每人每次任务最多 3 个文件。**这条必须用真适配器**——
        `FakeStorage.draft_paper_ok` 只看 `draft_ok` 标志、忽略计数。"""
        d, 真 = _依赖(tmp_path, 真存储=True)
        assert 真 is not None
        运行 = _运行器(d)
        角色 = RoleId.STRUCTURE
        for i in range(3):
            r = 运行.调用("draft.submit", 角色=角色, 参数={"content": f"第{i}稿", "version": i})
            assert r.is_ok, f"第 {i + 1} 个应被接受：{r.error if r.is_error else ''}"
        第4 = 运行.调用("draft.submit", 角色=角色, 参数={"content": "第四稿", "version": 4})
        assert 第4.is_error, "第 4 个草稿必须被拒"

    def test_上限按角色分别计(self, tmp_path: Path) -> None:
        """A 满了不影响 B。"""
        d, _ = _依赖(tmp_path, 真存储=True)
        运行 = _运行器(d)
        for i in range(3):
            _ = 运行.调用(
                "draft.submit", 角色=RoleId.STRUCTURE, 参数={"content": f"a{i}", "version": i}
            )
        _b = 运行.调用("draft.submit", 角色=RoleId.LANGUAGE, 参数={"content": "b", "version": 0})
        assert _b.is_ok, "B 的第一个草稿不该受 A 影响"

    def test_真的落盘了(self, tmp_path: Path) -> None:
        """D-165 同型守卫：断言文件真的在 `data/<run-id>/` 下，
        而不是只在内存里。**这条判据就是为了抓「写了假存储」**。"""
        d, _ = _依赖(tmp_path, 真存储=True)
        运行 = _运行器(d)
        r = 运行.调用("draft.submit", 角色=RoleId.STRUCTURE, 参数={"content": "正文", "version": 1})
        assert r.is_ok
        落盘 = list((tmp_path / "data").rglob("v1"))
        assert 落盘, "草稿没有真的写到磁盘上"

    def test_shared_write真的落盘且带run目录(self, tmp_path: Path) -> None:
        """两次运行必须隔离，否则公共区互相覆盖。"""
        d, _ = _依赖(tmp_path, 真存储=True)
        运行 = _运行器(d)
        _ = 运行.调用(
            "shared.write",
            角色=RoleId.STRUCTURE,
            参数={
                "name": "大纲",
                "kind": "outline",
                "content": "一、背景",
            },
        )
        落盘 = list((tmp_path / "data").rglob("*.outline"))
        assert 落盘, "公共区产物没落盘"
        assert _RUN in str(落盘[0]), f"落点应含 run 目录，实得 {落盘[0]}"


# ═══ 数据纪律：绝不编造 ═══


class Test参数校验:
    def test_缺必填参数被拒(self, tmp_path: Path) -> None:
        d, _ = _依赖(tmp_path)
        运行 = _运行器(d)
        assert 运行.调用("shared.write", 角色=RoleId.STRUCTURE, 参数={}).is_error
        assert 运行.调用("kb.search", 角色=RoleId.STRUCTURE, 参数={}).is_error

    def test_未知产物种类被拒(self, tmp_path: Path) -> None:
        """不认识的 kind 不能「猜一个最接近的」——那正是编造（D-007）。"""
        d, _ = _依赖(tmp_path)
        运行 = _运行器(d)
        r = 运行.调用(
            "shared.write",
            角色=RoleId.STRUCTURE,
            参数={
                "name": "x",
                "kind": "胡说八道",
                "content": "c",
            },
        )
        assert r.is_error

    def test_scope只能是kb或user(self, tmp_path: Path) -> None:
        d, _ = _依赖(tmp_path)
        运行 = _运行器(d)
        r = 运行.调用("kb.search", 角色=RoleId.STRUCTURE, 参数={"query": "通知", "scope": "别处"})
        assert r.is_error

    def test_截断材料必须标明(self, tmp_path: Path) -> None:
        """D-053 的两条：截断 **+ 标明**。只给内容不给标记，
        模型会把残缺材料当完整材料用。"""
        from zhengtong.domain.errors import Result as R
        from zhengtong.domain.task import UserFile, 读到的文件

        def 假读(name: str) -> R[读到的文件]:
            if name == "长材料.txt":
                return R.ok(读到的文件(text="内" * 10, 已截断=True, 完整字数=10, 原始字数=99999))
            return R.fail(ErrorCode.FILE_UNREADABLE, f"没有 {name}")

        class 假用户文件:
            registered: tuple[UserFile, ...] = ()

            def scan_and_register(self) -> tuple[UserFile, ...]:
                return ()

            def list_registered(self) -> tuple[UserFile, ...]:
                return ()

            #: staticmethod：写成普通函数会在类体内被绑定成方法、
            #: 多收一个 self，调用时抛 `takes 1 positional argument but 2 were given`。
            read_text = staticmethod(假读)

        d, _ = _依赖(tmp_path)
        d = replace(d, user_files=假用户文件())
        运行 = _运行器(d)
        r = 运行.调用("kb.read_user_file", 角色=RoleId.STRUCTURE, 参数={"filename": "长材料.txt"})
        assert r.is_ok, r.error
        assert "已截断" in r.unwrap(), "截断了却没标明"
        assert "99999" in r.unwrap(), "只说截断不说缺了多少"


# ═══ 副作用类别 ═══


class Test副作用类别:
    def test_类别分布与架构一致(self) -> None:
        按类: dict[Effect, set[str]] = {}
        for s in TOOL_SPECS:
            按类.setdefault(s.effect, set()).add(s.name)
        assert "shared.write" in 按类[Effect.PUBLISH]
        assert "draft.submit" in 按类[Effect.PUBLISH]
        assert "validate.run" in 按类[Effect.PURE]
        assert "flow.sleep" in 按类[Effect.SUSPEND]
        assert "flow.ask_user" in 按类[Effect.ASK_USER]
        assert "flow.rollback" in 按类[Effect.REWORK]

    def test_产物类工具必须标记requires_publish(self) -> None:
        """产物必须经公共区（D-042）。漏标等于允许绕过公共区。"""
        for s in TOOL_SPECS:
            if s.name in ("shared.write", "draft.submit"):
                assert s.requires_publish, f"{s.name} 必须标 requires_publish"

    def test_仅主理人工具不得是全员可调(self) -> None:
        for s in TOOL_SPECS:
            if s.仅主理人可调:
                assert not s.可被全员调用, f"{s.name} 标了仅主理人却又是全员可调"
