"""工具运行器的构造入口。**接线在这里，不在别处**（D-195）。

## 为什么需要这个文件

第 10 步交付后核实发现：`services/tools/` 建好了、16 个工具全测过，
但 `src/` 里**没有任何一处引用它**——零引用。
没有入口就没有「真实的模型调用能调到工具」这回事。

这是 **D-165 的第三次**：第 6 步（假 Llm）、第 9 步附近（假检索）、第 10 步（整个工具层）。
故本文件的第一要务不是「提供便利」，而是**让真配置必定接到真工具层**。

## 铁律

**`建工具运行器` 永远接真适配器，不接假实现。**

与 `build_app_from_workspace` 里 `if llm is None: llm = HttpLlm(...)`
是同一条规矩：不传就接真的，绝不「默认给个假货」。
否则用户配好端点、跑起来一个工具都调不到，而测试照样全绿。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ...domain.ids import RunId
from ...domain.role import RoleId
from ...ports.content_pack import ContentPackPort
from ...ports.retriever import RetrieverPort
from ...ports.storage import StoragePort
from ...ports.user_files import UserFilesPort
from ...ports.validators import ValidatorPort
from .audit import 审计器
from .handlers import 工具依赖
from .runner import 工具运行器


def 建工具运行器(
    *,
    storage: StoragePort,
    user_files: UserFilesPort,
    retriever: RetrieverPort,
    content_pack: ContentPackPort,
    validators: ValidatorPort,
    run_id: RunId,
    审计时钟: Callable[[], float] | None = None,
) -> 工具运行器:
    """造一个工具运行器。

    **端口一律用协议类型**（`StoragePort` 等），不写死实现类型——
    与 `build_app` 同一条规矩：端口的意义就是「实现可换」，
    签名写死实现等于废掉端口（第 5 步实测：签名写死
    `FakeSecrets | None`，结果 `LocalSecrets` 根本传不进去）。

    `审计时钟` 是测试注入口：真实时间戳无法写确定性断言。
    """
    return 工具运行器(
        依赖=工具依赖(
            storage=storage,
            user_files=user_files,
            retriever=retriever,
            content_pack=content_pack,
            validators=validators,
            run_id=run_id,
        ),
        审计=审计器(时钟=审计时钟 or time.monotonic),
    )


__all__ = ["RoleId", "工具依赖", "工具运行器", "建工具运行器"]
