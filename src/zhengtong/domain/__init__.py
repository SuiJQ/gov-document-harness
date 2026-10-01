"""L0 领域层：零第三方依赖的纯数据类型。

本层是整个系统的底座，向上只暴露不可变值对象与稳定错误码。
**不得**导入 adapters / services / orchestration / interfaces 任何内容（D-070），
**不得**导入任何第三方库（判据：只用标准库）。
"""

from __future__ import annotations

__all__ = ["SCHEMA_VERSION"]

# 持久化状态结构版本（D-066）。改动 RunState 的字段时 +1，
# 且必须提供从上一版本到本版本的迁移链，不得静默丢字段。
SCHEMA_VERSION = 1
