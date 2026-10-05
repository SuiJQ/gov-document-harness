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
import logging
import os
from pathlib import Path

from ..domain.errors import ErrorCode, Result


def write_atomic(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    newline: str | None = None,
) -> Result[None]:
    """原子写文本。**父目录不存在则失败**——不隐式建目录。

    隐式建目录会让一次打错的路径凭空生成一串空目录，
    掩盖调用方的错误。建目录是 `ensure_dirs` 的职责。

    `newline=None`（默认）时跟随平台：Windows 得 CRLF，Linux 得 LF。
    **不硬写 `newline="\\n"`**——那样会在 Windows 上产出 LF-only 的公文，
    记事本能开，但 Word 打开时行尾不认，排版会出现整篇挤成一段的效果。

    调用方若**确切知道**目标格式（如导出 TXT 给用户），可显式传入。
    """
    tmp: Path | None = None
    try:
        # 临时名与目标同目录：同文件系统，replace 才成立
        tmp = path.with_name(f".{path.name}.tmp")
        with tmp.open("w", encoding=encoding, newline=newline) as f:
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
            _ = _fsync_or_ignore(f.fileno())
        # `Path.replace` 底层即 `os.replace`，同样原子。
        # Windows 上若目标正被 Word/记事本打开，会抛 PermissionError——
        # 那是"用户正看着这个文件"，不是写入失败，要说清
        try:
            _ = tmp.replace(path)
        except PermissionError:
            cleanup(tmp)
            return Result.fail(
                ErrorCode.STORAGE_WRITE_FAILED,
                f"写入失败：{path.name} 正被其他程序占用"
                + "（例如已在 Word 或记事本中打开），请关闭后重试",
            )
    except OSError as exc:
        cleanup(tmp)
        return Result.fail(ErrorCode.STORAGE_WRITE_FAILED, f"写入失败：{exc}")
    return Result.ok_none()


def _fsync_or_ignore(fd: int) -> None:
    """刷盘，失败不致命。

    POSIX 上 fsync 是原子写的第三步，缺了它断电就丢数据。
    但 Windows 上它对**部分文件系统会失败**：FAT32、部分网络盘、
    某些虚拟盘会抛 `OSError`（invalid argument / not supported）。
    那些盘不提供刷盘保证，硬失败只会让程序在这些机器上根本用不了。

    取舍很明确：**能刷就刷，刷不了照写**。
    丢数据的风险只出现在断电瞬间，而"程序在 U 盘上打不开"是必然发生。

    静态推理：Linux 上本函数恒成功，故此分支的跨平台依据是上两行注释所述
    的 Windows 行为，**未经 Windows 实测**。
    """
    try:
        _ = os.fsync(fd)
    except OSError:
        # 刷不了盘，如实放过——但**不留痕迹是错的**，
        # 故用 Debug 级别记一笔，便于用户报障时能查到
        logging.getLogger(__name__).debug("fsync 不可用，跳过刷盘", exc_info=True)


def cleanup(tmp: Path | None) -> None:
    """清理临时文件。**清不掉也不抛**——
    它只是个垃圾文件，为它把写入结果变成"另一类错误"不值得。"""
    if tmp is None:
        return
    with contextlib.suppress(OSError):
        tmp.unlink(missing_ok=True)


def read_text(path: Path, *, encoding: str = "utf-8") -> Result[str]:
    """读文本。**显式 UTF-8**，不依赖系统默认编码——
    同一份程序在中文 Windows 与 Linux 上必须读出同一结果。"""
    try:
        return Result.ok(path.read_text(encoding=encoding))
    except OSError as exc:
        return Result.fail(ErrorCode.FILE_UNREADABLE, f"读取失败：{exc}")
