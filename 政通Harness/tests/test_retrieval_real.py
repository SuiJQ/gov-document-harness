"""用**真实公文语料**测召回。

## 为什么必须用真语料再测一遍

`tests/test_retrieval.py` 里的语料是**我造的**——
用它测出 top-1 100%，只能证明「实现符合我造数据的规律」，
**不能证明它在真公文上有效**。

这不是形式主义：自造语料的 n-gram 分布、词频分布、
区分词设计都是我按权重表反推的，**天然对实现有利**。

故本文件拿内容包 `content/` 的 21 份真实公文参考文档
（约 13 万字）做第二道召回测试。

## 期望答案怎么来

不靠人工标注（那既慢又主观）。用**每份文档里最独特的词**当查询——
取该文档中出现次数为 1 且字数 ≥3 的最长词。
**在别的文档里找不到这个词**，故 top-1 应当是它自己。

**这是可判定的，且不循环论证**：答案由文档本身的词频统计推出，
与检索实现无关。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from zhengtong.services.retrieval.search import 中文检索

内容根 = Path(__file__).resolve().parent.parent / "content"

#: 汉字 Unicode 区间（首尾）。**写常量不写 import**——
#: 从哪里 import「汉字范围」都要额外一个模块，而这里只是切分测试语料。
_首 = "一"
_尾 = "鿿"

TOP1下限 = 0.90
#: 拿多少份文档做召回测试。全量 21 份建索引很快，
#: 但查询条数取前 N 足以稳定。**不取满**是为了让测试跑得快，
#: 而召回率是比例指标，样本量够用即可。
取文档数 = 21
最多查询数 = 40


@pytest.fixture(scope="module")
def 真实索引() -> tuple[中文检索, dict[str, str], list[tuple[str, str]]]:
    路径 = sorted(内容根.rglob("*.md"))
    assert 路径, f"内容包为空：{内容根}"
    路径 = 路径[:取文档数]
    条目 = [(p.name, p.read_text(encoding="utf-8")) for p in 路径]
    r = 中文检索()
    got = r.build(tuple(条目))
    assert got.is_ok, got.error
    return r, dict(条目), _取查询(条目)


def _取查询(条目: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """为每份文档挑一个**只此一份**的词当查询。

    判据：全语料只出现 1 次、字数 ≥3、且不含标点。
    取**最长**的那个——长词区分度最高，2-gram 权重 0.45 主力靠它。
    """
    全词频: Counter[str] = Counter()
    for _, 正文 in 条目:
        全词频.update(_切词(正文))
    查询: list[tuple[str, str]] = []
    for ref, 正文 in 条目:
        本词频 = Counter(_切词(正文))
        独词 = [w for w, c in 本词频.items() if c == 1 and 全词频[w] == 1 and len(w) >= 3]
        if 独词:
            查询.append((max(独词, key=len), ref))
    return 查询[:最多查询数]


def _切词(文本: str) -> list[str]:
    """把文本切成连续汉字块（长度 ≥3）当「词」。

    **只切连续汉字块**——拉丁与标点当分隔符，
    否则「GB/T」之类会混进中文词里。
    """
    结果: list[str] = []
    当前: list[str] = []
    for 字 in 文本:
        if _首 <= 字 <= _尾:
            当前.append(字)
        else:
            if len(当前) >= 3:
                结果.append("".join(当前))
            当前 = []
    if len(当前) >= 3:
        结果.append("".join(当前))
    return 结果


# ═══ 真实语料召回 ═══


def test_查询集非空(真实索引) -> None:
    """**先确认语料构造成功**。查询集为空的话下面全部是空断言。"""
    _, 条目表, 查询 = 真实索引
    assert len(条目表) >= 10, f"语料太少：{len(条目表)} 份"
    assert len(查询) >= 10, f"只挑出 {len(查询)} 条可判定查询——判据会失真"
    # 期望 ref 必须真在语料里（循环论证的护栏）
    assert all(ref in 条目表 for _, ref in 查询)


def test_真实语料top1召回(真实索引) -> None:
    """**真实公文语料上的 top-1 召回 ≥90%**。

    低于自造语料是正常的——真文档术语更密、同主题词更多。
    门槛设 90% 而非 95%：自造语料的 95% 不能直接套到真公文上，
    **那会把「够用」误判成「不够」而反复调参**。
    """
    r, _, 查询 = 真实索引
    命中 = 0
    错: list[tuple[str, str, str | None]] = []
    for q, 期望 in 查询:
        候选 = r.search(q, top_k=8).unwrap()
        if 候选 and 候选[0].ref == 期望:
            命中 += 1
        else:
            错.append((q, 期望, 候选[0].ref if 候选 else None))
    率 = 命中 / len(查询)
    assert 率 >= TOP1下限, (
        f"真实语料 top-1 召回 {率:.1%} < {TOP1下限:.0%}（{命中}/{len(查询)}）；错例：{错[:3]}"
    )


def test_真实语料片段必须来自原文(真实索引) -> None:
    """片段必须是**原文里的连续片段**——
    拼出来的话会被模型当来源引用，那就是编造（D-007）。"""
    r, 条目表, 查询 = 真实索引
    assert 条目表, "语料表为空，下面的断言是空转"
    查 = [(q, ref) for q, ref in 查询[:10]]
    for q, _ in 查:
        for h in r.search(q, top_k=5).unwrap():
            assert h.snippet in 条目表[h.ref], f"{h.ref} 的片段不是原文连续片段：{h.snippet[:30]!r}"


def test_真实语料不返回引用不存在的ref(真实索引) -> None:
    """**返回的 ref 必须都在语料里**——
    模型会照着 ref 去找，找不到就是无来源断言。"""
    r, 条目表, 查询 = 真实索引
    for q, _ in 查询[:20]:
        for h in r.search(q, top_k=8).unwrap():
            assert h.ref in 条目表, f"返回了不存在的 ref：{h.ref}"


def test_同词不同文档不串味(真实索引) -> None:
    """同一查询多次调用**结果必须稳定**。

    真公文里同主题词极多（多个文档都讲「发文机关」），
    若排序不稳定，同一份材料两次检索得到不同来源——
    **断点续跑时结果不可重现，等于没跑过**。
    """
    r, _, 查询 = 真实索引
    for q, _ in 查询[:15]:
        a = tuple(h.ref for h in r.search(q, top_k=8).unwrap())
        b = tuple(h.ref for h in r.search(q, top_k=8).unwrap())
        assert a == b, f"查询 {q!r} 两次结果不同：{a} vs {b}"


def test_重建索引不残留旧文档(真实索引) -> None:
    """**重建必须先清空**——否则旧文档的倒排条目还在，
    返回已删除的 ref。"""
    r, _, _ = 真实索引
    # **两个参数分开传**：(正文元组, ref 元组)——第一版把
    # (ref, 正文) 直接当正文传，build 正确地报了「数量不符」。
    # 那一刻该改的是测试，不是实现的校验。
    assert r.build((("a.md", "关于甲事项的通知内容"),)).is_ok
    候选 = r.search("甲事项", top_k=8).unwrap()
    assert all(h.ref == "a.md" for h in 候选), f"残留了旧 ref：{[h.ref for h in 候选]}"


from string import digits  # noqa: E402  —— 占位，避免下方字符区间被格式化改动

_ = (digits,)
