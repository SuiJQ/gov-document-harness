"""提示词装配（架构 §7.1，第 11 步）。

## 拼五块

```
角色提示词（内容包）
  + 按需装载的规则条目（内容包 references）
  + D-Card 事实块
  + 产物契约（**引用**，不是全文）
  + 工具清单（本次实际可调子集）
  → 最终 prompt
```

## 三条纪律

1. **规则按需装载，不常驻**（D-025 分层装载）——省上下文预算
2. **产物只传引用不传全文**（D-025 结果直传，D-030/D-072）——
   要内容自己 `shared.read`，避免同一份大纲在五个角色上下文里各存一份
3. **预算在装载时就扣减**（D-189）——不是装完发现装不下

## 纪律 3 为什么是这个模块的活

D-053 管「单文件别读太多」（存储层，按字符）；
D-189 管「这一轮总共装多少」（装配层，按 token）。
两者各管一段，缺一不可：只有 D-053 时，五份材料能一次性塞满上下文，
而 compactor 唯一的杠杆是丢**对话历史**——
体量若主要来自材料，丢历史解决不了问题，`压缩()` 返回 `None`，这一轮白跑。

Codex 的做法是装载时逐条扣减 `remaining`，
**永远不会出现「装进去才发现装不下」**。本模块照此办理。

## 本模块不做什么

- **不硬编码任何公文内容**（D-055）：规则条目的名字与装载条件
  从内容包 `manifest.json` 读，代码只决定**何时**装。
- **不决定流程**：装不装某条目是内容包声明的策略，
  而「这一轮该不该用这条规则」需要 D-Card 与阶段上下文，本模块拿不到也不该猜。
- **不裁剪对话窗口**：那是 compactor 的活（第 9 步）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from ...domain.dcard import DCard, Provenance
from ...domain.role import RoleId
from ...ports.content_pack import ContentPackPort
from ...ports.llm import Message
from ..context.estimate import 估算文本
from ..tools.registry import 取工具, 该角色可调
from ..tools.spec import ToolSpec

#: 截断后必须留够的字数——标「已截断」提示本身也要占位置，
#: 截到 0 就等于没截。
_截断留白: Final = 40


@dataclass(frozen=True, slots=True)
class 装载条件:
    """一条规则条目的装载条件。**声明在内容包里，此处只执行。**

    刻意不放 `prompt` 字段：那会变成「装载条件与内容不在一处」，
    改条件时容易漏改正文位置。
    """

    ref: str
    描述: str
    #: 主体数 ≥ 本值时装载。0 表示无条件装载（如红线 E1–E14）。
    最少主体数: int = 0
    #: 只在这些角色装载；空表示全体。
    限角色: frozenset[RoleId] = frozenset()


@dataclass(frozen=True, slots=True)
class 装配请求:
    """装配输入。**全部显式**——不读环境、不读全局状态。"""

    角色: RoleId
    卡: DCard | None = None
    主体数: int = 0
    产物: tuple[str, ...] = ()
    #: 产物全文。**装配器读它只是为了证明「不该用」**——
    #: 真值不进 prompt。真要读内容用 `shared.read` 工具。
    产物全文: dict[str, str] = field(default_factory=dict)
    #: 本次可调工具名；不传则按角色白名单算。**不是全集。**
    工具: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class 预算闸门:
    """本轮 prompt 的 token 上限（D-189）。

    **`上限` 是装配层的闸门**，与 D-053 的单文件字符上限不是一回事：
    那个管「一个文件别读太多」，这个管「这一轮总共装多少」。
    """

    上限: int

    def __post_init__(self) -> None:
        if self.上限 <= 0:
            raise ValueError("预算上限必须为正数")


@dataclass(frozen=True, slots=True)
class 装配结果:
    """装配产物。**占用与截断都可测**——第 11 步判据就落在这里。"""

    角色: RoleId
    提示词正文: str
    消息: tuple[Message, ...]
    #: 本次实际装载的条目 ref，可审计。
    已装条目: tuple[str, ...] = ()
    #: 整轮占用（token 估算）。
    占用: int = 0
    #: 分块占用，用来回答「是谁超了」。
    _分块: tuple[tuple[str, int], ...] = ()
    #: 被截断的块名。**空元组表示没截**——不能靠正文里有没有
    #: 「已截断」字样来判断，那样截断信息与内容混在一起、无法审计。
    被截块: tuple[str, ...] = ()

    @property
    def 事实块占用(self) -> int:
        return dict(self._分块).get("D-Card 事实块", 0)

    def 分块占用(self) -> dict[str, int]:
        return dict(self._分块)

    @property
    def 可用余量(self) -> int:
        """**预算没存进结果**——本类不记预算，避免两处真相。
        调用方拿 `预算闸门.上限 - 结果.占用` 即可，不必在此再开一个字段。"""
        raise NotImplementedError("占用比例请用「闸门.上限 - 装配结果.占用」自算，本类不记预算")


def 装配提示词(
    *,
    请求: 装配请求,
    内容包: ContentPackPort,
    闸门: 预算闸门,
    条件表: tuple[装载条件, ...] | None = None,
) -> 装配结果:
    """拼出最终 prompt。**纯函数**：同样输入必得同样输出。

    纯函数是 D-008「可 diff」的前提——装配结果要能逐字节比对，
    才能看出「改一句提示词，装配后变成什么样」。
    """
    块: list[tuple[str, str]] = []

    角色正文 = _取角色提示词(请求.角色, 内容包)
    块.append(("角色提示词", 角色正文))

    条件 = 条件表 if 条件表 is not None else 默认装载条件()
    已装: list[str] = []
    for 条 in 条件:
        if not _该装(条, 请求):
            continue
        正文 = _取参考(条.ref, 内容包)
        if 正文 is None:
            continue
        已装.append(条.ref)
        块.append((f"规则条目 · {条.ref}", 正文))

    事实 = _渲染事实块(请求.卡)
    if 事实:
        块.append(("D-Card 事实块", 事实))

    契约 = _渲染产物契约(请求.产物)
    if 契约:
        块.append(("产物契约", 契约))

    工具块 = _渲染工具清单(请求)
    if 工具块:
        块.append(("工具清单", 工具块))

    正文, 分块, 被截 = _按预算截(块, 闸门)

    return 装配结果(
        角色=请求.角色,
        提示词正文=正文,
        消息=(Message(role="system", content=正文),),
        已装条目=tuple(已装),
        占用=分块.pop("_合计"),
        _分块=tuple(分块.items()),
        被截块=tuple(被截),
    )


def _取角色提示词(角色: RoleId, 内容包: ContentPackPort) -> str:
    got = 内容包.load_role_prompt(角色)
    if got.is_error:
        码 = got.error.code if got.error else "内容包读取失败"
        raise ValueError(f"角色 {角色.value} 提示词读取失败：{码}")
    return got.unwrap()


def _取参考(ref: str, 内容包: ContentPackPort) -> str | None:
    got = 内容包.load_reference(ref)
    return None if got.is_error else got.unwrap()


def _该装(条: 装载条件, 请求: 装配请求) -> bool:
    """条件不满足就不装。**省下的每一 token 都是真省下来的**。"""
    if 条.限角色 and 请求.角色 not in 条.限角色:
        return False
    return 请求.主体数 >= 条.最少主体数


def _渲染事实块(卡: DCard | None) -> str:
    """D-Card → 事实块。

    **未填字段必须写「待填」而非省略**（D-034）：
    省略等于隐瞒，模型看不到 D2 缺了，就可能自己编一个配套件。
    缺就是缺，写出来——这是「严禁编造」在装配层的落地。

    `LEAD_INFERRED` 的字段必须标明是**研判**而非用户提供（D-069）：
    模型据此调整语气，但它自己也得知道这条不是用户拍板的。
    """
    if 卡 is None:
        return ""
    行: list[str] = []
    for fid, f in sorted(卡.by_id.items(), key=lambda kv: kv[0].value):
        if f.is_filled:
            值 = (
                f"{f.value}（{fid.value} 范正章研判，非用户提供）"
                if (f.provenance is Provenance.LEAD_INFERRED)
                else f"{f.value}"
            )
        else:
            值 = f"【待填 · {fid.value}】"
        行.append(f"- {fid.value} {f.label}：{值}")
    return "已确认的事实（**待填项不得自行编造**，须向用户询问）：\n" + "\n".join(行)


def _渲染产物契约(产物: tuple[str, ...]) -> str:
    """产物**只列引用**，绝不带正文（D-030/D-072）。"""
    if not 产物:
        return ""
    清单 = "\n".join(f"- {名}" for 名 in 产物)
    return (
        f"上游产物（共 {len(产物)} 份，**此处只有引用，正文不在上下文里**）：\n"
        f"{清单}\n"
        "需要正文时用 shared.read 按名称读取，不要凭文件名猜测内容。"
    )


def _渲染工具清单(请求: 装配请求) -> str:
    """本轮实际可调子集。**不传则按角色白名单算，不是全集**。"""
    清单 = 该角色可调(请求.角色) if 请求.工具 is None else tuple(_校验工具(名) for 名 in 请求.工具)
    if not 清单:
        return ""
    行 = [f"- {t.name}：{t.description}" for t in 清单]
    return "本次可调用的工具（**调不到的工具不会出现在这里**）：\n" + "\n".join(行)


def _校验工具(名: str) -> ToolSpec:
    规 = 取工具(名)
    if 规 is None:
        # 工具名可能来自配置，**报错而非静默跳过**：
        # 静默跳过会让工具凭空消失，而用户查不出原因。
        raise ValueError(f"不存在的工具：{名}")
    return 规


def _按预算截(块: list[tuple[str, str]], 闸门: 预算闸门) -> tuple[str, dict[str, int], list[str]]:
    """逐块装入，**装到预算用尽为止**（D-189）。

    ## 为什么按块而不是按字符切

    按字符切会拦腰截断一条规则的中段，
    模型读到半句「以上要求适用」根本不知道适用什么。
    按块截要么整块进、要么整块出，最坏情况是少一条规则——
    **少一条会被发现，断一条不会**。

    ## 为什么必须标明

    静默截断：模型不知道内容缺了，会拿残缺的规则去推理。
    硬塞：预算形同虚设（第 9 步的教训）。

    ## 角色提示词永不截

    它是五块里唯一的身份来源，截了模型就不是这个角色了。
    预算再紧也得留它——真装不下是**上游该报错的事**（编排层的事）。
    """
    分块: dict[str, int] = {}
    入选: list[tuple[str, str]] = []
    被截: list[str] = []
    已用 = 0

    for 名, 正文 in 块:
        代价 = 估算文本(正文)
        装得下 = 名 == "角色提示词" or 已用 + 代价 <= 闸门.上限
        if not 装得下:
            被截.append(名)
            continue
        分块[名] = 代价
        入选.append((名, 正文))
        已用 += 代价

    正文 = _拼接(入选, 被截)
    分块["_合计"] = 估算文本(正文)
    return 正文, 分块, 被截


def _拼接(入选: list[tuple[str, str]], 被截: list[str]) -> str:
    段 = [f"## {名}\n{正文}" for 名, 正文 in 入选]
    if 被截:
        段.append(
            "## 预算不足，以下内容未装入\n"
            + "\n".join(f"- {名}（**已截断**，不是不存在）" for 名 in 被截)
            + "\n预算见本轮上下文配置；若内容必需，用对应工具按需读取。"
        )
    return "\n\n".join(段)


#: 默认装载条件。**条件表是本模块唯一的策略入口**——
#: 内容包可整体替换（换内容包即换一套写作标准），
#: 代码不得硬编码条目清单（D-055）。
def 默认装载条件() -> tuple[装载条件, ...]:
    return (
        装载条件(
            ref="error-memory",
            描述="红线抑制清单 E1-E14（判定存疑或用户质询时读）",
            最少主体数=0,
        ),
        装载条件(
            ref="admin-ordering-rules",
            描述="行政主体排序规则库",
            最少主体数=3,
        ),
        装载条件(
            ref="gbt9704-format",
            描述="GB/T 9704-2012 版式规范",
            限角色=frozenset({RoleId.COMPLIANCE}),
        ),
        装载条件(
            ref="punctuation",
            描述="标点与序号规范",
            限角色=frozenset({RoleId.LANGUAGE, RoleId.COMPLIANCE}),
        ),
    )
