"""`kernel/redactor.py` 测试 —— D-030「密钥绝不能进日志」的唯一执行者。

## 为什么这个文件的判据是「穷举」而非「抽样」

脱敏是**否定性**承诺：「任何结构里都不会有密钥」。
抽样只能证明"某些情况脱敏了"，证不了"没有漏的情况"。

且 `Event.data` 的类型是 `object`——可以是 str、list、tuple、dict、
任意嵌套。密钥十有八九藏在嵌套里（如请求体、日志片段），
**只测顶层等于没测**。

故本文件按「结构维度 × 泄漏位置」铺开：
每个组合都要有一条"密钥进不去"的证据。
"""

from __future__ import annotations

from typing import cast

import pytest

from zhengtong.domain.event import Event, EventKind, Level, Span
from zhengtong.domain.ids import RunId
from zhengtong.domain.phase import PhaseId
from zhengtong.kernel.redactor import REDACTED, Redactor

KEY = "sk-proj-ABCDEFGH12345678XYZ"  # sk- 形态，被第一道兜底模式覆盖
片段 = KEY[8:20]


# ═══ 事件级：D-120 不能只扫 data ═══


def test_call_id须脱敏() -> None:
    """**审计 D-120 查出的缺陷**：原实现只处理 `event.data`。

    而 `bus` 对**每个**事件调 `scrub`，于是任何进了 `call_id` 的密钥原样落盘。
    `call_id` 尤其危险——它本该是 `sha256`（D-076），
    但 `make_call_id` 接的是 `**discriminators: str`，
    一旦误把请求内容或 URL 塞进去，泄漏就发生在最不该发生的地方。
    """
    e = _ev()
    assert KEY in e.call_id, "前置：本测试的密钥确实在 call_id 里"
    out = Redactor().scrub(e)
    assert KEY not in out.call_id
    assert out.call_id == REDACTED


def test_run_id须脱敏() -> None:
    e = _ev(run_id=f"run-{KEY}")
    out = Redactor().scrub(e)
    assert KEY not in str(out.run_id)


def test_事件所有文本字段均已脱敏() -> None:
    """穷举判据：扫遍事件的每个字符串表示，都不该再有密钥。"""
    e = Event(
        seq=1,
        kind=EventKind.MODEL_REQUEST,
        run_id=RunId(f"run-{KEY}"),
        phase=PhaseId.DRAFTING,
        level=Level.ERROR,
        call_id=KEY,
        span=Span(role="team-lead", gate=KEY, round_no=1),
        data={"顶层": KEY, "嵌套": {"深": [KEY]}},
    )
    out = Redactor().scrub(e)
    文本 = f"{out.run_id}|{out.call_id}|{out.data}|{out.span}"
    assert KEY not in 文本, f"仍有泄漏：{文本}"


def test_枚举字段不被改坏() -> None:
    """`kind` / `phase` / `level` 来自封闭枚举集，不可能承载密钥。
    脱敏它们只会白白改坏合法数据。"""
    out = Redactor().scrub(_ev())
    assert out.kind is EventKind.MODEL_REQUEST
    assert out.phase is PhaseId.DRAFTING
    assert out.level is Level.ERROR


def test_原事件不被就地修改() -> None:
    """`Event` 是 frozen；`scrub` 必须返回新对象而非改原对象。"""
    e = _ev()
    _ = Redactor().scrub(e)
    assert e.call_id == KEY, "原事件被就地修改了"


def test_脱敏后结构类型保持() -> None:
    """tuple 应仍能序列化为 JSON 兼容结构——journal 要落盘。"""
    out = Redactor().scrub(_ev(data={"元组": (KEY,)}))
    assert isinstance(out.data["元组"], list)


# ═══ data 里的嵌套结构：穷举 ═══


@pytest.mark.parametrize(
    "载荷",
    [
        pytest.param(KEY, id="顶层字符串"),
        pytest.param([KEY], id="列表一层"),
        pytest.param([[KEY]], id="列表两层"),
        pytest.param((KEY,), id="元组"),
        pytest.param({"键": KEY}, id="字典一层"),
        pytest.param({"a": {"b": {"c": KEY}}}, id="字典三层"),
        pytest.param([{"a": [KEY]}], id="列表套字典"),
        pytest.param({"a": (KEY, "正常")}, id="字典套元组"),
    ],
)
def test_各种嵌套结构均脱敏(载荷: object) -> None:
    got = Redactor().scrub_value(载荷)
    assert KEY not in str(got), f"{载荷!r} 未脱敏"


@pytest.mark.parametrize(
    "载荷",
    [pytest.param(None, id="None"), pytest.param(42, id="整数"), pytest.param(True, id="布尔")],
)
def test_非文本值原样返回(载荷: object) -> None:
    assert Redactor().scrub_value(载荷) == 载荷


def test_字典键也被脱敏() -> None:
    """**容易漏的一处**：密钥可能出现在键名里，不只值里。"""
    got = Redactor().scrub_value({f"key是{KEY}": "值"})
    assert KEY not in str(got)


def test_普通中文不受影响() -> None:
    """脱敏不能误伤正常公文内容——这是最要紧的副作用约束。"""
    正文 = "关于做好旱情应对工作的通知\n请各地于本月底前报送。"
    assert Redactor().scrub_text(正文) == 正文


def test_短哈希不被误伤() -> None:
    """32 位以上的十六进制才拦；短的哈希指纹要留着，
    否则事件里全是 `[已脱敏]`，排障时看不出哪条是哪条。"""
    短 = "abc123"
    assert Redactor().scrub_text(f"指纹={短}") == f"指纹={短}"


def test_长十六进制被拦() -> None:
    长 = "a" * 40
    assert REDACTED in Redactor().scrub_text(f"值={长}")


# ═══ 各类密钥形态 ═══


@pytest.mark.parametrize(
    ("文本", "名"),
    [
        pytest.param(f"Authorization: Bearer {KEY}", "Bearer令牌"),
        pytest.param(f"api_key={KEY}", "api_key赋值"),
        pytest.param("password: hunter2xyz", "password赋值"),
        pytest.param("token=abcdefghijkl", "token赋值"),
    ],
)
def test_常见密钥形态被拦(文本: str, 名: str) -> None:
    """**不依赖 `SecretsPort` 的第二道防线**：即便用户没提供模式，
    这些形态也要拦得住。"""
    got = Redactor().scrub_text(文本)
    assert KEY not in got and "hunter2xyz" not in got and "abcdefghijkl" not in got, 名


def test_自定义片段生效() -> None:
    """`extra_patterns` 来自 `SecretsPort.redaction_patterns()`。"""
    r = Redactor(extra_patterns=("我的片段",))
    assert "我的片段" not in r.scrub_text("这里有我的片段出现")


def test_空片段不致崩() -> None:
    """`SecretsPort` 可能返回空元组或含空串，不能因此崩掉。"""
    assert Redactor(extra_patterns=("",)).scrub_text("正常文本") == "正常文本"


def test_脱敏标记本身可见() -> None:
    """刻意用可检索的固定串：万一真漏了，一眼能看出
    "这里本该有内容却是空的"，而不是一个看起来正常的空字符串。"""
    assert REDACTED == "[已脱敏]"


def test_重复脱敏幂等() -> None:
    """bus 可能重放同一事件；第二次脱敏不该把标记再洗一遍。"""
    r = Redactor()
    一次 = r.scrub_text(f"key={KEY}")
    assert r.scrub_text(一次) == 一次


# ═══ 夹具 ═══


def _ev(
    run_id: str = "r-1",
    call_id: str = KEY,
    data: dict[str, object] | None = None,
) -> Event:
    return Event(
        seq=1,
        kind=EventKind.MODEL_REQUEST,
        run_id=RunId(run_id),
        phase=PhaseId.DRAFTING,
        level=Level.ERROR,
        call_id=call_id,
        data=data if data is not None else {"请求体": f"Authorization: Bearer {KEY}"},
    )


# ═══ 中文上下文（D-122）═══════════════════════════════════════════════


def test_词边界在中文处失效() -> None:
    """**审计 D-122 的根因证据**：Python 的 `\\w` 含中文，
    故 `\\b` 在"我的key是sk-xxx"里找不到边界。"""
    import re

    assert re.match(r"\w", "是"), "前提变了：`\\w` 若不含中文，本条判据失效"


@pytest.mark.parametrize(
    "上下文",
    [
        pytest.param("我的key是{key}", id="中文紧贴"),
        pytest.param("错误信息：{key} 附近", id="冒号后"),
        pytest.param("前缀{key}", id="无分隔中文"),
        pytest.param("run-{key}", id="连字符前缀"),
        pytest.param("任务-{key}-结束", id="两侧包裹"),
        pytest.param("={key}", id="等号后"),
        pytest.param("{key}", id="裸密钥"),
    ],
)
def test_密钥嵌在中文里也须拦(上下文: str) -> None:
    """本项目是**中文公文系统**，密钥嵌在中文句子里是常态而非特例。

    原实现用 `\\bsk-`，而 `\\b` 基于 `\\w`、`\\w` 含中文——
    于是「我的key是sk-xxxx」匹配不上，密钥原样进日志。
    这与 D-115（中文检索）、D-121（中文键名）是同一类问题的第三次出现。
    """
    文本 = 上下文.format(key=KEY)
    assert KEY in 文本, "前置：密钥确实在文本里"
    assert KEY not in Redactor().scrub_text(文本)


@pytest.mark.parametrize(
    "正常文本",
    [
        pytest.param("task-manager", id="英文连字符词"),
        pytest.param("mask-key 设计", id="中文里的mask-key"),
        pytest.param("ask-me-anything", id="ask开头"),
        pytest.param("disk-usage", id="磁盘"),
        pytest.param("risk-score-2024", id="风险评分"),
        pytest.param("sk-learn 是机器学习库", id="sk前缀但非密钥"),
    ],
)
def test_不得误伤正常词(正常文本: str) -> None:
    """脱敏误伤的后果：正常公文内容被洗成 `[已脱敏]`，
    排障时反而看不出问题在哪。"""
    assert Redactor().scrub_text(正常文本) == 正常文本


# ═══ 键名脱敏碰撞（D-121）════════════════════════════════════════════


def test_键名碰撞不静默丢数据() -> None:
    """两个**不同**的密钥做键名，脱敏后都是 `[已脱敏]`——
    碰撞不可避免，但**不能无声吞掉一条**。"""
    甲, 乙 = "sk-aaaaaaaaaaaaaaaaaaaaaaaa1", "sk-bbbbbbbbbbbbbbbbbbbbbb2"
    结果 = Redactor().scrub_value({甲: "甲的值", 乙: "乙的值"})
    assert isinstance(结果, dict), f"dict 输入应回 dict，实得 {type(结果).__name__}"
    映射 = cast("dict[str, object]", 结果)
    撞 = [k for k in 映射 if "碰撞" in k]
    assert 撞, f"碰撞未留痕，疑似静默丢数据：{结果}"
    assert 映射[撞[0]] == 1


def test_无碰撞时不加多余标记() -> None:
    结果 = Redactor().scrub_value({"正常键": "值"})
    assert 结果 == {"正常键": "值"}


# ═══ D-134：片段是锚点，不是待替换的完整子串 ═══════════════════════════
#
# 本节的判据是**穷举 + 反向验证**：每条用例都必须同时证明
# 「密钥主体没落盘」和「周围说明文字没被误伤」。
# 只证明前者是不够的——一个把所有东西都替换成 [已脱敏] 的脱敏器
# 同样能通过前一条，但它会让日志彻底失去可读性。


def _泄(输出: str, key: str, 片段: str) -> bool:
    """密钥全文或**主体**（去掉片段后的剩余部分）是否还在输出里。"""
    主体 = key[len(片段) :]
    return key in 输出 or (bool(主体) and 主体 in 输出)


@pytest.mark.parametrize(
    ("key", "锚点", "文本"),
    [
        pytest.param(
            "projXYZ1234567890abcdef",
            "projXYZ",
            "连接串 projXYZ1234567890abcdef 超时",
            id="前缀片段",
        ),
        pytest.param("Zq7x9w2mKj3n", "Zq7x", "凭据 Zq7x9w2mKj3n 无效", id="中段片段"),
        pytest.param(
            "AbCdEf1234567890XyZ", "AbCdEf", "字段 AbCdEf1234567890XyZ 缺失", id="另一中段"
        ),
        pytest.param(
            "projXYZ1234567890abcdef",
            "projXYZ",
            "Authorization: Bearer projXYZ1234567890abcdef",
            id="Bearer形态",
        ),
        pytest.param(
            "sk-proj-abcdef123456",
            "sk-proj",
            "我的key是sk-proj-abcdef123456请检查",
            id="sk形态嵌中文",
        ),
    ],
)
def test_片段替换必须吞掉密钥主体(key: str, 锚点: str, 文本: str) -> None:
    """**修复前 3/3 用例泄漏。**

    原实现对 `extra_patterns` 做 `str.replace` 字面替换。而
    `SecretsPort.redaction_patterns()` 按设计只返回**片段**
    （D-030：让 Redactor 持有「怎么脱」而非「脱什么」），
    于是字面替换只吃掉片段本身，密钥主体原样落盘：

        key  = "projXYZ1234567890abcdef"
        片段 = "projXYZ"
        修复前 = "连接串 [已脱敏]1234567890abcdef 超时"

    **这比不脱更糟**：日志里留下「一半密钥 + 一半明文上下文」，
    比完整密钥更容易被拼接还原。

    故片段被当作**锚点**，向后贪婪吞掉同构成字符
    （`[A-Za-z0-9._-]`），一次吃到密钥的自然边界。
    这样 D-030 的设计意图（Redactor 不持有密钥全文）
    与实际效果（脱干净）两件事才同时成立。
    """
    输出 = Redactor(extra_patterns=(锚点,)).scrub_text(文本)
    assert not _泄(输出, key, 锚点), f"密钥主体落盘：{输出!r}"


@pytest.mark.parametrize(
    ("锚点", "文本", "须保留"),
    [
        pytest.param(
            "sk-proj", "我的key是sk-proj-abcdef请检查并重试", "请检查并重试", id="中文说明"
        ),
        pytest.param(
            "projXYZ",
            "连接串 projXYZ1234567890abcdef 超时，请稍后重试",
            "请稍后重试",
            id="逗号后说明",
        ),
        pytest.param("Zq7x", "凭据 Zq7x9w2mKj3n 无效，请联系管理员", "请联系管理员", id="致谢语"),
    ],
)
def test_脱敏不得吃掉后续说明(锚点: str, 文本: str, 须保留: str) -> None:
    """**贪婪必须有边界。**

    若无脑吃到空白，「key 是 sk-xxx 请检查」会把后面的中文说明
    一起替换掉，日志可读性尽失——而日志的价值恰恰在于
    「出了什么事、上下文是什么」。

    故边界排除空格与中文：密钥后面跟的往往是给人看的说明。
    """
    输出 = Redactor(extra_patterns=(锚点,)).scrub_text(文本)
    assert 须保留 in 输出, f"脱敏误伤了说明文字：{输出!r}"


def test_锚点不存在时文本不变() -> None:
    """锚点不命中就什么都不该改——不能凭空插入 `[已脱敏]`。"""
    文本 = "一切正常的日志行"
    assert Redactor(extra_patterns=("sk-proj",)).scrub_text(文本) == 文本


def test_多个锚点各自生效() -> None:
    """用户可能有多个凭据（如主 key + 备用 key）。"""
    文本 = "主 projXYZ1234567890abc 备用 Zq7x9w2mKj3n 都试过了"
    输出 = Redactor(extra_patterns=("projXYZ", "Zq7x")).scrub_text(文本)
    assert "1234567890abc" not in 输出
    assert "9w2mKj3n" not in 输出
    assert "都试过了" in 输出, f"说明文字被误伤：{输出!r}"
