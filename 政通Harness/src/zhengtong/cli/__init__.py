"""命令行入口。

**放在 `zhengtong.cli` 而不是 `zhengtong.__main__`**：
`pyproject.toml` 的 `[project.scripts]` 早就指向 `zhengtong.cli.main:main`，
而那个模块**从未存在**——装出来的命令一启动就
`ModuleNotFoundError: No module named 'zhengtong.cli'`。

本包**不 import textual**（D-003 / 架构 §15 依赖策略）：
便携运行时必须能在没有 textual 的环境里跑，
故 `--doctor` 走纯标准库路径。
"""

from .main import main

__all__ = ["main"]
