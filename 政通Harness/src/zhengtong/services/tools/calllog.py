"""工具调用存档：给模型查回某次调用结果（第 14.5 步）。

## 为什么要有这个

对话序列里**只记动作不记结果**（用户定）：模型只知道自己做过什么，
结果按需用 `tool.result(call_id)` 查回。

全记则上下文被材料正文撑爆；全不记则模型不知道自己查过什么，会重复查询。

## 三份存储必须分开

| | 读者 | 内容 | 寿命 |
|---|---|---|---|
| 审计条目 | 人 / 排障 | **只有摘要** | 写 journal |
| **调用存档** | **模型查回** | 完整结果 | **只在内存** |
| 对话序列 | 模型 | 只有动作 | 本轮运行 |

并进审计条目就等于「把材料正文抄进日志」，违反 D-035；
用户已定：只放内存，不落盘——落盘会把一个查回的便利
变成新的产物生命周期管理问题（何时清理、算不算 D-038 交付物、断点续跑要不要带）。

## 查不到就说查不到

`取()` 查不到时返回 `None` 而非空串。返回空串的话，
模型会以为「我查过了，确实没有」，于是不再重查——
那是编造证据的另一种形态，与 D-218「编造即红线」同源。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock


@dataclass(slots=True)
class 调用存档:
    """`call_id → 完整结果`。**只在内存**，随进程消失。

    用锁保护：工具调用可能来自不同线程（D-049 并行取证）。
    """

    _数据: dict[str, str] = field(default_factory=dict)
    _锁: Lock = field(default_factory=Lock)

    def 记(self, call_id: str, 结果: str) -> None:
        with self._锁:
            self._数据[call_id] = 结果

    def 取(self, call_id: str) -> str | None:
        """取回某次调用的完整结果。**查不到返回 None**，不返回空串。"""
        with self._锁:
            return self._数据.get(call_id)

    def __len__(self) -> int:
        with self._锁:
            return len(self._数据)
