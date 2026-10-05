"""任务输入。

一次运行的全部输入。**用户在开跑前选好档位**（D-030）：
工作量档位由用户自行选择，程序不提供自动判断——
让程序猜"这活儿大概需要多少人"，猜错的后果是用户白等一轮。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .tier import TIER_BY_ID, TierId, TierSpec


@dataclass(frozen=True, slots=True)
class UserFile:
    """`user/` 中一个已登记文件的**元信息**。

    刻意**不含内容**：D-023 要求 AI 初始只拿到文件名，
    内容必须通过工具按需读取。把它放进任务对象等于绕过了那道限制。

    **`rel_path` 为什么必须存在**（第 7 步复查实测）：
    扫描是递归的，两个子目录里各有一个 `通知.docx` 时，
    只用 `name` 会登记出两条**完全相同**的记录，模型无法指定读哪个，
    且按 `name` 找不到任何一份。故 `rel_path`（相对 `user/` 的路径）
    才是**唯一定位依据**；`name` 保留为人类可读的展示名。
    """

    name: str
    size: int
    suffix: str
    #: 相对 `user/` 的路径（POSIX 分隔符，如 `2026年/通知.docx`）。
    #: 定位以它为准，**且同样受路径守卫约束**。
    rel_path: str = ""


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """一次任务。

    `requirement` 是用户的多行输入原文（D-028：不设背景材料区、
    不用 `@` 引用语法），**原样保存**——
    G12 用户输入回看要拿它和成稿逐句对照，改写过的版本会让门禁失效。
    """

    tier: TierId
    requirement: str
    user_files: tuple[UserFile, ...] = ()

    @property
    def tier_spec(self) -> TierSpec:
        return TIER_BY_ID[self.tier]

    @property
    def requires_user_questions(self) -> bool:
        """是否需要弹表单问 D1–D9。Flash 与 Pro 都问全部要素（D-029）。"""
        return self.tier_spec.requires_user_questions


#: 单文件读取上限（D-053），**2 万字**。
#:
#: 定义在 domain 而非各 reader：docx 与 txt/md 若各写各的，
#: 同一决策就有了两个上限——读 txt 会截断而读 docx 不会。
#: 第 7 步复查实测过这个漏洞。
单文件上限: Final[int] = 20_000

#: 支持的输入文件类型（D-024）：不支持 PDF、旧 .doc、图片，不做 OCR。
SUPPORTED_SUFFIXES: Final[frozenset[str]] = frozenset({".txt", ".md", ".docx"})


@dataclass(frozen=True, slots=True)
class 读到的文件:
    """一次 `read_text` 的结果：**内容 + 完整性**。

    ## 为什么不能只返回 `str`

    D-053 是「2 万字截断**+ 事件标明**」——**两条**。
    只返回 `str` 时调用方拿不到「已被截断」这个事实，
    模型会把 2 万字的残缺材料当完整材料用，
    **基于残缺事实做研判**——这是「严禁编造」（D-007）的直接反例。

    不返回 `(text, 是否截断)` 元组，也不加 `read_text_detailed()`：
    那两种都会让调用方**可能忘记看第二个值**。
    做成值对象属性，`已截断` 与内容走同一条路径，取不到内容就取不到标记。

    ## 给模型的文案为什么在这里定

    第 10 步工具层会把这段文字拼进给模型的输出，但**文案必须现在定**——
    内容与「缺了多少」要一起呈现，拆到两处就会出现
    「说了截断却没说原文字数」的半截实现。
    """

    #: 内容。**可能已被截断**——判断依据是 `已截断`。
    text: str
    #: 内容是否因超过单文件上限而被截断。
    已截断: bool
    #: 实际返回的字数。
    完整字数: int
    #: 截断**前**的原始字数。未截断时与 `完整字数` 相等。
    原始字数: int

    def 给模型的提示(self) -> str:
        """给模型看的一句说明。**未截断时返回空串**。

        截断时必须含**原文字数**：只说「已截断」而不说缺了多少，
        模型不知道该不该追问、也不知道该换哪个文件。
        """
        if not self.已截断:
            return ""
        return (
            f"（该文件共 {self.原始字数} 字，"
            f"因单文件 {self.完整字数} 字上限已截断，内容不完整。"
            "如需完整内容请调整材料或分次提供。）"
        )
