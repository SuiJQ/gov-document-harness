"""PyInstaller 入口包装器。

`zhengtong/__main__.py` 用相对导入 `from .cli.main import main`，
PyInstaller 直接执行它时没有父包上下文，会报
`ImportError: attempted relative import with no known parent package`。

这个文件作为 Analysis 的入口点，先导入 zhengtong 包建立上下文，
再调用 cli.main.main，绕开相对导入问题。
"""

from __future__ import annotations

import sys

from zhengtong.cli.main import main

if __name__ == "__main__":
    sys.exit(main())