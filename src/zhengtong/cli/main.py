"""命令行入口：`zhengtong`。

## 为什么这个文件迟到现在才有

`pyproject.toml` 早就声明了

```toml
[project.scripts]
zhengtong = "zhengtong.cli.main:main"
```

而 `zhengtong.cli` **从未存在**——装出来的命令一启动就
`ModuleNotFoundError`。全仓没有 `__main__.py`、没有 `def main()`，
**没有任何判据测过「能不能启动」**。

这是本项目「东西齐了但没接上」的又一例，也是最严重的一例：
它让 `test_step16_presentation.py` 那 21 条全绿的判据
**证明了一件与「程序可用」无关的事**——三驱动确实同源，
但没有东西把它们摆到程序流程里。

## 为什么不 import textual

D-003 与架构 §15 的依赖策略：便携运行时必须能在**没有 textual**
的环境里跑（断网 Windows 便携 zip、依赖越少越好）。

故本模块**纯标准库**：
`--doctor` 不碰 textual，Textual App 放在
`zhengtong.tui_app` 里由 `--tui` 按需导入——
**让「缺 textual」只影响 TUI，不影响自检**。

## 命令面保持极简

| 命令 | 用途 |
|---|---|
| `--doctor` | 健康自检（探测能不能用，不跑编排） |
| `--tui` | 交互界面（需 textual） |
| `--debug` | 打开调试显示（配合 `--tui`） |

**不设子命令层级**：`zhengtong --doctor` 而不是 `zhengtong doctor check`
——本项目是给非专业用户用的单任务工具，命令面越短越不容易错。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ..adapters.terminal import 查终端显示能力
from ..app import 准备目录, 工作区
from ..domain.tier import TIER_BY_ID, TierId
from ..presentation.doctor import 打印自检, 自检项, 跑自检

#: 中文档位名 → 内部标识。用户输入中文，程序内部用英文（D-008）。
档位别名: dict[str, TierId] = {
    "咨询": TierId.CONSULT,
    "flash": TierId.FLASH,
    "闪": TierId.FLASH,
    "pro": TierId.PRO,
    "普": TierId.PRO,
}

#: 缺配置时用它跑一遍，好让用户**看到缺什么**，而不是只看到一个错误码。
默认档位 = TierId.CONSULT


def 造解析器() -> argparse.ArgumentParser:
    """造命令行解析器。

    **不带参数时显示帮助而不是静默退出**——
    静默退出对用户等于「程序没反应」。
    """
    解析器 = argparse.ArgumentParser(
        prog="zhengtong",
        description="政通 —— 党政公文写作专家团系统",
        epilog="不带参数运行会显示本帮助。",
    )
    解析器.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="工作区根目录（含 system/ data/ user/ 三目录），默认当前目录",
    )
    组 = 解析器.add_mutually_exclusive_group()
    组.add_argument("--doctor", action="store_true", help="健康自检：探测能不能用，不跑编排")
    组.add_argument("--tui", action="store_true", help="交互界面（需要 textual）")
    # **需求与档位不再有命令行参数**（D-294）：这两个原本只服务无界面模式。
    # TUI 在诉求输入屏里收多行需求、并让用户在开始前选定档位
    # （`adapters/tui.py` 的诉求输入屏，D-067 档位是数据），
    # 用户要求本来就是「用多行输入框」与「开始前选择」，不是「敲命令行」。
    # 留着两个零引用的死参数，只会让用户以为还有第二条路。
    #
    # **--debug 保留并接进 TUI**：用户明确「调试模式通过命令行开启」，
    # 它原先只接在无界面驱动上，删掉那条路就断了。
    解析器.add_argument("--debug", action="store_true", help="调试模式：尽可能完整输出")
    return 解析器


def 取档位(原文: str) -> tuple[TierId | None, str]:
    """把用户填的档位解析成 `TierId`。

    **不接受就明说**，不静默回落到默认档——
    用户选了 Pro 却跑成咨询，比报错糟得多（D-082 同理）。
    """
    if not 原文.strip():
        return 默认档位, ""
    键 = 原文.strip().lower()
    命中 = 档位别名.get(键) or 档位别名.get(原文.strip())
    if 命中 is None:
        return None, f"档位「{原文}」不认识。可选：咨询 / flash / pro"
    return 命中, ""


def 本地自检项(根: Path) -> list[自检项]:
    """**不碰网络、不需要凭据**的那几项探测。

    ## 为什么先只做这些

    用户要求自检覆盖网络连通、鉴权、模型调用、工具调用支持、流式能力
    五项（D-024）。那五项**都要真凭据**，而本命令的第一要务是
    「**没有配置时也能给出有用的结论**」——
    一个连工作区都没建起来的程序，不该先问 key。

    故拆两段：本地项永远可跑（这里），远端项在装配成功后跑（`main`）。

    ## 顺带把手写该看的都看了

    目录缺失**当场建回来**（D-017：用户全删过一次之后再启动，
    这里把它们建回来；已存在则绝不删改）。
    """
    项: list[自检项] = []

    建好 = 准备目录(根)
    if 建好.is_error:
        错 = 建好.error
        assert 错 is not None
        项.append(自检项("工作目录", False, 错.detail, "检查目录权限，换一个可写的位置重试"))
        return 项
    项.append(自检项("工作目录", True, "system/ data/ user/ 已就位"))

    配置件 = 根 / "system" / "config.toml"
    样板件 = 根 / "system" / "config.example.toml"
    if 配置件.is_file():
        项.append(自检项("配置文件", True, str(配置件)))
    elif 样板件.is_file():
        项.append(
            自检项(
                "配置文件",
                False,
                "未找到 system/config.toml",
                "复制 system/config.example.toml 为 config.toml 并填写端点与模型名",
            )
        )
    else:
        项.append(
            自检项(
                "配置文件",
                False,
                "未找到 system/config.toml，也没有样板可复制",
                "在 system/ 下建一份 config.toml，字段参照 docs/ARCHITECTURE.md 的 §6",
            )
        )
    return 项


def 远端自检项(根: Path) -> list[自检项]:
    """装配真实端口后探测远端（D-024 五项）。

    **装配失败就把失败原样呈现，不抛异常**——
    自检的核心价值是「不管多坏都要给出结论」。

    目前只报「能不能装配起来」这一项，逐项细分待凭据就位后再补。
    **不做半成品的探测**：宁可少报一项，也不要报一个假的「通过」——
    自检里出现假通过，比缺一项危险得多（D-024「不提供测速按钮」同源）。
    """
    from ..app import build_app_from_workspace

    got = build_app_from_workspace(工作区(根))
    if got.is_error:
        错 = got.error
        assert 错 is not None
        return [自检项("端点装配", False, 错.detail, "按详情补齐配置后重试；程序不会替你猜端点")]
    应用 = got.unwrap()
    try:
        规 = TIER_BY_ID[默认档位]
        return [
            自检项("端点装配", True, f"模型 {应用.config.model}"),
            自检项("专家团", True, f"当前档位「{规.display_name}」含 {len(规.roster)} 位成员"),
        ]
    finally:
        应用.close()


def main(argv: Sequence[str] | None = None) -> int:
    """命令行主入口。**返回退出码**，`sys.exit` 留给最外层。

    ## 退出码约定

    | 码 | 义 |
    |---|---|
    | 0 | 成功 |
    | 1 | 有事项未通过（自检未过，或运行失败） |
    | 2 | 参数用错 |

    **不细分 3+**：退出码是给脚本和 CI 看的，
    细分意义让它们去读文字输出。
    """
    解析器 = 造解析器()
    参数 = 解析器.parse_args(list(argv) if argv is not None else None)

    if not any((参数.doctor, 参数.tui)):
        解析器.print_help()
        return 0

    if 参数.doctor:
        项 = 本地自检项(参数.root)
        项 += 远端自检项(参数.root)
        # **D-045 字形检测在此接上**（B4）。
        # 此前 `跑自检` 有 `字体` 参数、`doctor.py` 有 `字体提示` 字段，
        # 但**没人传值**——实测 `--doctor` 输出里一个字形提示都没有。
        # 管道建好了，实现没接。
        #
        # 为什么由 CLI（装配根）去调：`查终端显示能力` 在 `adapters` 层，
        # 而 `doctor` 在 `presentation` 层，**后者不能 import 前者**（分层判据）。
        # 故在装配根算好、按参数注入——这个参数当初就是为此留的。
        能力 = 查终端显示能力(sys.stdout)
        # **诊断值要一并报出去**，不只是那句提示。
        #
        # 只报「编码写不出字」而不说「当前编码是什么、终端多大」，
        # 用户看到之后仍然不知道该改什么——**报问题不给依据等于没报**。
        # 那四个字段就是为此存在的，不是探测器自娱自乐的产物。
        # **`中文可显示` 也要报**，哪怕它为真——`提示` 只在**出问题**时说话，
        # 于是「通过」这件事在报告里一个字都看不到。
        # 用户跑 `--doctor` 多半是想确认「这台机器行不行」，
        # 沉默不等于通过。
        详情 = (
            f"编码 {能力.编码}　"
            f"汉字与界面符号 {'可正常显示' if 能力.中文可显示 else '无法显示'}　"
            f"终端 {能力.宽度}×{能力.高度}　"
            f"交互终端 {'是' if 能力.是终端 else '否'}"
        )
        结果 = 跑自检(探测=tuple(项), 字体=f"{详情}。{能力.提示}" if 能力.提示 else 详情)
        打印自检(结果)
        return 0 if 结果.全通过 else 1

    if 参数.tui:
        return _进tui(参数)

    解析器.print_help()
    return 2


def _tui缺依赖提示(缺: str) -> str:
    """按缺的是哪个模块给不同的提示（D-027）。

    ## 为什么要分开

    `ModuleNotFoundError` 有两种完全不同的成因：

    - `textual` 没装 → **用户的处置**是 `pip install textual`
    - `zhengtong.tui_app` 没写 → **这是程序自己的缺口**，
      让用户去装 textual 只会让他装完还是同样的错

    我第一版**一律报「textual 没装」**，而本地 textual 明明已装、
    真错是 `tui_app` 还不存在——用户照着提示装一遍，问题原样不变。
    """
    if 缺 == "textual" or 缺.startswith("textual."):
        return (
            "交互界面需要 textual 组件，但当前环境没有装。\n"
            "请在联网环境执行：pip install textual\n"
            "装好后再运行：zhengtong --tui\n"
        )
    return (
        f"交互界面还没接上（缺少模块：{缺}）。\n"
        "这是程序自身尚未完成的部分，不是你的环境问题。\n"
        "当前可用：zhengtong --doctor（健康自检）\n"
    )


def _进tui(参数: argparse.Namespace) -> int:
    """按需导入 Textual App。**缺依赖要说清是环境问题还是程序缺口**（D-027）。

    ## 为什么用 `importlib` 而不是静态 import

    两条理由，第二条是硬的：

    1. **缺依赖要说清是哪种缺**——`importlib` 让我能拿到
       `ModuleNotFoundError.name`，分清「textual 没装」与
       「`tui_app` 还没写」。静态 import 抛出来的信息更少。
    2. **静态 import 会被类型检查器当成欠债**：
       `tui_app` 不存在时 BasedPyright 直接报 `reportMissingImports`，
       于是「TUI 还没做」这个**如实状态**变成了一个类型错误，
       逼着人写 `# type: ignore`（D-018 禁止）
       或者提前造一个空壳模块骗过检查器。

    动态导入让「还没做」可以**如实存在**而不欠债。
    """
    import importlib

    try:
        模块 = importlib.import_module("zhengtong.tui_app")
        启动 = 模块.启动
    except (ModuleNotFoundError, AttributeError) as 错:
        缺 = getattr(错, "name", None) or "zhengtong.tui_app"
        sys.stderr.write(_tui缺依赖提示(缺))
        return 1
    return 启动(参数.root, debug=参数.debug)
