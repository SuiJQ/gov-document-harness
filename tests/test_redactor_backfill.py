"""脱敏器对「后缀锚点」的处理（第 5 步实测补上）。

## 查出来的缺陷

第 5 步把真实密钥接进 `LocalSecrets` 后实测六个样本，
发现**后缀片段只覆盖它自己，前面那段裸露**：

    密钥   = "sk-4d49...fa7a676102265"
    片段   = ("sk-4d492", "2265")          # 前缀 + 后缀
    文本   = "后缀段 5f55550fa7a676102265 泄漏"
    修复前 = "后缀段 5f55550fa7a67610[已脱敏] 泄漏"   ← 半截明文

完整密钥确实没泄漏（**不违反 D-030**），但留下 16 个字符的密钥片段
加上明文上下文，比完整密钥更容易被拼接还原——**这比不脱更糟**。

## 根因

D-134 修过一次（原实现是字面 `str.replace`，只吃掉片段本身），
改成「锚点向后贪婪吞到自然边界」。**但只有向后。**
前缀锚点天然在密钥开头，向后吞正好覆盖全文；
后缀锚点在末尾，向后吞只能吃掉自己那 4 个字符。

故补**向前吞**：从锚点往前吃同构成字符。
两条规则合起来，任何位置的锚点都能覆盖整段密钥。
"""

from __future__ import annotations

from zhengtong.kernel.redactor import Redactor


#: 查「有没有残留明文」而不是「完整密钥在不在」。
#:
#: **证伪时踩的第二个坑**：第一版断言写成
#: `assert 假密钥 not in out`。但后缀锚点的缺陷形态是
#: **完整密钥被吃掉了、剩下前 60 个字符裸露**——
#: 完整密钥确实不在结果里，断言照样通过。
#: 也就是说：那个断言测的正是「已经满足的那部分」，
#: 对真正的缺陷完全失明。
def _残留(原文: str, 脱敏后: str) -> str:
    """返回密钥的**最长裸露子串**。空串表示脱干净了。"""
    最佳 = ""
    for 起 in range(len(原文)):
        for 止 in range(len(原文), 起, -1):
            片 = 原文[起:止]
            if 片 in 脱敏后 and len(片) > len(最佳):
                最佳 = 片
            if len(最佳) and 止 - 起 < len(最佳):
                break
    return 最佳


#: 形态逼真的假密钥。不用真密钥——测试进 git。
#:
#: **刻意不用 `sk-` 开头的形态**——证伪时踩过的坑：
#: 第一版测试用的就是 `sk-`，结果 8 条全绿，看着像「向前吞已经生效」。
#: 实际是 `Redactor._PATTERNS` 里的 `PAT_SK` 兜住的，
#: 跟锚点毫无关系。换成非 `sk-` 形态后立刻复现缺陷。
#: **这正是「测试必须能证伪」的意思**：能过的测试不等于测到东西。
假密钥 = "AKIA9PROJ4d49250f2829531b2cd40ba85af817d2d117cb71822c5f55550fa7a676102265"
片段 = ("AKIA9PR", "2265")


def _r() -> Redactor:
    return Redactor(extra_patterns=片段)


# ═══ 一、后缀锚点必须能向前覆盖 ═══


def test_后缀锚点向前吞掉前面的密钥() -> None:
    """**只给后缀片段**——这才是本缺陷的真正形状。

    证伪时实测：给全两个片段时本条恒过（前缀片段向后吞掉了全部），
    换成只有后缀才复现：残留 36 个字符明文。
    """
    r = Redactor(extra_patterns=("2265",))
    露 = _残留(假密钥, r.scrub_text(f"后缀段 {假密钥} 泄漏"))
    assert 露 == "", f"残留 {len(露)} 个字符明文：{露[:20]}…"


def test_全片段时也不漏() -> None:
    assert _残留(假密钥, _r().scrub_text(f"后缀段 {假密钥} 泄漏")) == "", "有密钥明文残留"


def test_纯后缀锚点也能全覆盖() -> None:
    """**只有后缀片段**（前缀片段缺失）时也必须脱干净——
    不能依赖「另一个锚点会救它」。"""
    r = Redactor(extra_patterns=("2265",))
    露 = _残留(假密钥, r.scrub_text(f"某处 {假密钥} 出现"))
    assert 露 == "", f"残留 {len(露)} 个字符明文"


def test_锚点在中间时两侧都覆盖() -> None:
    r = Redactor(extra_patterns=("50f28295",))
    露 = _残留(假密钥, r.scrub_text(f"中间锚点 {假密钥} 结束"))
    assert 露 == "", f"残留 {len(露)} 个字符明文"


# ═══ 二、向前吞不得吃掉可读内容 ═══


def test_向前吞不吃空格与中文() -> None:
    """这是向后吞那条纪律的对称要求。

    「请检查 sk-xxx」若向前吞到中文，日志就没法看了。
    脱敏是为了不让密钥落盘，**不是为了让日志变得不可读**。
    """
    r = Redactor(extra_patterns=("2265",))
    out = r.scrub_text(f"请检查 {假密钥} 是否有效")
    assert "请检查" in out, "向前吞把中文说明吃掉了"
    assert "是否有效" in out, "向前吞把尾部中文吃掉了"


def test_向前吞不吃另一个键值对() -> None:
    r = Redactor(extra_patterns=("2265",))
    out = r.scrub_text(f"key={假密钥} next=abc")
    assert "next=abc" in out, "向前吞越界吃掉了后续内容"


def test_空格分隔的两个键不粘连() -> None:
    """`a=<key> b=2` 里向前吞不能一路吃到 `b=2`。"""
    r = Redactor(extra_patterns=("2265",))
    out = r.scrub_text(f"a={假密钥} b=2")
    assert "b=2" in out


# ═══ 三、向前吞不得引入新泄漏 ═══


def test_片段本身不出现在结果里() -> None:
    for 锚点 in 片段:
        assert 锚点 not in _r().scrub_text(f"值 {假密钥} 结束")


def test_空片段不导致全文本被清() -> None:
    """空串锚点若参与向前吞，会把整段文本吃掉。"""
    r = Redactor(extra_patterns=("",))
    out = r.scrub_text("正常日志内容")
    assert out.strip(), "空锚点把整段文本清空了"
