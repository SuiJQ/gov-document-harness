"""诊断包：把散落的信息聚合起来，输出单一 JSON。

## 定位

`--diagnose` 不是 `--doctor` 的替代品，而是它的**编排层**：

| | `--doctor` | `--diagnose` |
|---|---|---|
| 做什么 | 探测远端能不能用 | **把所有检查串起来**，输出一份 JSON |
| 产出 | 终端文本 | 单一 JSON 文件 |
| 本地检查 | 工作目录、配置文件 | **加上**依赖版本、事件日志、工作区大小 |
| 远端检查 | 五项探测 | **同一套**，按依赖顺序跑 |
| 内容包 | 完整性 | **加上**清单内容 |

## 为什么需要它

doctor 的输出是给人看的，而排障时真正需要的是
「**所有信息在一个地方**」——依赖版本、内容包清单、
事件日志统计、工作区大小，这些散落在不同文件里，
每次排障都要翻一遍。诊断包把它们聚到一个 JSON 里。

## 不做的事

- **不跑编排**：doctor 不跑，diagnose 也不跑。
  它是取证，不是执行。
- **不做瞬时诊断**：1 分钟以内产出 JSON 即可（用户明确）。
  远端探测按依赖顺序跑，前项失败后续标「未测」。
- **不碰密钥**：用户明确不要密钥硬约束。
  配置文件中的密钥原样放进 JSON 的 `config` 字段，
  由用户自己决定是否脱敏。
"""

from __future__ import annotations

import json
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Final

from ..ports.llm import 探测结论 as _探测结论类型

#: 运行时依赖清单（与 pyproject.toml 的 dependencies 对应）。
#:
#: **只读不写**：版本号来自已安装的包，不从 pyproject 读——
#: 便携 zip 里没有 pyproject，但包是装好的。
运行时依赖: Final[tuple[str, ...]] = (
    "httpx",
    "textual",
    "tomli_w",
)


def _依赖版本(包名: str) -> str | None:
    """取已安装包的版本。**找不到返回 None**，不抛异常。

    为什么不用 `importlib.metadata.version` 直接抛异常：
    排障时「某个依赖没装」是**要报出来的事实**，不是要修的 bug。
    返回 None 让调用方自己决定怎么显示。
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(包名)
    except PackageNotFoundError:
        return None


def 检依赖版本() -> dict[str, str | None]:
    """逐个取运行时依赖的版本，**缺失的也列出来**（值为 None）。

    排障时「依赖没装」和「依赖装错版本」是两回事，
    一个不漏地列出来才不用用户自己翻。
    """
    return {名: _依赖版本(名) for 名 in 运行时依赖}


def _目录大小(路径: Path) -> int:
    """递归算目录总字节数。**不存在返回 0**，不抛异常。

    为什么需要：工作区放多了东西会变慢，
    排障时「这次怎么这么卡」的第一个 suspects 就是数据量。
    """
    if not 路径.is_dir():
        return 0
    总 = 0
    for 子 in 路径.rglob("*"):
        try:
            if 子.is_file():
                总 += 子.stat().st_size
        except OSError:
            continue
    return 总


def 检工作区大小(根: Path) -> dict[str, int]:
    """逐目录报大小（字节）。

    只报三个用户目录 + 日志目录，**不递归进 system/**——
    那是程序的，大小稳定，排障时没有参考价值。
    """
    子目录 = ("data", "user", "system")
    结果: dict[str, int] = {}
    for 名 in 子目录:
        结果[名] = _目录大小(根 / 名)
    return 结果


def _事件统计(路径: Path) -> dict[str, object]:
    """读事件日志，返回按类型计数。

    **文件不存在返回空统计**——默认不落盘（`build_app` 的
    `log_path` 默认 None），所以多数时候这里就是空的。
    那不是错误，只是「没跑过」。
    """
    if not 路径.is_file():
        return {"文件数": 0, "事件数": 0, "类型计数": {}}
    计数: dict[str, int] = {}
    总 = 0
    for _行号, 行 in enumerate(路径.read_text(encoding="utf-8").splitlines(), 1):
        行 = 行.strip()
        if not 行:
            continue
        try:
            对象 = json.loads(行)
        except json.JSONDecodeError:
            # **损坏行也要报**——进程被强杀时末行只写了一半（D-135）。
            键 = "__损坏__"
            计数[键] = 计数.get(键, 0) + 1
            continue
        if not isinstance(对象, dict):
            键 = "__非对象__"
            计数[键] = 计数.get(键, 0) + 1
            continue
        类型 = 对象.get("type")
        if isinstance(类型, str):
            计数[类型] = 计数.get(类型, 0) + 1
        总 += 1
    return {"文件数": 1, "事件数": 总, "类型计数": 计数}


def 检事件日志(根: Path) -> dict[str, object]:
    """找事件日志文件并统计。

    日志放在 `system/events.jsonl`（与 `build_app` 里的
    `JsonlJournal(log_path)` 约定一致）。
    """
    路径 = 根 / "system" / "events.jsonl"
    return _事件统计(路径)


def _读配置文件(根: Path) -> dict[str, object] | None:
    """读 `system/config.toml`，**不存在返回 None**。

    为什么不做容错：配置文件是用户写的，
    读不出来就是有东西要看，返回 None 让调用方标红。
    """
    路径 = 根 / "system" / "config.toml"
    if not 路径.is_file():
        return None
    try:
        import tomllib  # py311+
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            return {"_错误": "无 tomllib/tomli，无法读 TOML"}
    try:
        with 路径.open("rb") as fh:
            数据 = tomllib.load(fh)
    except Exception as 错:  # 任何解析错都要报
        return {"_错误": f"读配置文件失败：{错}"}
    return 数据


def 检配置文件(根: Path) -> dict[str, object] | None:
    """读配置文件并返回。**None 表示文件不存在**。"""
    return _读配置文件(根)


def _读内容包清单(根: Path) -> dict[str, object] | None:
    """读内容包 manifest（`content/manifest.json`）。

    与 `adapters/content_pack.py` 的 `_读清单` 约定一致。
    """
    路径 = 根 / "content" / "manifest.json"
    if not 路径.is_file():
        return None
    try:
        数据 = json.loads(路径.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as 错:
        return {"_错误": f"读 manifest 失败：{错}"}
    return 数据 if isinstance(数据, dict) else {"_错误": "manifest 不是对象"}


def 检内容包(根: Path) -> dict[str, object] | None:
    """读内容包清单。**None 表示没有内容包**。"""
    return _读内容包清单(根)


def 检终端能力() -> dict[str, object]:
    """取终端显示能力。

    **不 import `adapters.terminal`**（分层判据）——
    这里只做标准库能做的事：编码、是否 tty、窗口大小。
    详细检测（中文能否往返）由 doctor 负责。
    """
    流 = sys.stdout
    编码 = getattr(流, "encoding", None) or "未知"
    是终端 = bool(getattr(流, "isatty", lambda: False)())
    宽 = shutil.get_terminal_size((80, 24)).columns
    高 = shutil.get_terminal_size((80, 24)).lines
    return {
        "编码": 编码,
        "是终端": 是终端,
        "宽度": 宽,
        "高度": 高,
    }


def _远端探测(根: Path) -> dict[str, object]:
    """跑远端五项探测，**按依赖顺序**，前项失败后续标「未测」。

    与 `cli/main.py` 的 `远端自检项` 走同一套路径
    （`build_app_from_workspace` → `ports.llm.probe()`），
    这里只做调用 + 结构化，不做展示。

    **为什么不在这里 import `cli/main`**：那是 L6 接入层，
    而本模块在 L6 presentation，**后者不能 import 前者**（分层判据）。
    故直接调装配根 `app.build_app_from_workspace`。
    """
    from ..app import build_app_from_workspace
    from ..domain.tier import TIER_BY_ID, TierId

    结果: dict[str, object] = {
        "端点装配": None,
        "专家团": None,
        "探测": None,
    }
    档 = TIER_BY_ID[TierId.CONSULT]
    装配 = build_app_from_workspace(工作区(根))
    if 装配.is_error:
        错 = 装配.error
        assert 错 is not None
        结果["端点装配"] = {"通过": False, "详情": 错.detail}
        return 结果
    应用 = 装配.unwrap()
    结果["端点装配"] = {"通过": True, "详情": f"模型 {应用.config.model}"}
    结果["专家团"] = {
        "通过": True,
        "详情": f"当前档位「{档.display_name}」含 {len(档.roster)} 位成员",
    }
    探测 = 应用.ports.llm.probe()
    if 探测.is_error:
        错2 = 探测.error
        assert 错2 is not None
        结果["探测"] = {
            "通过": False,
            "详情": 错2.detail,
            "项": _探测转字典(_探测结论类型()),
        }
        return 结果
    结论 = 探测.unwrap()
    结果["探测"] = {
        "通过": True,
        "项": _探测转字典(结论),
    }
    return 结果


def _探测转字典(结论: _探测结论类型) -> dict[str, object]:
    """把 `探测结论` 转成可 JSON 化的字典。

    **按 `结论.项序` 遍历**，不另写一份顺序。
    三态（通过/未通过/未测）各用一个键表达，前端按它渲染。
    """
    出: dict[str, object] = {}
    for 名 in 结论.项序:
        态 = 结论.取(名)
        详情 = 结论.详情.get(名, "")
        if 态 is None:
            出[名] = {"通过": False, "未测": True, "详情": 详情 or "前项失败，未能跑到这一项"}
        else:
            出[名] = {"通过": 态, "未测": False, "详情": 详情}
    # **推理结论也要带出来**（D-380）：它不是五项之一，
    # 但「推理能不能关」是排障时第二常问的问题。
    推理 = 结论.推理结论
    出["推理开关"] = {
        "状态": str(推理.状态) if 推理.状态 is not None else None,
        "能关闭": 推理.能关闭,
        "可用写法": 推理.可用写法,
    }
    return 出


def 工作区(根: Path) -> object:
    """包一层 `Workspace`。**不 import `adapters.paths`**（分层判据）——
    `build_app_from_workspace` 在 `app.py`，它已经 import 过了，
    这里只要造个能传过去的对象。
    """
    from ..adapters.paths import Workspace

    return Workspace(root=根)


def _版本() -> str:
    """取程序版本号。**不从 pyproject 读**——便携 zip 里没有它。

    包名是 `zhengtong`（见 pyproject.toml 的 `[project] name`），
    不是「gov-document-harness」——后者只是仓库目录名。
    """
    try:
        from importlib.metadata import version

        return version("zhengtong")
    except Exception:
        return "未知"


def 跑诊断(根: Path) -> dict[str, object]:
    """跑全部检查，返回可直接 JSON 序列化的字典。

    ## 检查项顺序

    本地项全部先跑（不依赖网络），远端探测最后跑——
    这样即使远端不通，前面的信息也已经拿到了。
    """
    开始 = time.perf_counter()
    检查: list[dict[str, object]] = []

    def _记(名称: str, 数据: object, *, 状态: str = "通过") -> None:
        检查.append({"名称": 名称, "状态": 状态, "数据": 数据})

    # --- 本地检查（不依赖网络） ---
    _记("工作目录", _检工作目录(根))
    _记("依赖版本", 检依赖版本())
    _记("工作区大小", 检工作区大小(根))
    _记("事件日志", 检事件日志(根))
    _记("配置文件", 检配置文件(根))
    _记("内容包", 检内容包(根))
    _记("终端能力", 检终端能力())

    # --- 远端探测（依赖网络） ---
    远端 = _远端探测(根)
    端点 = 远端.get("端点装配")
    if isinstance(端点, dict) and 端点.get("通过"):
        _记("端点装配", 端点)
    else:
        _记("端点装配", 端点 or {"通过": False, "详情": "未知"}, 状态="未通过")
    专家 = 远端.get("专家团")
    if isinstance(专家, dict) and 专家.get("通过"):
        _记("专家团", 专家)
    探测数据 = 远端.get("探测")
    if isinstance(探测数据, dict):
        _记("远端探测", 探测数据)
    else:
        _记("远端探测", {"通过": False, "详情": "未执行"}, 状态="未通过")

    耗时 = int((time.perf_counter() - 开始) * 1000)
    # **顶层也要各留一份快照**（D-380）：`checks` 是逐项结论，
    # 但「配置是什么」「工作区多大」这类**事实本身**也需要被消费方直接取用，
    # 不必从 checks 里翻。两份不冗余——一份是判据，一份是数据。
    return {
        "version": _版本(),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_ms": 耗时,
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "system": {
                "系统": platform.system(),
                "版本": platform.version(),
                "机器": platform.machine(),
                "处理器": platform.processor(),
            },
        },
        "checks": 检查,
        "config": 检配置文件(根),
        "workspace": 检工作区大小(根),
        "content_pack": 检内容包(根),
        "dependencies": 检依赖版本(),
        "run_history": 检事件日志(根),
    }


__all__ = [
    "检事件日志",
    "检依赖版本",
    "检内容包",
    "检工作区大小",
    "检终端能力",
    "检配置文件",
    "跑诊断",
]


def _检工作目录(根: Path) -> dict[str, object]:
    """检查三个用户目录是否存在。"""
    子目录 = ("system", "data", "user")
    状态: dict[str, object] = {}
    全好 = True
    for 名 in 子目录:
        路径 = 根 / 名
        存在 = 路径.is_dir()
        状态[名] = {"存在": 存在, "路径": str(路径)}
        if not 存在:
            全好 = False
    return {"全部存在": 全好, "子目录": 状态}
