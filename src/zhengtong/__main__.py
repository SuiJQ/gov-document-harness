"""包级入口：`python -m zhengtong`。

## 为什么需要它

便携交付形态是 **Windows zip，解压即用、不做 pip install**
（D-003）。而没有安装就没有 `[project.scripts]` 生成的
`zhengtong` 命令——那种分发方式下唯一能启动的办法就是
`python -m zhengtong`。

故两个入口都留着：`__main__.py` 走 `cli.main.main`，
`pyproject.toml` 的 console script 走同一个 `main`，
**同一个实现，不会有两条行为不同的路径**。
"""

from __future__ import annotations

from .cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
