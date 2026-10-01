"""唯一装配点。

**全部端口收在一处，且都是注入的。** 这是 D-070 的落点：
接入层不得直连服务——它只能拿 `PortRegistry`，
于是 TUI、无界面、doctor 三个驱动共用同一套装配（D-063），
而测试可以整套换成假实现。

刻意**不含** `Path` 与配置对象：端口的签名里不该出现具体实现类型。
要传路径就用 `str`，要读配置就通过适配层取好的值。
`PortRegistry` 自身是装配容器，不算端口签名的一部分。
"""

from __future__ import annotations

from dataclasses import dataclass

from .clock import ClockPort
from .content_pack import ContentPackPort
from .llm import LlmPort
from .retriever import RetrieverPort
from .secrets import SecretsPort
from .storage import StoragePort
from .user_files import UserFilesPort
from .validators import ValidatorPort


@dataclass(frozen=True, slots=True)
class PortRegistry:
    """全部端口的集合。

    **全字段无默认值**：漏装配一个端口会在构造时就炸，
    而不是在跑到某个分支时才发现。用默认值等于给这种疏漏开门。
    """

    llm: LlmPort
    storage: StoragePort
    user_files: UserFilesPort
    content_pack: ContentPackPort
    retriever: RetrieverPort
    validators: ValidatorPort
    secrets: SecretsPort
    clock: ClockPort

    def missing(self) -> tuple[str, ...]:
        """自检：返回所有 `None` 的端口名。正常装配下应为空元组。"""
        names = (
            "llm",
            "storage",
            "user_files",
            "content_pack",
            "retriever",
            "validators",
            "secrets",
            "clock",
        )
        return tuple(n for n in names if getattr(self, n) is None)
