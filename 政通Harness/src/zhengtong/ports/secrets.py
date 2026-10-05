"""密钥端口。

**唯一允许碰到密钥明文的地方**（D-030）。

API key 可以明文存在 `system/` 下，用户明确接受这个风险——
所以不做加密存储那套自欺欺人的东西。但有一条纪律是硬的：

**密钥值绝不出现在事件、日志、续跑摘要里。**
脱敏在**发射处**做（`kernel/redactor.py`），不依赖消费方自觉——
因为靠自觉的脱敏，第一次忘了就是密钥进日志。

因此本端口是**唯一**返回明文的端口，其他任何地方要密钥都得过这里，
而 `Redactor` 拿到的也只是"需要脱敏的模式"，不是密钥本身。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretsPort(Protocol):
    def get_api_key(self) -> str:
        """取 API key 明文。**仅供适配器构造请求时使用。**"""
        ...

    def redaction_patterns(self) -> tuple[str, ...]:
        """需要脱敏的字面量。

        返回的是**模式**（如密钥前缀片段），不是密钥本身——
        这样 Redactor 拿到的是"怎么脱"，而非"脱什么"，
        它的日志里不可能出现密钥。
        """
        ...
