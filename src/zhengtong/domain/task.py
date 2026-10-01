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
    """

    name: str
    size: int
    suffix: str


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
