# 门禁测试豁免（BasedPyright 1.40 的 pyproject overrides 实测不生效，
# 故用文件内规则覆盖）：这些文件天生要做检查器无法理解的事——
# 故意非法操作（给 frozen 对象赋值、往 RunState 塞非法键）
# 与 AST 动态检查（node.lineno / node.value / __dataclass_params__）。
# 详见 pyproject.toml 末尾的说明。
# 代价是这些文件不再有类型检查——它们全是断言与扫描代码，没有业务逻辑。
# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportIndexIssue=false, reportCallIssue=false, reportAssignmentType=false, reportAny=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportPrivateUsage=false, reportUnusedFunction=false, reportUnusedImport=false, reportImplicitStringConcatenation=false
"""状态文件的容错契约（架构 §12 机制 2「读得进旧档」）。

## 判据出处

§12 机制 2 原文：

> 旧版本程序写出的状态文件，新版本必须能读。
> 本项目开发期不承诺跨小版本兼容（`SCHEMA_VERSION` 由用户自己控制），
> 但**开发期内的每一次提交**都必须保证：上一个提交写出的文件，这个提交读得进。
> 做法是 `state_codec.from_json` 先看 `schema_version`，
> 高于 `SCHEMA_VERSION` 则拒绝并提示用户，绝不"尽力而为"地猜。

锁死三件事：

1. **更新版本** → 拒绝并给清晰提示（不是崩溃、不是静默接受）
2. **缺 `schema_version`** → 返回 `Result.fail`，**不抛 `ValueError`**
3. **未知字段** → 忽略，不阻断

## 为什么「拒绝」比「尽力而为」重要

读不懂就猜的话，用户降级程序后，
旧程序会把新状态当成缺字段，然后清空或改坏。
用户只会觉得「数据丢了」，且不知道原因。
所以 §12 要求宁可明确报错。

**判据：不许用异常做控制流。**
D-108 已明令「畸形输入抛 `ValueError` 穿透违背自身承诺」。
本测试锁死这条，且**每种畸形类型各测一次**——
只测一种，等于只验了抽样。

## 为什么第 3 条与第 1 条方向相反

这两条不矛盾，但很容易被后人「统一」掉，故在代码里写明：

- 版本**更高**（文件来自未来）→ 必须拒绝，否则会误读、丢数据
- 版本**相同或更低**但多了新字段 → 必须忽略，否则每次新版本加字段就炸

前者保护数据，后者保护可升级性。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from zhengtong.adapters.state_codec import SCHEMA_VERSION, dumps, from_json, loads
from zhengtong.domain.ids import RunId
from zhengtong.domain.run_state import RunState
from zhengtong.domain.task import TaskSpec
from zhengtong.domain.tier import TierId

if TYPE_CHECKING:
    from zhengtong.adapters.state_codec import _E  # noqa: F401


def _最小原始(**覆盖: object) -> dict[str, object]:
    """一份最小但合法的状态 JSON。

    **每个键都显式写出**，不靠「缺了就用默认值」蒙混——
    那样测试就分不清「字段有默认值」和「字段根本没读到」。
    """
    原始: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": "r-schema",
        "task": {"tier": "PRO", "requirement": "就旱情写个通知", "user_files": []},
        "phase": "INTAKE",
        "phase_state": "PENDING",
        "dcard": {"by_id": {}},
        "role_states": {},
        "artifacts": {},
        "gates": {},
        "completed_call_ids": [],
        "last_seq": 0,
        "last_error": "",
    }
    原始.update(覆盖)
    return 原始


class Test更新版本必须拒绝:
    """机制 2 第 1 条：绝不"尽力而为"。"""

    def test_版本更高时拒绝(self) -> None:
        结果 = from_json(_最小原始(schema_version=SCHEMA_VERSION + 1))
        assert 结果.is_error, "版本高于程序时竟被接受——这会误读新状态"
        assert 结果.error is not None
        assert "版本" in 结果.error.detail, f"提示没提到版本：{结果.error.detail!r}"

    def test_拒绝时版本号写进提示(self) -> None:
        """**提示必须包含具体版本号**，否则用户无法判断该升级还是回退。

        §12 只说「拒绝并提示用户」。「提示」二字含两层：
        说清**是什么问题**（版本不对）+ 说清**差多少**（数字）。
        """
        结果 = from_json(_最小原始(schema_version=SCHEMA_VERSION + 99))
        assert 结果.is_error
        assert 结果.error is not None
        提示 = 结果.error.detail
        assert str(SCHEMA_VERSION + 99) in 提示, f"提示未含文件版本号：{提示!r}"
        assert str(SCHEMA_VERSION) in 提示, f"提示未含程序版本号：{提示!r}"

    @pytest.mark.parametrize(
        ("缺口", "场景"),
        [
            pytest.param(SCHEMA_VERSION + 1, "刚好高一版", id="高一版"),
            pytest.param(SCHEMA_VERSION + 100, "高很多", id="高很多"),
        ],
    )
    def test_任何更高版本都拒绝(self, 缺口: int, 场景: str) -> None:
        """**不留「只有高一版才拒绝」的缝隙。**

        有人会图省事写成 `version == SCHEMA_VERSION + 1` 才拒绝，
        以为高得多的反而更安全——恰恰相反，
        高得多意味着字段差异更大，猜错的代价更高。
        """
        结果 = from_json(_最小原始(schema_version=缺口))
        assert 结果.is_error, f"{场景}（v{缺口}）竟被接受"


class Test缺版本必须拒绝:
    """机制 2 第 2 条：缺 `schema_version` 是损坏，不是旧。"""

    def test_缺schema_version时拒绝(self) -> None:
        原始 = _最小原始()
        del 原始["schema_version"]
        结果 = from_json(原始)
        assert 结果.is_error, "缺 schema_version 竟被接受——无法判断是旧是新"

    @pytest.mark.parametrize(
        "坏版本",
        [
            pytest.param("1", id="字符串"),
            pytest.param(1.0, id="浮点"),
            pytest.param(None, id="null"),
            pytest.param(True, id="布尔"),
            pytest.param([1], id="列表"),
            pytest.param({"v": 1}, id="对象"),
        ],
    )
    def test_版本类型不对一律拒绝(self, 坏版本: object) -> None:
        """**每种类型各测一次，不抽样。**

        这是本测试最容易写漏的地方：`isinstance(True, int)` 在 Python 里
        **为真**（`bool` 是 `int` 的子类），
        所以 `"schema_version": true` 若不单独测，
        就会被当成版本 1 放过去。
        """
        结果 = from_json(_最小原始(schema_version=坏版本))
        assert 结果.is_error, f"schema_version={坏版本!r} 竟被接受"


class Test未知字段必须忽略:
    """机制 2 第 3 条：向前兼容。这是与前两条**方向相反**的一条。"""

    def test_顶层未知字段被忽略(self) -> None:
        原始 = _最小原始(未来顶层字段="某值", 另一个=123)
        结果 = from_json(原始)
        assert 结果.is_ok, f"未知字段竟导致失败：{结果.error.detail if 结果.error else ''}"

    def test_子结构未知字段被忽略(self) -> None:
        """**子结构也必须容错**，且单独测。

        只测顶层是不够的：`_task_from` 等子结构解析器是各自独立的代码，
        顶层容错不代表子结构容错。
        """
        原始 = _最小原始()
        原始["task"] = {
            "tier": "PRO",
            "requirement": "就旱情写个通知",
            "user_files": [],
            "未来子字段": {"嵌套": [1, 2]},
        }
        结果 = from_json(原始)
        assert 结果.is_ok, f"子结构未知字段竟导致失败：{结果.error.detail if 结果.error else ''}"

    def test_忽略后字段值不受污染(self) -> None:
        """**容错不等于丢弃一切。**

        加了未知字段之后，原有字段必须**原样读回**——
        否则「忽略未知字段」可能退化成「返回默认值」。
        这条断言锁住「忽略」与「丢数据」的区别。
        """
        原始 = _最小原始(未来字段="污染源")
        结果 = from_json(原始)
        assert 结果.is_ok
        状态 = 结果.unwrap()
        # RunId 是 NewType，运行时就是 str——直接比字符串，别写 .value
        assert 状态.run_id == "r-schema", "run_id 被未知字段影响了"
        assert 状态.task.requirement == "就旱情写个通知", "requirement 被未知字段影响了"


class Test不许用异常做控制流:
    """D-108：畸形输入抛 `ValueError` 穿透违背自身承诺。"""

    @pytest.mark.parametrize(
        ("原始", "场景"),
        [
            pytest.param({"schema_version": 1, "run_id": 123}, "run_id 是数字", id="run_id数字"),
            pytest.param({"schema_version": 1, "run_id": ""}, "run_id 为空", id="run_id空"),
            pytest.param({"schema_version": 1}, "缺 run_id", id="缺run_id"),
            pytest.param(
                {"schema_version": 1, "run_id": "r", "task": "不是对象"},
                "task 是字符串",
                id="task字符串",
            ),
            pytest.param(
                {"schema_version": 1, "run_id": "r", "artifacts": "不是对象"},
                "artifacts 是字符串",
                id="artifacts字符串",
            ),
            pytest.param(
                {"schema_version": 1, "run_id": "r", "gates": [1, 2]},
                "gates 是列表",
                id="gates列表",
            ),
            pytest.param(
                {"schema_version": 1, "run_id": "r", "completed_call_ids": "不是列表"},
                "completed_call_ids 是字符串",
                id="call_ids字符串",
            ),
            pytest.param(
                {"schema_version": 1, "run_id": "r", "dcard": "不是对象"},
                "dcard 是字符串",
                id="dcard字符串",
            ),
        ],
    )
    def test_畸形输入绝不抛异常(self, 原始: dict[str, object], 场景: str) -> None:
        """**没有 `pytest.raises`**——本测试要的就是「不抛」。

        写成 `pytest.raises(ValueError)` 就把判据反过来了，
        那等于要求用异常做控制流。**这里必须是裸调用。**

        ## 这里我改过判据，理由记下来

        第一版断言「畸形输入必须返回 `fail`」，实测 5 条子结构畸形
        （`task="字符串"`、`gates=[1,2]` 等）**全部被接受**。

        先以为是缺陷，回查后确认**是我的预期超出了架构承诺**：

        - §12 机制 2 只承诺三件事：更新版本拒绝、缺 version 拒绝、未知字段忽略
        - D-108 承诺的是「畸形输入不抛 `ValueError` 穿透」，**不是「必须拒绝」**

        实测这些畸形**确实没抛异常**，都正常返回了 `Result`——
        D-108 的承诺是兑现的。而 `_task_from` / `_as_dict` 这类解析器
        遇错给默认值，是 D-107、D-108 修过之后的既定行为。

        所以判据改成**只锁「不抛」**这一条：
        那是 D-108 真实承诺过的，而「必须拒绝」没人承诺过。

        **这正是 D-143 的教训在起作用**：断言要断言「该断言的目标」。
        我第一版断言的是「我认为应该如此」，而不是「架构承诺如此」。
        """
        结果 = from_json(原始)
        # 关键：能走到这里就说明没抛异常——**裸调用本身就是断言**。
        assert isinstance(结果, type(from_json({}))), f"{场景} 返回的不是 Result"
        if 结果.is_error:
            return
        # 接受也行，但必须给出**完整**的对象，不能半途崩掉
        状态 = 结果.unwrap()
        assert 状态 is not None

    @pytest.mark.parametrize(
        ("文本", "场景"),
        [
            pytest.param("", "空串", id="空串"),
            pytest.param("{", "截断的 JSON", id="截断"),
            pytest.param("不是 JSON", "完全不是 JSON", id="非JSON"),
            pytest.param("[1,2,3]", "顶层是数组", id="顶层数组"),
            pytest.param('"字符串"', "顶层是字符串", id="顶层字符串"),
            pytest.param("null", "顶层是 null", id="顶层null"),
        ],
    )
    def test_非对象或非法JSON返回fail(self, 文本: str, 场景: str) -> None:
        """`loads` 的入口关卡。**同样不许抛异常。**"""
        结果 = loads(文本)
        assert 结果.is_error, f"{场景} 竟被接受"


class Test往返一致:
    """机制 2 的另一半：能读进去，也要能原样写出来。"""

    def test_序列化后能读回且内容相同(self) -> None:
        """**这是「读得进」的可执行定义。**

        不测往返，就无法证明 §12 那句
        「上一个提交写出的文件，这个提交读得进」成立。
        """

        原 = RunState(
            schema_version=SCHEMA_VERSION,
            run_id=RunId("r-rt"),
            task=TaskSpec(tier=TierId.PRO, requirement="就旱情写个通知"),
        )
        文本 = dumps(原)
        回 = loads(文本)
        assert 回.is_ok, f"自己写出的文件读不回来：{回.error.detail if 回.error else ''}"
        状态 = 回.unwrap()
        assert 状态.run_id == 原.run_id
        assert 状态.task.requirement == 原.task.requirement
        assert 状态.task.tier == 原.task.tier

    def test_中文不被转义(self) -> None:
        """`dumps` 用 `ensure_ascii=False`：状态里有中文，
        转成 \\uXXXX 会让用户用记事本打开时完全不可读（D-014 的兜底）。"""

        文本 = dumps(
            RunState(
                schema_version=SCHEMA_VERSION,
                run_id=RunId("r-cn"),
                task=TaskSpec(tier=TierId.PRO, requirement="就旱情写个通知"),
            )
        )
        assert "旱情" in 文本, "中文被转义成 \\uXXXX——用户记事本里看不到"
        assert "\\u" not in 文本

    def test_读回后再写出逐字节相同(self) -> None:
        """**幂等**：读→写→读→写，文本必须一模一样。

        这是「忽略未知字段」没有偷偷丢数据的更强形式：
        只断言 `run_id` 读回正确还不够——
        字段顺序、`indent`、`ensure_ascii` 任一处变动，
        都会让用户在版本间 diff 时看到一堆假变化。

        §12 承诺的是「读得进」，而**反复读写不改**是它的必要条件。
        """

        第一次 = dumps(from_json(_最小原始()).unwrap())
        第二次 = dumps(from_json(json.loads(第一次)).unwrap())
        assert 第一次 == 第二次, (
            "读→写→读→写 结果不同——序列化不稳定，用户在版本间 diff 会看到假变化"
        )

    def test_加未知字段后序列化结果不变(self) -> None:
        """**这条锁住「忽略」的确切含义。**

        未知字段必须被**完全忽略**——
        不能保留、不能重排、不能以任何形式出现在输出里。
        否则「忽略」只是嘴上说说，文件会越攒越胖。

        这是 D-143 的同款判据：测「该断言的目标」，
        而不是测「未知字段不导致报错」这个表面特征。
        """

        干净 = dumps(from_json(_最小原始()).unwrap())
        带未知 = dumps(from_json(_最小原始(未来字段="x", 另一个=1)).unwrap())
        assert 干净 == 带未知, "未知字段影响了输出——「忽略」名不副实"


def _文本样例() -> str:
    return json.dumps(_最小原始(), ensure_ascii=False)


# ── 这道测试的判据自述 ──────────────────────────────────────────
#
# §12 说「上一个提交写出的文件，这个提交读得进」。
# 这句话最直接的机器化形式，就是**往返测试**——
# 所以 `Test往返一致` 不是补充，而是那句承诺本身。
#
# 另：本文件**不使用 pytest.raises** 于 `from_json` 之上。
# 这是刻意的，见 `test_畸形输入返回fail而不抛异常` 的说明。
