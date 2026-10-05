"""三目录与启动自检（D-017）。

## 前提：用户可以把这三个目录全删了

`data/`（AI 产出）、`user/`（用户文件）、`system/`（配置与日志）
**都可以被用户直接整个删掉**，程序下次启动必须自动重建并继续工作。
这不是容错，是产品承诺（D-017）。

于是启动自检 `ensure_dirs()` 做的事很朴素：三个目录，不在就建。
**不做"是否被篡改"的检查** —— 目录里没有可信基线可比，
凭空造一个校验规则只会给用户一种虚假的安心感。

## 为什么 `system/` 也要重建

直觉上 `system/` 存配置、删了应该报错。但它同样可被删，
且程序没有别处能恢复配置——所以只能重建空目录，
让用户重新填。**这比"启动失败并说配置丢了"更符合 D-017**：
目录结构是程序的一部分，配置才是用户的数据。
"""

# 本文件放宽 Unknown 类规则，理由同 domain/errors.py 的 D-085：
# `Result.ok(...)` 返回 `Result[Any]`（三方案实测后确认唯一可行解）。
# 只影响静态提示，不影响运行行为。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..domain.errors import ErrorCode, Result

#: 三个目录名。**刻意用常量而非散落字面量**：
#: 这三个名字散在各处时，改一个就会漏另一个。
DATA = "data"
USER = "user"
SYSTEM = "system"

#: 全部目录名，顺序固定，便于日志与测试对照
ALL_DIRS: tuple[str, ...] = (DATA, USER, SYSTEM)

#: AI 可以读写的目录。**不含 system**：
#: AI 能看到 data/ 与 user/，能写 data/，看不到也改不了 system/。
AI_READABLE: tuple[str, ...] = (DATA, USER)
AI_WRITABLE: tuple[str, ...] = (DATA,)


@dataclass(frozen=True, slots=True)
class Workspace:
    """工作区根。三目录都挂在它下面。"""

    root: Path

    def dir(self, name: str) -> Path:
        """取某个目录的路径。**不创建**——判断与创建分开，
        免得"看一眼目录"这种只读操作产生副作用。"""
        return self.root / name

    def exists_ok(self) -> bool:
        """三目录是否齐全。"""
        return all((self.root / n).is_dir() for n in ALL_DIRS)


def ensure_dirs(root: Path) -> Result[Workspace]:
    """启动自检：确保三目录存在。**幂等**。

    用户全删过一次之后再启动，这里把它们建回来；
    已经存在则不动——**绝不删改已有内容**。
    """
    ws = Workspace(root)
    try:
        for name in ALL_DIRS:
            (ws.root / name).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Result.fail(
            ErrorCode.STORAGE_WRITE_FAILED,
            f"无法创建工作目录：{exc}",
        )
    if not ws.exists_ok():
        return Result.fail(
            ErrorCode.STORAGE_WRITE_FAILED,
            "工作目录创建后仍不齐全",
        )
    return Result.ok(ws)
