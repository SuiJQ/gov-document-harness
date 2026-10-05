"""中文 n-gram 倒排索引（D-073 主路径）。**零第三方依赖**（D-013）。

## 为什么自写而不是用 FTS5

这不是偏好，是实测：`FTS5` 默认分词器 `unicode61` 按 `[A-Za-z0-9_]+`
切词，**汉字不在这个字符类里**，整句无空格中文被当成一个超长 token。
实测「通知」「批复」「工作」「增长」**全部 0 命中**——
而这正是公文最高频的检索词（架构 §7.3 有完整实测表，
`tests/test_retrieval.py::test_fts5对中文完全无效` 会重跑一遍它）。

`FTS5` 降为拉丁字符的补充路径，**不可用时只损失拉丁检索能力，
中文主路径完全不受影响**——比「FTS5 挂了降级到 n-gram」健壮得多。

## n-gram 权重

| n | 权重 | 作用 |
|---|---|---|
| 1 | 0.15 | 兜底，保证单字词可召回 |
| 2 | 0.45 | 主力，"通知""批复"靠它 |
| 3 | 1.00 | 提升区分度 |
| 4 | 1.40 | 长查询靠它拿到高区分度 |

**1-gram 保留是功能性必需，不是妥协**：去掉它，「批复」这类两字词会归零。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Final

#: n-gram 权重（架构 §7.3 表）。**越长的片段信息量越大**。
NGRAM权重: Final[dict[int, float]] = {1: 0.15, 2: 0.45, 3: 1.00, 4: 1.40}

#: 生成的最大 n。4 是实测平衡点：再长则短查询完全无重叠。
最大N: Final[int] = 4

#: TF-IDF 参数（架构 §7.3 公式）。
_K1: Final[float] = 1.2
_B: Final[float] = 0.25

#: 汉字区间（Unicode CJK 统一表意文字基本区）。
#:
#: **用字符串比较而不是正则**：实测（5000 篇建索引实测）
#: 正则 `re.match` 121 ns/字、字符串比较 48 ns/字、ord+集合 64 ns/字。
#: 建索引时 `是汉字` 要对每个汉字调一次，**这就是热路径**。
_汉首 = "一"
_汉尾 = "鿿"


def 是汉字(字: str) -> bool:
    """是否 CJK 汉字。**不含标点与空格**——
    切 n-gram 时标点必须当分隔符，否则「通知。」的 3-gram 跨了标点。"""
    return _汉首 <= 字 <= _汉尾


@dataclass(frozen=True, slots=True)
class _文档:
    ref: str
    #: 分词结果（n-gram 元组）。**建索引时算一次，查询时复用**。
    词表: tuple[str, ...]


@dataclass(slots=True)
class 倒排索引:
    """n-gram → (文档序, 词频) 的倒排表 + 打分所需的长度信息。

    **只保留必要的**：不存正文（片段在搜索时从 `_原文` 取），
    倒排表里是整数与浮点，不存字符串元组——省内存是实测约束
    （5000 篇约 24–78 MB）。
    """

    _倒排: dict[str, dict[int, int]] = field(default_factory=dict)
    _长度: list[int] = field(default_factory=list)
    _原文: list[str] = field(default_factory=list)
    _refs: list[str] = field(default_factory=list)
    _已降级: bool = False
    _文档上限: int = 20000

    # ── 建索引 ──

    def 建(self, 原文: tuple[str, ...], refs: tuple[str, ...]) -> None:
        """建立/重建索引。**先清空**——重建不该在旧索引上叠加。"""
        self._倒排.clear()
        self._长度.clear()
        self._原文.clear()
        self._refs.clear()
        self._已降级 = False

        待建 = 原文
        if len(原文) > self._文档上限:
            # **降级而非耗尽内存**（架构 §7.3 规模护栏）。
            # 只索引标题与首段，并如实标记——**不静默降低质量**。
            self._已降级 = True
            待建 = tuple(_取首段(t) for t in 原文)

        for 序, (文本, ref) in enumerate(zip(待建, refs, strict=True)):
            词表 = 分词(文本)
            self._长度.append(len(词表))
            self._原文.append(文本)
            self._refs.append(ref)
            频: dict[str, int] = {}
            for 词 in 词表:
                频[词] = 频.get(词, 0) + 1
            for 词, 次 in 频.items():
                # **`setdefault(词, {})` 每次都造一个空 dict 再丢弃**
                # （词不在倒排表里时）。用「取出来，没有才建」省掉那次分配。
                表 = self._倒排.get(词)
                if 表 is None:
                    self._倒排[词] = {序: 次}
                else:
                    表[序] = 次

    # ── 查询 ──

    def 打分(self, 查询权重: dict[str, float]) -> dict[int, float]:
        """按加权 TF-IDF 给每篇打分。

        ```
        score(d) = Σ_t idf(t)·qw(t)·tf(t,d)·k₁ / (tf(t,d) + k₁·(b + b·len(d)/maxlen))
        idf(t)   = ln(1 + (N − df(t) + 0.5) / (df(t) + 0.5))
        ```
        """
        总数 = len(self._长度)
        if 总数 == 0:
            return {}
        最长 = max(self._长度) or 1
        分: dict[int, float] = {}
        for 词, 查询w in 查询权重.items():
            表 = self._倒排.get(词)
            if not 表:
                continue
            df = len(表)
            idf = math.log(1 + (总数 - df + 0.5) / (df + 0.5))
            for 序, 词次 in 表.items():
                归一 = 词次 * _K1 / (词次 + _K1 * (1 - _B + _B * self._长度[序] / 最长))
                分[序] = 分.get(序, 0.0) + idf * 查询w * 归一
        return 分

    # ── 取用 ──

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(self._refs)

    def 片段(self, 序: int, 查询词: str, 宽度: int = 60) -> str:
        """取命中处的原文片段。

        **必须是原文**——模型会把它当来源引用，拼出来的话就是编造（D-007）。
        命中词找不到时退回开头，因为开头是公文价值最高的部分（标题、发号）。
        """
        文本 = self._原文[序]
        位 = 文本.find(查询词)
        if 位 < 0:
            return 文本[:宽度]
        起 = max(0, 位 - 宽度 // 3)
        return 文本[起 : 起 + 宽度].strip()

    def 长度(self, 序: int) -> int:
        return self._长度[序]


def 分词(文本: str) -> tuple[str, ...]:
    """切成 n-gram 序列。

    汉字串切 1/2/3/4-gram；拉丁字母数字按整词（大小写归一）；
    标点与空白当分隔符。
    """
    结果: list[str] = []
    for 段 in _切段(文本):
        if _全拉丁(段):
            结果.append(段.lower())
            continue
        字 = [c for c in 段 if 是汉字(c)]
        for n in NGRAM权重:  # 权重不在这里用——见 `查询权重`
            结果.extend(_滑窗(字, n))
    return tuple(结果)


def 查询权重(查询词表: tuple[str, ...]) -> dict[str, float]:
    """查询里每个 n-gram 的权重（按长度查表）。

    **权重与分词分开是刻意的**：建索引和查查询走同一套 `分词`，
    权重只在打分时用一次。混进 `分词` 会让索引里也存一份无用权重。

    **查询里重复的词只算一次**——同词重复不增加信息量，
    却会让那篇含该词的文档凭空得分翻倍。
    """
    权重: dict[str, float] = {}
    for 词 in 查询词表:
        n = _ngram长度(词)
        权重[词] = NGRAM权重.get(n, NGRAM权重[最大N])
    return 权重


def _ngram长度(词: str) -> int:
    """从词本身判断它是几元 gram。**拉丁词按整词算（n=1）**。"""
    if _全拉丁(词):
        return 1
    return len(词)


#: 切段用的一次性正则。**实测比逐字符循环快 5×**（5000 篇建索引实测：
#: 0.129ms vs 0.026ms 每千字），且结果完全一致——循环是热路径上的浪费。
_切段正则 = re.compile(r"[一-鿿]+|[A-Za-z0-9_.-]+")


def _切段(文本: str) -> list[str]:
    """按汉字串 / 拉丁词切段。标点与空白天然成为分隔符。

    **归类规则**（与正则一字对应）：
    - 连续汉字 → 一个汉字段（切 n-gram）
    - 字母数字与 `_. -` → 一个拉丁段（整词，不切）
    - 其它（标点空白）→ 分隔符，直接丢弃

    `-.` 收进拉丁段是有意的：`GB/T 9704`、`e-mail` 这类**整体专名**
    切碎了反而检索不到。
    """
    return _切段正则.findall(文本)


def _全拉丁(段: str) -> bool:
    return all(c.isascii() and (c.isalnum() or c in "-_.") for c in 段) and bool(段)


def _滑窗(字: list[str], n: int) -> list[str]:
    if len(字) < n:
        return []
    return ["".join(字[i : i + n]) for i in range(len(字) - n + 1)]


def _取首段(文本: str, 上限: int = 200) -> str:
    """降级时只索引开头一段。"""
    return 文本[:上限]
