"""原子写。

## 为什么不能"先删后写"

断电、崩溃、进程被杀——任何时刻打断都会留下**半截文件**。
状态 JSON 尤其致命：截断一半的 JSON 解析失败，
断点续跑（D-027）就再也起不来，用户以为在续跑，其实在读垃圾。

## 三步缺一不可

1. **写临时文件**：同目录，保证 `replace` 在同一文件系统内。
   写到别处再 replace 过来会跨设备，失败。
2. **`fsync`**：刷到盘。不刷的话 replace 只是改了目录项，
   数据还在页缓存里，断电照样丢——**这是最常被漏掉的一步**。
3. **`replace`**：POSIX 与 Windows 都保证目标要么是旧的、要么是新的，
   **不存在中间态**。这才是"原子"的来源。

失败路径上要**清理临时文件**。留着垃圾不致命，
但那会让用户在自己的目录里看到一堆看不懂的 `.tmp`。
"""

# 本文件放宽 Unknown 类规则，理由同 domain/errors.py 的 D-085：
# `Result.ok(...)` 返回 `Result[Any]`（三方案实测后确认唯一可行解）。
# 只影响静态提示，不影响运行行为。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
from __future__ import annotations

import contextlib
import os
from pathlib import Path

from ..domain.errors import ErrorCode, Result


def write_atomic(path: Path, content: str, *, encoding: str = "utf-8") -> Result[None]:
    """原子写文本。**父目录不存在则失败**——不隐式建目录。

    隐式建目录会让一次打错的路径凭空生成一串空目录，
    掩盖调用方的错误。建目录是 `ensure_dirs` 的职责。
    """
    tmp: Path | None = None
    try:
        # 临时名与目标同目录：同文件系统，replace 才成立
        tmp = path.with_name(f".{path.name}.tmp")
        with tmp.open("w", encoding=encoding, newline="\n") as f:
            # 写入字符数在这里没用，但**必须检查**：短写说明磁盘满，
            # 而短写后的 fsync+replace 会把半截文件当成完整文件落盘
            written = f.write(content)
            if written != len(content):
                return Result.fail(
                    ErrorCode.STORAGE_WRITE_FAILED,
                    f"短写：期望 {len(content)} 字符，实写 {written}",
                )
            # flush 把 Python 缓冲推给 OS；返回值无用但调用必要
            _ = f.flush()
            # fsync 无返回值可用，但**必须调用**——不刷盘则 replace 只改目录项
            _ = os.fsync(f.fileno())
        # `Path.replace` 底层即 `os.replace`，同样原子
        _ = tmp.replace(path)
    except OSError as exc:
        # 清理临时文件。失败了也不抛——它只是个垃圾文件，
        # 为它把整个写入结果变成"另一类错误"不值得。
        if tmp is not None:
            # 清不掉就算了：它只是个垃圾文件，为它把写入结果
            # 变成"另一类错误"不值得
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        return Result.fail(ErrorCode.STORAGE_WRITE_FAILED, f"写入失败：{exc}")
    return Result.ok_none()


def read_text(path: Path, *, encoding: str = "utf-8") -> Result[str]:
    """读文本。**显式 UTF-8**，不依赖系统默认编码——
    同一份程序在中文 Windows 与 Linux 上必须读出同一结果。"""
    try:
        return Result.ok(path.read_text(encoding=encoding))
    except OSError as exc:
        return Result.fail(ErrorCode.FILE_UNREADABLE, f"读取失败：{exc}")
