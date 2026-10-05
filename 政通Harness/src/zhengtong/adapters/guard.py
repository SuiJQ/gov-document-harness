"""路径守卫（D-076）。

**这是整个系统唯一允许碰文件系统路径的地方。**

## 为什么必须这么严

`user/` 归用户所有，AI 会去读里面的文件。一个软链就足以击穿：

    user/evil.txt -> /etc/passwd

按字面路径 `user/evil.txt` 看，它在允许的根下；但 `realpath` 之后
它指向系统文件。所以**判断必须在解析之后做**，
`os.path.realpath` 是唯一可靠的依据。

## 三步顺序不可换

```python
resolved = Path(os.path.realpath(os.path.normcase(str(raw))))
base     = Path(os.path.realpath(os.path.normcase(str(root))))
resolved.relative_to(base)
```

- `normcase` 在前：Windows 上大小写不敏感，不归一则 `User` 与 `user` 会被判为两个目录。
- `realpath` 解析符号链接与 Windows reparse point（junction），3.8+ 已支持。
- `relative_to` 判包含：它不做前缀字符串比较，
  故 `data-evil/` 不会被误判为在 `data/` 下。

**对不存在的路径 `realpath` 也能安全工作**（逐级解析已存在的部分），
所以"先创建再校验"不是前提，写入前即可判定。

## 拒绝是正常返回，不是异常（D-076）

越权时返回 `Result.fail(PATH_ESCAPE)` 而不是抛异常：
给模型一句"该角色无权调用此工具"，让它换条路；
抛异常会把整个流程带崩——而崩掉对"换个工具再试"毫无帮助。
"""

# 本文件放宽 Unknown 类规则，理由同 domain/errors.py 的 D-085：
# `Result.ok(...)` 返回 `Result[Any]`（三方案实测后确认唯一可行解）。
# 只影响静态提示，不影响运行行为。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
from __future__ import annotations

import os
import re
from pathlib import Path

from ..domain.errors import ErrorCode, Result
from . import winname


def resolve(raw: str | Path) -> Path:
    """归一 + 解析。**不要单独依赖这一函数的返回值做安全判断**，
    必须配 `ensure_within` —— 它只做归一，不做包含检查。"""
    return Path(os.path.realpath(os.path.normcase(str(raw))))


def ensure_within(root: str | Path, raw: str | Path) -> Result[Path]:
    """确保 `raw` 解析后位于 `root` 之内。**通过则返回解析后的绝对路径。**

    `root` 自身也经同样处理，故 root 是软链时其真实目标才是基准——
    否则一个指向 `/` 的软链根会让所有路径都"合法"。
    """
    base = resolve(root)
    target = resolve(raw)
    for 分量 in _分量(str(raw)):
        if reason := winname.why_rejected(分量):
            return Result.fail(
                ErrorCode.PATH_ESCAPE,
                f"路径分量不可用：{reason}（{分量}）",
            )
    try:
        # 只用它"是否抛异常"来判包含，**不用它的返回值**：
        # 返回的相对路径在这里没有用处
        _ = target.relative_to(base)
    except ValueError:
        return Result.fail(
            ErrorCode.PATH_ESCAPE,
            f"路径越出允许范围：{raw}",
        )
    return Result.ok(target)


def _分量(raw: str) -> tuple[str, ...]:
    """把路径**按两种分隔符都切开**，返回每个分量。

    ## 审计 D-133：不能只用 `Path.parts` —— 它依赖运行平台

    原实现用 `Path(str(raw)).parts` 取分量。在 Windows 上这是对的，
    但在 POSIX 上**反斜杠不是分隔符**：

        Path("data\\NUL\\x.txt").parts  →  ('data\\NUL\\x.txt',)   # 一个整体

    实测后果：`why_rejected("data\\NUL\\x.txt")` 返回 `None`（未识别），
    `ensure_within` **放行**，文件被真的写了出来。搬到 Windows 上，
    `NUL` 被当成设备名——正是本模块开头警告的
    「写进去、找不回来，且没有任何报错」。

    这在 Linux 上跑测试时**完全看不出来**：整套设备名防护的反斜杠
    分支从不执行。而 D-003 的验收平台是 Windows。

    故这里**手工按两种分隔符切**，与运行平台无关：
    无论在 Linux 还是 Windows 上开发，`data\\NUL\\x.txt` 都会被拦下。

    保留 `Path.parts` 的结果并**求并集**，因为它还负责拆掉
    盘符（`C:`）与 UNC 前缀（`\\\\server\\share`）——
    那些不是文件名，不该拿去做 `why_rejected`。

    **不能简单用 `保留名 in 分量` 来判断**：`我的NUL文件` 含 `NUL`
    但不是保留名，Windows 上完全合法。实测确认 `data/我的NUL文件/x.txt`
    现在是通过的，不能被这个修复误伤。
    """
    原文 = str(raw)
    parts = set(Path(原文).parts)
    parts.update(p for p in re.split(r"[\\/]+", 原文) if p)
    # 审计 D-141 回归修正：**分隔符本身不是文件名分量，必须滤掉**。
    # `re.split` 会产出空串与单个分隔符，`Path("/a").parts` 会产出
    # 根标记 `/`——它们都不是文件名，交给 `why_rejected` 会被
    # D-141 新加的「这不是文件名」规则拦下，
    # 从而把 `data/我的NUL文件/y.txt` 这类**完全正常**的路径误杀。
    # 判据很简单：**只有不含分隔符、且非空的才可能是文件名。**
    return tuple(p for p in parts if p and p not in {"/", "\\"})


def is_within(root: str | Path, raw: str | Path) -> bool:
    """只要布尔。`ensure_within` 的薄封装，供不便拿 `Result` 的场合。"""
    got = ensure_within(root, raw)
    _ = got  # 结果本身不要，只要"是否通过"这个布尔
    return got.is_ok
