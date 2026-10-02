"""L4 检索的契约测试（第 8 步）。

判据（架构 §15 第 8 步）：中文 n-gram 倒排 + 加权 TF-IDF 主路径（D-073）；
top-k 召回 ≥95%；FTS5 仅拉丁补充。

## 本文件盯的三件事

1. **召回**——top-1 命中率 ≥95%（架构实测基线是 top-8 100%）。
2. **主路径真的是 n-gram**，不是碰巧对：写一条判据证明
   FTS5 的默认分词器对中文**完全无效**，故中文能力必然来自自建倒排。
3. **不许有"必须唯一命中"判断**（D-073）——
   这是架构专门写进注释的一条纪律，**它最容易被"优化"掉**。
"""

from __future__ import annotations

import sqlite3
from typing import Any, cast

import pytest

from corpus_factories import 干扰语料, 探针词, 探针语料, 查询与答案, 混合语料
from zhengtong.domain.errors import ErrorCode
from zhengtong.services.retrieval.search import 建检索器

TOP1下限 = 0.95


@pytest.fixture(scope="module")
def 索引():
    """整个模块共用一个索引——`build` 在大语料上不便宜。"""
    条目 = 混合语料()
    r = 建检索器()
    got = r.build(tuple(条目 for 条目 in 条目))
    assert got.is_ok, got.error
    return r, 条目


# ═══ 一、召回率（主判据）═══


def test_中文召回率达标(索引) -> None:
    """**top-1 命中率 ≥95%**。

    架构 §7.3 的判据原文：「5 个候选里只要 top-1 正确，检索层就是合格的
    ——精确过滤是下游门禁与模型的活」。故用 top-1 而非 top-8 计分。
    """
    r, _ = 索引
    答案 = 查询与答案(混合语料())
    命中 = 0
    for 查询, 期望 in 答案:
        got = r.search(查询, top_k=8)
        assert got.is_ok, got.error
        候选 = got.unwrap()
        if 候选 and 候选[0].ref == 期望:
            命中 += 1
    率 = 命中 / len(答案)
    assert 率 >= TOP1下限, f"top-1 召回 {率:.1%} < {TOP1下限:.0%}（{命中}/{len(答案)}）"


def test_高区分度查询必须全对(索引) -> None:
    """**唯一关键词是基准线，不是难例**——这条卡 100%。

    唯一关键词只在一篇出现，任何「返回全部候选」的实现都能满足它。
    卡 100% 是为了确认：**连最简单的情况都不是碰巧对的**。
    """
    r, _ = 索引
    答案 = 查询与答案(混合语料())
    错 = []
    for 查询, 期望 in 答案:
        候选 = r.search(查询, top_k=8).unwrap()
        if not 候选 or 候选[0].ref != 期望:
            错.append((查询, 期望, 候选[0].ref if 候选 else None))
    assert not 错, f"{len(错)}/{len(答案)} 条基础查询都排不对：{错[:3]}"


def test_排序不是按插入顺序(索引) -> None:
    """干扰语料里**每篇都含全部高频词**，只有区分词不同。

    若排序退化成「按插入顺序返回」，查 10 次得到同一个 top-1——
    这正是它要抓的退化。
    """
    r, _ = 索引
    条目 = 干扰语料(20)
    got = r.build(tuple(条目 for 条目 in 条目))
    assert got.is_ok
    名次: list[str] = []
    for i in range(10):
        候选 = r.search(探针词(i), top_k=5).unwrap()
        assert 候选, f"探针{i}无候选"
        名次.append(候选[0].ref)
    assert len(set(名次)) == 10, (
        f"10 次不同查询得到 {len(set(名次))} 个不同 top-1——排序没生效：{名次}"
    )


# ═══ 二、主路径真的是 n-gram（证伪 FTS5 依赖）═══


def test_fts5对中文完全无效() -> None:
    """**先证明为什么不能用 FTS5 做主路径**（架构 §7.3 实测结论）。

    `unicode61` 按 `[A-Za-z0-9_]+` 切词，**汉字不在这个字符类里**，
    整句无空格中文被当成一个超长 token。于是「通知」「批复」这类
    **公文最高频的检索词全部 0 命中**。

    本条把架构里那张实测表**重新跑一遍**：
    **架构实测表不能照抄当判据**——那是别人机器上的测量。
    """
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
        for 词 in ("通知", "批复", "工作", "增长"):
            db.execute("INSERT INTO t(body) VALUES (?)", (f"关于做好{词}有关工作的通知",))
        命中 = {
            词: db.execute("SELECT count(*) FROM t WHERE t MATCH ?", (词,)).fetchone()[0]
            for 词 in ("通知", "批复", "工作", "增长")
        }
    finally:
        db.close()
    assert 命中["通知"] == 0, f"FTS5 竟然命中了「通知」{命中}——本判据的前提变了，要重估主路径"
    assert 命中["批复"] == 0, f"FTS5 竟然命中了「批复」{命中}"


def test_中文检索不依赖fts5(索引) -> None:
    """**主路径独立于 FTS5**：把 FTS5 完全关掉，中文召回不得下降。

    架构 §7.3 明确：「FTS5 不可用时**只损失拉丁字符检索能力，
    中文主路径完全不受影响**」。这条把它锁住。
    """
    # **自己的检索器**：module 级 fixture 是可变的，
    # 前一个测试重建过索引（干扰语料），共享它必然拿到错的索引。
    # 第一版就是这么写的，召回算成 0%——**失败原因指向无关的地方**。
    条目 = 混合语料()
    r = 建检索器()
    assert r.build(tuple(条目)).is_ok
    答案 = 查询与答案(条目)
    with r.临时关闭补充路径() as 关前:
        assert 关前, "FTS5 本就不可用，本条测不到任何东西——前提变了要重估"
        assert not r.拉丁可用, "没真的关掉补充路径"
        命中 = sum(
            1
            for 查询, 期望 in 答案
            if (候选 := r.search(查询, top_k=8).unwrap()) and 候选[0].ref == 期望
        )
    率 = 命中 / len(答案)
    assert 率 >= TOP1下限, f"关掉 FTS5 后召回降到 {率:.1%}——说明中文主路径依赖了 FTS5"
    assert r.拉丁可用, "退出上下文后未恢复——污染了后续测试"


# ═══ 三、禁止「必须唯一命中」判断（D-073）═══


def test_同分时仍返回全部候选() -> None:
    """**架构专门写进注释的纪律，最容易被"优化"掉**。

    D-073：返回有序候选，不做「必须唯一命中」判断。
    两篇完全相同的文档 → 分数必然相同 → 若实现里有唯一性检查，
    此刻就会报错或返回空。**正确的实现应当照常返回两条。"""
    r = 建检索器()
    got = r.build((("a", "完全相同的内容"), ("b", "完全相同的内容")))
    assert got.is_ok, got.error
    候选 = r.search("完全相同", top_k=8).unwrap()
    assert len(候选) == 2, f"同分候选被吞掉了，只剩 {len(候选)} 条"


def test_无命中不报错(索引) -> None:
    """**不报错**，且**噪声不得冒充命中**。

    第一版断言「返回空」，实测返回 8 条——**因为 1-gram 兜底**
    （架构 §7.3：权重 0.15，「功能性必需」，去掉它「批复」这类
    两字词会直接归零）。查一句完全无关的话，每个汉字都能以
    极低分命中。

    **那不是缺陷，是 1-gram 的既定代价**，D-073 也明确
    「返回有序候选，不做唯一命中判断」——检索层不判断相关性，
    那是门禁与模型的活。

    故判据改为绑在**分数量级**上：噪声候选的分数必须
    **远低于**真实命中的分数。否则模型拿到一串高分噪声，
    会当成真材料引用——那是「严禁编造」（D-007）的反例。
    """
    r, _ = 索引
    噪声 = r.search("这个字符串肯定不在任何一篇里出现过", top_k=8)
    assert 噪声.is_ok, f"无命中应返回结果而非错误：{噪声.error}"
    真 = r.search("独000词", top_k=1).unwrap()
    if 噪声.unwrap() and 真:
        assert 真[0].score > 噪声.unwrap()[0].score * 10, (
            f"噪声分数 {噪声.unwrap()[0].score:.2e} 与真实命中 "
            f"{真[0].score:.2e} 量级相同——模型会分不出哪个是命中"
        )


def test_空查询返回空而非崩(索引) -> None:
    r, _ = 索引
    assert r.search("", top_k=8).unwrap() == ()


# ═══ 四、形状与可重现性 ═══


def test_命中按分数降序(索引) -> None:
    r, _ = 索引
    候选 = r.search("通知", top_k=8).unwrap()
    分 = [h.score for h in 候选]
    assert 分 == sorted(分, reverse=True), f"候选未按分数降序：{分}"


def test_同分按ref排序保证可重现(索引) -> None:
    """**同分必须有确定性序**（`SearchHit` 文档已写）。

    不稳定排序会让同一查询两次结果不同，
    而检索结果会进 prompt 与台账——**不可重现的结果没法排查**。
    """
    r = 建检索器()
    r.build((("c", "同样的内容"), ("a", "同样的内容"), ("b", "同样的内容")))
    两次 = [tuple(h.ref for h in r.search("同样", top_k=8).unwrap()) for _ in range(5)]
    assert len(set(两次)) == 1, f"同分排序不稳定：{两次}"


def test_片段来自命中文档(索引) -> None:
    """`snippet` 必须是**该文档里真实存在的文本**——
    拼一段看起来像的话会误导模型（"严禁编造"）。"""
    r, 条目 = 索引
    正文表 = dict(条目)
    for h in r.search("通知", top_k=5).unwrap():
        assert h.snippet, f"{h.ref} 片段为空"
        assert h.snippet in 正文表[h.ref], f"{h.ref} 的片段不是原文：{h.snippet[:20]!r}"


def test_top_k生效(索引) -> None:
    r, _ = 索引
    assert len(r.search("通知", top_k=3).unwrap()) <= 3
    assert len(r.search("通知", top_k=8).unwrap()) <= 8


def test_top_k为0不报错(索引) -> None:
    """`top_k=0` 是合法调用，返回空即可——**不是错误**。"""
    r, _ = 索引
    assert r.search("通知", top_k=0).unwrap() == ()


# ═══ 五、规模护栏 ═══


def test_超文档上限降级而非耗尽内存() -> None:
    """架构 §7.3：`INDEX_DOC_CAP`（默认 20000）超出即**降级为只索引
    标题与首段并如实提示**，而不是继续吃内存直到被杀掉。

    本条用**可注入的小上限**验证降级路径存在——
    真造 20001 篇不现实（慢），而**降级逻辑本身**才要测。
    """
    r = 建检索器()
    r.设文档上限(5)  # 注入小上限，不改全局常量
    条目 = [(f"d{i}", f"第{i}篇的正文内容，关键词标记{i}") for i in range(10)]
    got = r.build(tuple(条目 for 条目 in 条目))
    assert got.is_ok, got.error
    assert r.已降级(), "超上限未降级"
    提示 = r.降级提示()
    assert "5" in 提示 and "10" in 提示, f"提示未如实说明：{提示!r}"
    # 降级后仍可检索（只是覆盖面变小）
    assert r.search("标记3", top_k=8).unwrap(), "降级后连首段都检索不到"


def test_未超上限不降级() -> None:
    r = 建检索器()
    r.设文档上限(100)
    条目 = 探针语料(10)
    r.build(tuple(条目 for 条目 in 条目))
    assert not r.已降级(), "没超上限却降级了"


def test_空索引查询不崩() -> None:
    r = 建检索器()  # 自己的实例，不共享
    assert r.search("任何东西", top_k=8).unwrap() == ()


# ═══ 六、失败路径 ═══


def test_ref重复应报错() -> None:
    """**ref 是定位依据，必须唯一**（`SearchHit.ref` 文档已写）。

    重复 ref 会让检索结果无法定位到具体文档——
    而结果要进 prompt，定位不到就等于模型拿到了无来源的断言。
    """
    r = 建检索器()
    got = r.build((("dup", "甲文档内容"), ("dup", "乙文档内容")))
    assert got.is_error, "重复 ref 被放过了"
    assert got.error is not None
    assert got.error.code is ErrorCode.FILE_UNREADABLE


def test_错位的一对应报错而非静默() -> None:
    """**`(ref, 正文)` 对的价值就在这里**：错位立刻炸。

    原签名是两个平行元组（`build(docs, refs)`），数量不符能返回 fail，
    但**把 `(ref, 正文)` 整个当正文传**也能混过去——
    第一版测试就是这么写的，错误信息是「ref 数量与正文数量不符」，
    **当时差点以为是实现错了**。

    现在传入的不是二元组，解包就失败。判据绑在这个性质上：
    **错位的输入不该被静默接受，更不该被当成合法数据索引**。
    """
    r = 建检索器()
    # **有意传错类型**：这正是本条要验的输入。
    # 用 `cast` 绕过检查器——**判据不能因为类型检查器不高兴就改写法**
    # （早先被 `check.sh --fix` 悄悄改过一次）。
    坏输入 = cast("Any", ("不是二元组",))
    got = r.build(坏输入)
    assert got.is_error, "非二元组被当成了合法条目"
    assert got.error is not None


# ═══ 七、权重表本身（证伪②栽在这）═══
#
# 前六节测的是**端到端召回**，而我造的语料区分词都是 4–5 字，
# 于是「所有 n-gram 等权」与「按权重表加权」**召回结果完全一样**——
# 证伪②（权重表改成全 1.0）24 条全绿。
#
# **判据只测端到端结果，就测不出中间那步。**
# 故本节直接验权重表：同一批文档里，让命中只靠不同长度的
# n-gram，得分必须**按 0.15 / 0.45 / 1.0 / 1.4 严格递增**。

from zhengtong.services.retrieval.indexer import NGRAM权重  # noqa: E402


def test_权重表数值正确() -> None:
    """架构 §7.3 的表是**实测得出**的，不是随手填的。"""
    assert NGRAM权重 == {1: 0.15, 2: 0.45, 3: 1.00, 4: 1.40}


def test_权重随n严格递增() -> None:
    """**n 越大权重越大**——长片段信息量更大，是架构的设计意图。

    （第一版断言了「递减」，与设计相反：低权重恰恰是给短片段兜底用的。）
    """
    序 = [NGRAM权重[n] for n in sorted(NGRAM权重)]
    assert 序 == sorted(序) and len(set(序)) == len(序), f"权重不是随 n 严格递增：{NGRAM权重}"


def test_权重按比例进入得分() -> None:
    """**白盒验权重表**——端到端验不了，只能这样。

    第一版想用端到端测（「长片段得分应更高」），实测：
    查「通知」，全文只有这两个字的短文档得分 **0.082**，
    含 10 个 n-gram 的长文档 **0.075**，短文档反而更高。

    **那是 BM25 的正确行为**，不是缺陷：`tf` 的长度归一按**文档长度**算，
    短文档里「通知」占全文，tf 自然更高。
    拿跨文档的长短比长短，测的是长度归一，不是 n-gram 权重。

    故改为**验线性缩放**：同一篇文档、同一个词，只改传入的查询权重，
    得分之比应当**精确等于权重之比** 0.45/0.15 = 3。
    这验的是「权重真的线性进入得分」，而第一版两次都传 1.0，
    比值自然是 1.0——**那条判据什么都没测**。
    """
    from zhengtong.services.retrieval.indexer import 倒排索引

    ix = 倒排索引()
    ix.建(("关于通知的批复",), ("a",))
    一字 = ix.打分({"通知": 0.15})[0]
    二字 = ix.打分({"通知": 0.45})[0]
    assert 二字 / 一字 == pytest.approx(0.45 / 0.15, rel=1e-6), (
        f"权重 0.45 与 0.15 的得分之比是 {二字 / 一字:.3f}，"
        f"应为 {0.45 / 0.15:.1f}——权重没按比例进入"
    )


def test_长度归一是文档级的() -> None:
    """把上一条的教训钉死：**短文档里同一个词的相关性更高**。

    这是 BM25 的定义行为，不是可调参数——
    写成「长文档该得分更高」会把正确实现判成错的。
    """
    r = 建检索器()
    r.build((("甲", "通知"), ("乙", "申办通知")))
    分 = {h.ref: h.score for h in r.search("通知", top_k=8).unwrap()}
    assert 分["甲"] > 分["乙"], f"短文档里「通知」占全文，tf 归一后应更高，却低了：{分}"


def test_查询权重只取长度不重复累加() -> None:
    """查询里同一个词重复出现**不加分**——
    同词重复不增加信息量，却会让含该词的文档得分翻倍。"""
    from zhengtong.services.retrieval.indexer import 查询权重

    单 = 查询权重(("通知", "通知", "通知"))
    assert 单["通知"] == pytest.approx(0.45, rel=1e-6), f"重复查询词被累加了：{单}"
