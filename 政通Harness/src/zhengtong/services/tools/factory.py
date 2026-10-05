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
from pathlib import Path

from ...domain.ids import RunId
from ...domain.role import RoleId
from ...ports.content_pack import ContentPackPort
from ...ports.retriever import RetrieverPort
from ...ports.storage import StoragePort
from ...ports.user_files import UserFilesPort
from ...ports.validators import ValidatorPort
from .audit import 审计器
from .calllog import 调用存档 as 调用存档类
from .handlers import (
    唤醒回调,
    回滚回调,
    工具依赖,
    挂起回调,
    汇报回调,
    状态回调,
    记事件回调,
    请示回调,
    问用户回调,
)
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
    #: **审计落盘路径**（D-258）。不给则审计只在内存，
    #: 进程一结束留痕全丢——架构 §7.4 要求「全部进 journal」。
    审计落盘: Path | None = None,
    记事件: 记事件回调 | None = None,
    # ── 编排回调（第 0 步接；此前一个都没在工厂里露过面，D-286）──
    #
    # **此前 `工具依赖` 上有 7 个回调字段，工厂一个都不传**——
    # 于是它们在真实运行路径上恒为 `None`，全部走 `_未接线`。
    #
    # 后果不是「少个功能」，而是 D-007 的反例：
    # Flash/Pro 要求必须问用户（D-029/D-040），而模型拿到的是
    # 「尚未接线」，**流程带着未填的 D-Card 继续走**——
    # 不报错，但产出建立在空字段上。
    问用户: 问用户回调 | None = None,
    汇报: 汇报回调 | None = None,
    请示: 请示回调 | None = None,
    #: **此前工厂连这个参数都没有**（D-406）：`工具依赖.挂起` 有字段，
    #: `flow.sleep` 处理器有判空与失败返回，**唯独工厂无法注入它**。
    #: 于是无论装配根多努力，`flow.sleep` 必然落到「未接线」——
    #: 看起来接线齐全，实则整条链不存在。
    挂起: 挂起回调 | None = None,
    唤醒: 唤醒回调 | None = None,
    回滚: 回滚回调 | None = None,
    状态: 状态回调 | None = None,
    #: **工具调用存档**（D-406）：`tool.result` 据此查回上一轮的工具输出。
    #: 此前 `工具依赖.调用存档` 恒为 None，故 `tool.result` 永远失败。
    调用存档: 调用存档类 | None = None,
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
            # **发事件的回调**（D-266）：工具调用与拒绝要进 journal，
            # 否则审计条目只留在内存，进程一结束即消失
            # （架构 §7.4「全部进 journal」）。
            记事件=记事件,
            问用户=问用户,
            汇报=汇报,
            请示=请示,
            挂起=挂起,
            唤醒=唤醒,
            回滚=回滚,
            状态=状态,
            调用存档=调用存档,
        ),
        审计=审计器(时钟=审计时钟 or time.monotonic, 落盘=审计落盘),
    )


__all__ = ["RoleId", "工具依赖", "工具运行器", "建工具运行器"]
