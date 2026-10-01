"""发射处脱敏（架构 §5.3，D-030）。

**为什么脱敏必须在发射处，不能在显示端**：日志、投影卡、续跑摘要
各有各的出口，一个漏掉就是密钥落盘。显示端过滤是"每个出口自己记得脱"，
发射处脱敏是"出去的东西本来就是干净的"——后者不会漏。

**为什么密钥可以明文存盘，却绝不能进日志**（D-030）：
用户在知情下接受 `system/` 里的明文 key，但日志会被贴给别人看、
会被翻出来做故障分析，传播范围远大于配置文件。
这两件事的风险等级不同，所以处理方式也不同。

**这里拿到的不是密钥本身，是"怎么脱"**（`ports.secrets` 的设计）：
`Redactor` 持有的是模式（片段），不是完整密钥全文。
它自己的日志里也不可能出现密钥。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Final, cast

from ..domain.event import Event

#: 脱敏后的替换文本。刻意写成可检索的固定串——
#: 万一真的漏了，一眼能看出来"这里本该有内容却是空的"，
#: 而不是一个看起来正常的空字符串。
REDACTED: Final[str] = "[已脱敏]"

#: 常见密钥形态的兜底模式。**不依赖具体密钥**，
#: 所以即便 `SecretsPort` 没给出模式，这些也照样拦得住。
#:
#: 这是第二道防线。第一道是 SecretsPort 提供的精确模式
#: （能认出用户自己那个 key，无论它长什么样）。
_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # sk- 开头的 OpenAI 风格 key
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    # Bearer 令牌
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    # 裸长十六进制串（可能是 key 或哈希），要求足够长以免误伤哈希指纹
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    # key/secret/token + 赋值
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*\S+"),
)


@dataclass(frozen=True, slots=True)
class Redactor:
    """把密钥从任意文本与事件里摘掉。

    `extra_patterns` 来自 `SecretsPort.redaction_patterns()`——
    即用户自己那个 key 的片段。**不持有密钥全文**，
    只持有足以定位它的片段。
    """

    extra_patterns: tuple[str, ...] = ()

    def scrub_text(self, text: str) -> str:
        """脱敏一段文本。"""
        for literal in self.extra_patterns:
            if literal and literal in text:
                text = text.replace(literal, REDACTED)
        for pattern in _PATTERNS:
            text = pattern.sub(REDACTED, text)
        return text

    def scrub(self, event: Event) -> Event:
        """脱敏一个事件，返回**新**事件。

        连 `data` 里的嵌套结构一起处理：`data` 的值类型是 `object`，
        可能是 str、list、dict，光处理顶层等于没处理——
        密钥十有八九藏在嵌套里（比如请求体、日志片段）。
        """
        return replace(event, data=self.scrub_value(event.data))

    def scrub_value(self, value: object) -> object:
        """递归脱敏任意 JSON 兼容结构。

        每个分支都给 `isinstance` 标注参数类型（`dict[object, object]` 等）——
        不标则收窄后仍是 `object`/`Unknown`，递归调用处全是警告。
        运行时是否合法由 Python 保证（D-086：3.11 下
        `isinstance` 传参数化泛体会抛 TypeError——实测确认，
        故此处用中间变量标注，不用 `isinstance(x, dict[...])`）。
        """
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, dict):
            mapping = cast("dict[object, object]", value)
            return {str(k): self.scrub_value(v) for k, v in mapping.items()}
        if isinstance(value, tuple | list):
            seq = cast("tuple[object, ...] | list[object]", value)
            return [self.scrub_value(v) for v in seq]
        return value
