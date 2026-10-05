"""配置读写。

## 位置：`system/config.toml`

AI **看不到也改不了** `system/`（D-017），所以配置文件天然是可信输入，
不需要像用户文件那样防篡改。

## 读写分离的依赖（D-079）

- 读：`tomllib` —— Python 3.11 内置，**不装 `tomli`**
- 写：`tomli-w` —— 标准库没有 TOML 写出

这条依赖差是刻意的：断网 Windows 便携包里的依赖越少越好（D-003）。

## 为什么写入要走原子写

配置是启动时读的。若写到一半崩了，下次启动配置就废了，
用户得从头填一遍端点和密钥。原子写让它要么是旧的、要么是新的。

## 密钥在本文件里是明文的（D-032 的实际含义见 §5.3）

用户明确要求 API key 可以明文放在 `system/` 目录里。
**但它绝不能进日志**——那由 `Redactor` 在**发射处**负责（D-030），
不是靠这里"不写"。配置层只负责如实存。
"""

# 本文件放宽 Unknown 类规则，理由同 domain/errors.py 的 D-085：
# `Result.ok(...)` 返回 `Result[Any]`（三方案实测后确认唯一可行解）。
# 只影响静态提示，不影响运行行为。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import tomli_w

from ..domain.errors import ErrorCode, Result
from .atomic import write_atomic
from .paths import SYSTEM, Workspace

#: 配置文件名。**带版本号后缀**是 D-008 的直接体现：
#: 将来格式变了可以并存两个文件，而不是把用户的旧配置直接覆盖掉。
CONFIG_NAME = "config.toml"


@dataclass(frozen=True, slots=True)
class EncodingConfig:
    """产出文件编码（D-044）。

    `gb18030=False` 即 UTF-8，是**默认值**。
    用 GB18030 而非 GBK：GB18030 是超集，生僻字不丢。
    """

    gb18030: bool = False

    def name(self) -> str:
        return "gb18030" if self.gb18030 else "utf-8"


@dataclass(frozen=True, slots=True)
class Config:
    """程序配置。

    **只放运行期必需的项。** 内容包（角色提示词、公文知识）不在这里——
    那是内容包的职责（D-008：软数据与代码平级，各版本化可 diff）。
    """

    encoding: EncodingConfig = field(default_factory=EncodingConfig)
    #: 用户手填的完整请求地址。**程序原样请求，不拼接**（D-004）。
    endpoint: str = ""
    #: 用户手填的模型名。**程序不猜**（D-047）。
    model: str = ""
    #: API key。可明文存（用户要求），但**不得进日志**（D-030）。
    api_key: str = ""
    #: 上下文容量，整数 K 为单位。
    context_k: int = 0
    #: 基准温度（D-066）：0.3，各角色可各自覆盖。
    temperature: float = 0.3

    def validate(self) -> Result[None]:
        """必填项检查。**只查"填了没"，不查"对不对"**——
        端点能不能连、模型存不存在，那是第 6 步连接检测的活。

        在这里做连通性检查会让人以为配置自检过了，实际没测过。
        """
        missing = [
            name
            for name, val in (
                ("endpoint", self.endpoint),
                ("model", self.model),
                ("api_key", self.api_key),
            )
            if not val
        ]
        if missing:
            return Result.fail(
                ErrorCode.CONFIG_INVALID,
                f"配置未填：{'、'.join(missing)}",
            )
        if self.context_k <= 0:
            return Result.fail(
                ErrorCode.CONFIG_INVALID,
                "上下文容量须为正整数（单位 K）",
            )
        return Result.ok_none()

    def to_toml(self) -> dict[str, object]:
        """→ 可写出结构。**分区而非平铺**：
        平铺的话将来加个同名词就会撞车。"""
        return {
            "encoding": {"gb18030": self.encoding.gb18030},
            "llm": {
                "endpoint": self.endpoint,
                "model": self.model,
                "api_key": self.api_key,
                "context_k": self.context_k,
                "temperature": self.temperature,
            },
        }

    @classmethod
    def from_toml(cls, raw: Mapping[str, object]) -> Config:
        """可写出结构 → 配置。**逐字段带类型转换与默认**。

        配置文件是用户手改的，类型写错（`context_k = "128"`）不该崩——
        回落默认并让后续 `validate` 报出"没填"，比抛 TypeError 好。
        """
        enc = _section(raw, "encoding")
        llm = _section(raw, "llm")
        return cls(
            encoding=EncodingConfig(gb18030=bool(enc.get("gb18030", False))),
            endpoint=str(llm.get("endpoint", "")),
            model=str(llm.get("model", "")),
            api_key=str(llm.get("api_key", "")),
            context_k=_as_int(llm.get("context_k"), 0),
            temperature=_as_float(llm.get("temperature"), 0.3),
        )


def config_path(ws: Workspace) -> Path:
    return ws.dir(SYSTEM) / CONFIG_NAME


def load(ws: Workspace) -> Result[Config]:
    """读配置。**文件不存在时返回默认配置，不报错**——
    首次启动本就没有配置，报错只会让用户看到一个看不懂的栈回溯。
    真正"填没填"由 `Config.validate` 在使用时管。"""
    p = config_path(ws)
    if not p.is_file():
        return Result.ok(Config())
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return Result.fail(
            ErrorCode.CONFIG_INVALID,
            f"配置文件读取失败：{exc}",
        )
    return Result.ok(Config.from_toml(raw))


def save(ws: Workspace, cfg: Config) -> Result[None]:
    """写配置。**走原子写**：配置是启动时读的，
    写到一半崩掉会让用户下次启动得从头填。"""
    try:
        text = tomli_w.dumps(cfg.to_toml())
    except (TypeError, ValueError) as exc:
        return Result.fail(ErrorCode.CONFIG_INVALID, f"配置序列化失败：{exc}")
    # 配置路径不经 AI 路径守卫：它在 `system/` 下，
    # 而 AI 根本不该触碰 `system/`。此处是程序自身在写自己的配置。
    return write_atomic(config_path(ws), text)


# ── 取值助手 ──


def _section(raw: Mapping[str, object], name: str) -> dict[str, object]:
    """取一个分区。**不是 dict 就当空**：
    用户手改配置时把 `[llm]` 写成了标量是常见错误。"""
    v = raw.get(name)
    if not isinstance(v, dict):
        return {}
    # 键统一转 str：TOML 的键本就是字符串，但经 `dict` 转换后
    # 静态类型是 object，故显式收窄而不是让类型检查器报未知
    return {str(k): x for k, x in v.items()}


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return _解析整数(value, default)
    return default


#: D-140：容量单位。用户在配置里写 `128k` / `128K` 是极自然的写法
#: （界面上问的就是「上下文容量多少 K」），原实现只认纯数字，
#: 于是静默回落 0，报错却说「须为正整数」——而用户填的 128k
#: **正是一个正整数**，用户只会反复重填而看不出自己多打了 k。
_UNIT_K: Final[re.Pattern[str]] = re.compile(r"^\s*([+-]?\d+)\s*[kK]\s*$")


def _解析整数(value: str, default: int) -> int:
    """字符串 → 整数。**容许 `128k` 这类带单位的写法**（D-140）。

    容许的单位只有 K——用户界面问的就是「多少 K」，
    放行 M/G 只会引入「128M 到底是 128 还是 131072」这种歧义。
    带 K 则无歧义：K 是本配置唯一约定的单位。

    仍读不出就回落默认值，由 `validate` 去报「没填」。
    """
    文本 = value.strip()
    if 文本.lstrip("+-").isdigit():
        return int(文本)
    if m := _UNIT_K.match(文本):
        return int(m.group(1))
    return default


def _as_float(value: object, default: float) -> float:
    """转 float。

    ## 审计 D-139：非有限值必须回落默认

    原实现直接 `float(value)`，`float("NaN")` / `float("inf")` /
    `float("1e999")` 都会成功，于是用户只要在配置里打一个字母
    或一个足够大的数，`temperature` 就变成 NaN 或 inf 并一路流进 API 请求。

    实测后果：`json.dumps({"temperature": nan})` 产出
    `{"temperature": NaN}`——**这不是合法 JSON**，
    多数服务端直接 400 拒绝，且报错与真实原因完全无关，
    用户会以为是模型或端点的问题。
    另一重后果：D-066 规定基准温度 0.3 是为可复现，
    NaN 让可复现彻底失效。

    且 `validate()` 只查必填项与 `context_k`，**从不查 temperature**，
    所以这条路径上没有任何兜底。
    故此处拦住非有限值（`math.isfinite`），回落默认值。
    """
    候选: float
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        候选 = float(value)
    elif isinstance(value, str):
        try:
            候选 = float(value)
        except ValueError:
            return default
    else:
        return default
    return 候选 if math.isfinite(候选) else default
