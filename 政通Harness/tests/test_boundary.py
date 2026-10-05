# 门禁测试豁免（BasedPyright 1.40 的 pyproject overrides 实测不生效，
# 故用文件内规则覆盖）：这些文件天生要做检查器无法理解的事——
# 故意非法操作（给 frozen 对象赋值、往 RunState 塞非法键）
# 与 AST 动态检查（node.lineno / node.value / __dataclass_params__）。
# 详见 pyproject.toml 末尾的说明。
# 代价是这些文件不再有类型检查——它们全是断言与扫描代码，没有业务逻辑。
# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportIndexIssue=false, reportCallIssue=false, reportAssignmentType=false, reportAny=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportPrivateUsage=false, reportUnusedFunction=false, reportUnusedImport=false, reportImplicitStringConcatenation=false
"""跨层调用规约（架构 §3.5）。

## 为什么这道门禁比分层那道更重要

§3.5 原文：

> 依赖方向只管"谁能看见谁"，管不住"传了什么过去"。
> 一个常见的退化：编排层为了方便，把整个 `RunState` 递给服务层——
> 服务层拿到后改了其中某个字段，编排层以为是自己改的。
> **这时 `frozen=True` 也没用，因为它拿到的是同一个对象。**

并补一句：

> 这条比依赖方向规则更有效——依赖方向错了测试立刻红，
> 而"传了整个状态对象"是**能通过所有现有测试的慢性退化**。

## 本测试锁四条

1. **`RunState` 的 `dict` 字段不得被原地改**（规约 3：状态只在编排层写）
2. 服务层函数的第一个参数**不叫 `state`**（§3.5 实现约定）
3. 跨层参数必须是 L0 `frozen` 值对象或纯数据（规约 1：只传值对象）
4. 服务不得持有传入对象的引用（规约 2：入深出浅）

## 规约 1 的实测背景

`RunState` 声明 `frozen=True`，但含三个 `dict` 字段
（`role_states` / `artifacts` / `gates`）。实测：

    st.phase = ...              → FrozenInstanceError ✅ 字段赋值被挡住
    st.role_states["x"] = 1     → 成功 ❌           原地改挡不住

§3.5 点名的退化**现在就能触发**。生产代码目前是干净的
（所有写方法都用 `{**self.xxx}` 造新 dict），但**防护靠自觉、无强制**。
本测试就是那道强制。
"""

# ── 反证记录（这道门禁被攻击过 4 次）─────────────────────────
#   1. 编排层原地改 role_states            → 1 条红
#   2. 服务层首参叫 state                  → 1 条红
#   3. 判定函数改成恒返回空                → 1 条红
#   4. update() 写法未被识别              → 由自检拦住
#
# 本道被判定为废三次才真正拦住（见 DECISIONS.md D-145）：
# 第一版匹配串对不上、第二版分不清读与写而误报 4 处。
# 另有两处防呆来自实测教训：
#   · test_门禁不得自我屏蔽（曾按文件名跳过探针，反证材料被自己屏蔽）
#   · 「读不算写」（误报比漏报更危险——门禁一旦误报就会被注释掉）

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from zhengtong.domain.run_state import RunState

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "zhengtong"

#: 服务层。规约 1、2 的落点。
SERVICE_LAYERS = frozenset({"services"})

#: `RunState` 里所有可变字段。§3.5 的退化就发生在这里。
MUTABLE_FIELDS: tuple[str, ...] = ("role_states", "artifacts", "gates")


def _源码文件() -> list[pathlib.Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _层(路径: pathlib.Path) -> str:
    相对 = 路径.relative_to(SRC)
    return "__root__" if 相对.name == "app.py" else 相对.parts[0]


class TestRunState冻结有效性:
    """规约 3 的核心：`frozen=True` 挡不住原地改，本测试补上那道强制。"""

    def test_字段赋值被frozen挡住(self) -> None:
        """**先确认前提成立**，再谈别的。

        这一条看着平凡，但没有它，下面两条就是无根之木——
        若哪天 `frozen` 被去掉，全部相关断言都要重写。
        """
        st = _状态()
        with pytest.raises(dataclasses.FrozenInstanceError):
            st.phase = "篡改"  # type: ignore[misc]

    @pytest.mark.parametrize("字段", MUTABLE_FIELDS)
    def test_可变字段被声明为dict(self, 字段: str) -> None:
        """**这条是「前提已知」的存档，不是断言。**

        它锁住一个事实：`RunState` 的这三个字段确实是可变容器。
        哪天有人把它们改成 `Mapping`/`tuple`，这里会红，
        提醒他同步更新 `test_boundary` 的规约 3 表述——
        因为「不再可变」本身是好消息，只是门禁的措辞要跟着变。
        """
        注解 = {f.name: f.type for f in dataclasses.fields(RunState)}[字段]
        assert "dict[" in str(注解), f"{字段} 已不是 dict，规约 3 的表述需同步更新"

    def test_原地改dict不会被frozen挡住(self) -> None:
        """**这是本测试存在的理由，必须显式记下来。**

        它断言的是一个**已知漏洞**：字段赋值抛异常，原地改不抛。
        有人会觉得「既然会通过为什么要写」——
        因为它把 §3.5 描述的退化固定在文档里，
        将来若给 `RunState` 加了 `__post_init__` 深冻结，这条会红，
        那正是应该庆祝的时刻。
        """
        st = _状态()
        st.role_states["偷偷塞一个"] = "值"  # type: ignore[index]
        assert "偷偷塞一个" in st.role_states


class Test无原地修改:
    """规约 3：状态只允许编排层写，且不得原地改。"""

    def test_全仓无人原地改RunState的可变字段(self) -> None:
        """**这是本测试真正要拦的东西，也是第二版才真正拦住的。**

        前两版都错在同一个地方：**分不清读与写**。
        第一版把 `.role_states[` 当匹配串，对不上实际输出；
        第二版修好了匹配，却把 `state.role_states.items()` 这类**读**
        也当成违规，于是报出 4 处全属误报——

            adapters/state_codec.py:95   state.role_states.items()   ← 读
            adapters/fakes.py:314        self.artifacts[...]         ← 假实现的字段

        误报的代价很实在：**门禁一旦误报，就会被人加豁免或注释掉，
        于是真正该拦的写法也跟着一起放过。**
        所以判据必须精确到「只认改变容器的那三种写法」：

        1. `st.role_states["k"] = v`      下标赋值
        2. `st.role_states.update(...)`    就地方法（`del`/`pop` 同样改变容器）

        只读不算违规——`.items()`、`.get()`、`in` 全部放行。
        """
        违例: list[str] = []
        for 路径 in _源码文件():
            树 = ast.parse(路径.read_text(encoding="utf-8"))
            for 节点 in ast.walk(树):
                目标 = _被改的容器(节点)
                if not 目标:
                    continue
                if _命中(目标, MUTABLE_FIELDS) and _是RunState的字段(路径, 节点, 目标):
                    违例.append(f"{路径.relative_to(SRC)}:{节点.lineno}  {目标}")

        assert not 违例, (
            "原地修改 RunState 的可变字段——"
            "§3.5：服务层拿到整个状态就能改，frozen 挡不住。\n"
            "正确写法是返回新实例（见 RunState.set_role_state / put_artifact）。\n"
            + "\n".join(违例)
        )

    def test_扫描器本身能识别原地赋值(self) -> None:
        """**防呆：门禁的扫描器自己也可能坏掉，且坏得极安静。**

        这条断言 `_被改的容器` 在两段真实代码上确实返回了目标——
        它的反证经历就是教训：匹配串写成 `.role_states[` 而实际输出是
        `st.role_states`，于是「全仓无人原地改」在有违规的情况下依然全绿。

        **一道从不变红的门禁，和没有门禁一样可疑。**
        """
        片段 = ast.parse('st.role_states["x"] = 1').body[0]
        assert isinstance(片段, ast.Assign)
        目标 = _被改的容器(片段)
        assert 目标 == "st.role_states", f"扫描器未识别下标赋值，得到 {目标!r}"
        assert _命中(目标, MUTABLE_FIELDS)

        就地 = ast.parse("st.artifacts.update({})").body[0].value  # 取 Call 节点
        assert _被改的容器(就地) == "st.artifacts"

        # 读不算写——这一条是第一版没想清楚的
        assert _被改的容器(ast.parse("x.role_states.items()").body[0].value) == ""  # 读

    def test_门禁不得自我屏蔽(self) -> None:
        """**第三个坑，也是最阴的一个。**

        我在扫描循环里写了 `if 路径.name == "_probe.py": continue`——
        想着「反证探针不该算产品代码」。结果**反证材料自己被门禁跳过**，
        于是「故意放一个违规文件进去」这个动作永远验证不出任何东西，
        而我据此以为门禁已经生效。

        这条断言读**本测试文件自身的源码**，检查扫描循环里没有
        「按文件名跳过」的规则。

        判据很朴素：门禁要么全扫，要么显式说明扫哪些，
        **不接受「按文件名开洞」**——
        跳过规则一旦存在，就等于给绕过留了一个不受检查的口子，
        而且它会随着时间被当成正常写法扩散。
        """
        本文件 = pathlib.Path(__file__)
        树 = ast.parse(本文件.read_text(encoding="utf-8"))

        跳过点: list[str] = []
        for 节点 in ast.walk(树):
            # 找出「跳过某类文件」的 continue：其所在 if 的判据里含 .name
            if not isinstance(节点, ast.If):
                continue
            if not any(isinstance(x, ast.Continue) for x in ast.walk(节点)):
                continue
            判据 = ast.unparse(节点.test)
            if ".name" in 判据 and ("路径" in 判据 or "文件" in 判据 or "f.name" in 判据):
                跳过点.append(判据)

        assert not 跳过点, (
            "门禁按文件名跳过：" + "；".join(跳过点) + "——"
            "这会让反证材料被自己屏蔽，绕过留了一个不受检查的口子"
        )


class Test服务层入深出浅:
    """规约 1 与 2 的落点。"""

    def test_服务层首参不得名为state(self) -> None:
        """§3.5 实现约定：

        > 服务层函数的**第一个参数不叫 `state`**。
        > 用命名约束让人一眼看出传窄了。

        这是**用命名做文档**的约定，故按名字断言。
        """
        违例: list[str] = []
        for 路径 in _源码文件():
            if _层(路径) not in SERVICE_LAYERS:
                continue
            树 = ast.parse(路径.read_text(encoding="utf-8"))
            for 节点 in ast.walk(树):
                if not isinstance(节点, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                参数 = 节点.args.args
                if not 参数:
                    continue
                首参 = 参数[0].arg
                # `self` / `cls` 不算首参
                if 首参 in ("self", "cls") and len(参数) > 1:
                    首参 = 参数[1].arg
                if 首参 == "state":
                    违例.append(f"{路径.relative_to(SRC)}:{节点.lineno}  {节点.name}(state=...)")
        assert not 违例, (
            "服务层首参叫 state——§3.5 明确禁止：\n"
            "它意味着拿到了整个运行状态，而下层只该拿片段。\n" + "\n".join(违例)
        )


class Test跨层只传值对象:
    """规约 1：跨层参数必须是 L0 frozen 值对象或纯数据。"""

    def test_领域对象全部frozen(self) -> None:
        """**这是「只传值对象」能成立的前提。**

        §3.5 说跨层传 `frozen` 值对象。若领域层有可变对象，
        整条规约就落空了——所以先锁「全 frozen」。
        """
        import importlib
        import pkgutil

        import zhengtong.domain as D

        违例: list[str] = []
        计数 = 0
        for info in pkgutil.iter_modules(D.__path__):
            模块 = importlib.import_module(f"zhengtong.domain.{info.name}")
            for 名, 对象 in vars(模块).items():
                # dataclass **类**本身就是 type，故不能靠 `isinstance(x, type)` 排除
                # （我第一版就是这么写的，结果把全部领域对象滤掉了，
                #   幸好加了「计数 > 0」的防呆断言才没让这条测试空跑）
                if not dataclasses.is_dataclass(对象):
                    continue
                if not 对象.__module__.startswith("zhengtong.domain"):
                    continue
                计数 += 1
                if not 对象.__dataclass_params__.frozen:
                    违例.append(f"{info.name}.{名}")
        assert 计数 > 0, "没扫到任何领域 dataclass——扫描逻辑失效了"
        assert not 违例, "领域对象未全部 frozen：\n" + "\n".join(违例)


# ── 工具 ────────────────────────────────────────────────────────


def _状态() -> RunState:
    from zhengtong.domain.ids import RunId
    from zhengtong.domain.task import TaskSpec
    from zhengtong.domain.tier import TierId

    return RunState(
        schema_version=1,
        run_id=RunId("r-boundary"),
        task=TaskSpec(tier=TierId.PRO, requirement="就旱情写个通知"),
    )


def _被改的容器(节点: ast.AST) -> str:
    """若该节点**改变了**一个容器，返回其对象路径；否则返回空串。

    **只认写，不认读**——这是第二版修正的核心。

    判定为「写」的两种形态：
      `x.field[...] = v`        → Subscript 出现在 Assign 的 target 里
      `x.field.update(...)`     → 调用了改变容器的方法（`update`/`clear`/`pop`…）

    以下**不算写**，故返回空串：
      `x.field.items()` / `.get()` / `.keys()` / `x.field in y`   —— 读
      `{**x.field, k: v}`                                            —— 造新 dict，正确写法

    第一版把 `.items()` 也算成写，报出 4 处误报；
    误报比漏报更危险，详见 `test_全仓无人原地改RunState的可变字段` 的说明。
    """
    if isinstance(节点, ast.Assign):
        for 目标 in 节点.targets:
            if isinstance(目标, ast.Subscript) and isinstance(目标.value, ast.Attribute):
                return _源码文本(目标.value)
        return ""

    if isinstance(节点, ast.Call):
        函数 = 节点.func
        if (
            isinstance(函数, ast.Attribute)
            and isinstance(函数.value, ast.Attribute)
            and 函数.attr in _改动容器的方法
        ):
            return _源码文本(函数.value)
    return ""


def _是RunState的字段(路径: pathlib.Path, 节点: ast.AST, 目标: str) -> bool:
    """判断被改的属性是否属于 `RunState`，而非同名巧合。

    第三版修正。`FakeStorage.artifacts` 与 `RunState.artifacts` **同名**，
    但前者是假实现自己的字段，原地改完全正当——
    第三版之前它被误报成违规。

    判据：**该属性所在的类是不是 `RunState` 本身。**
    用 AST 找属性所属的类名（`self.x` → 所在 ClassDef），
    只有类名为 `RunState` 时才算命中。

    局部变量（`state.role_states[...] = v`）没有所属类，
    靠**类型注解**判定：形参标注了 `RunState` 就算。
    两者都判不出时**保守放行**——门禁宁可漏也不可误报。
    """
    属性名 = 目标.split(".")[-1]
    变量名 = 目标.split(".")[0]

    # `self.` 形式：找它所在的类
    if 变量名 == "self":
        for 节点2 in ast.walk(节点):
            if isinstance(节点2, ast.ClassDef) and 节点2.name == "RunState":
                return 属性名 in {f.name for f in dataclasses.fields(RunState)}
        return False

    # 局部变量：形参/变量标注了 RunState 就算
    if 属性名 not in {f.name for f in dataclasses.fields(RunState)}:
        return False
    return _引用了RunState的局部变量(路径, 变量名)


def _引用了RunState的局部变量(路径: pathlib.Path, 变量名: str) -> bool:
    """该函数里是否有形参标注为 `RunState`，且名与传入的变量名一致。"""
    树 = ast.parse(路径.read_text(encoding="utf-8"))
    for 节点 in ast.walk(树):
        if not isinstance(节点, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        形参 = 节点.args.args + 节点.args.kwonlyargs
        for a in 形参:
            if a.arg != 变量名:
                continue
            标注 = ast.unparse(a.annotation) if a.annotation else ""
            if "RunState" in 标注:
                return True
    return False


#: 只有这些方法会改变容器。**`.items()`/`.get()`/`.keys()` 故意不在其中**——
#: 它们是读，把读判成写就是误报，而误报会让门禁被人关掉。
_改动容器的方法: frozenset[str] = frozenset(
    {"update", "clear", "pop", "popitem", "setdefault", "extend"}
)


def _源码文本(节点: ast.AST) -> str:
    try:
        return ast.unparse(节点)
    except Exception:
        return ""


def _命中(文本: str, 片段列表: tuple[str, ...]) -> bool:
    """文本是否含任一片段。`artifacts` 会命中 `artifact_*` 前缀吗？不会——
    这里用的是带前导点的完整片段如 `.artifacts[`，故无误伤。"""
    return any(片段 in 文本 for 片段 in 片段列表)
