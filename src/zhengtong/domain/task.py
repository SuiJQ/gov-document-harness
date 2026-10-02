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


#: 支持的输入文件类型（D-024）：不支持 PDF、旧 .doc、图片，不做 OCR。
SUPPORTED_SUFFIXES: Final[frozenset[str]] = frozenset({".txt", ".md", ".docx"})
