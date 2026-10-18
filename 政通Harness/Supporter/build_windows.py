"""PyInstaller 打包：完整版（含 TUI）。

用法：
    .venv/bin/python Supporter/build_windows.py

产出：政通Harness/dist/zhengtong/zhengtong.exe（Windows）
      政通Harness/dist/zhengtong/  （目录模式，调试用）

## 为什么用 spec 而不是命令行

PyInstaller 命令行的 `--add-data` 用 `:` 分隔源和目标，
而本项目路径里有中文（`政通Harness`），在 shell 里容易出编码问题。
spec 文件是 Python 源码，路径是 `str`，不存在这个问题。

## 打什么

- `src/zhengtong` — 主体代码
- `content/` — 内容包（manifest + 提示词），运行时读
- `textual` + `httpx` + `tomli-w` — 全部运行时依赖（自动收集）

## 不打什么

- `tests/` — 判据，不进交付物
- `Supporter/` — 开发工具，不进交付物
- `system/config.toml` — 用户运行时数据，不进交付物（用户自己填 API 密钥）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "政通Harness"
VENV = ROOT / ".venv"
DIST = ROOT / "dist"
BUILD = ROOT / "build"


def _spec_content() -> str:
    """生成 PyInstaller spec 文件内容。"""
    return r"""# -*- mode: python ; coding: utf-8 -*-
block_cipher = None

a = Analysis(
    ['Supporter/_entry.py'],
    pathex=['src'],
    binaries=[],
    datas=[
        ('content', 'content'),
    ],
    hiddenimports=[
        'zhengtong.tui_app',
        'zhengtong.adapters.terminal',
        'zhengtong.adapters.winname',
        'zhengtong.adapters.fs_storage',
        'zhengtong.adapters.atomic',
        'zhengtong.adapters.config',
        'zhengtong.adapters.content_pack',
        'zhengtong.adapters.http_llm',
        'zhengtong.adapters.httpx_transport',
        'zhengtong.adapters.local_secrets',
        'zhengtong.adapters.paths',
        'zhengtong.adapters.reasoning_cache',
        'zhengtong.adapters.state_codec',
        'zhengtong.adapters.userfiles',
        'zhengtong.adapters.userfiles.readers.docx',
        'zhengtong.adapters.userfiles.scanner',
        'zhengtong.adapters.fakes',
        'zhengtong.kernel.bus',
        'zhengtong.kernel.driver',
        'zhengtong.kernel.scheduler',
        'zhengtong.orchestration',
        'zhengtong.orchestration.orchestrate',
        'zhengtong.orchestration.gates_forging',
        'zhengtong.presentation.gate',
        'zhengtong.presentation.tui',
        'zhengtong.services.context',
        'zhengtong.services.context.compactor',
        'zhengtong.services.context.composer',
        'zhengtong.services.context.memory_gate',
        'zhengtong.services.context.view',
        'zhengtong.services.export',
        'zhengtong.services.prompting',
        'zhengtong.services.tools',
        'zhengtong.services.tools.calllog',
        'zhengtong.services.tools.handlers',
        'zhengtong.services.tools.registry',
        'zhengtong.services.tools.runner',
        'zhengtong.services.tools.spec',
        'zhengtong.services.retrieval',
        'zhengtong.services.retrieval.search',
        'textual',
        'httpx',
        'tomli_w',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='zhengtong',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name='zhengtong',
    upx_exclude=[],
)
"""


def main() -> int:
    # 1. 写 spec
    spec_path = ROOT / "zhengtong.spec"
    spec_path.write_text(_spec_content(), encoding="utf-8")
    print(f"spec 已写入 {spec_path}")

    # 2. 清理旧构建
    for p in (DIST, BUILD):
        if p.exists():
            shutil.rmtree(p)
    print("已清理旧构建产物")

    # 3. 跑 PyInstaller
    # 优先用 uv 管理的独立 Python（自带 libpython3.11.so），
    # 回退到 .venv（系统 Python 可能缺共享库）
    for candidate in [
        "/tmp/pyi-venv/bin/python",
        str(VENV / "bin" / "python"),
    ]:
        if Path(candidate).exists():
            py = candidate
            break
    else:
        py = str(VENV / "bin" / "python")

    cmd = [
        py, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        str(spec_path),
    ]
    print(f"\n执行: {' '.join(cmd)}\n")
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print(f"\nPyInstaller 退出码 {result.returncode}")
        return result.returncode

    # 4. 报告产物
    exe = DIST / "zhengtong" / "zhengtong.exe"
    if exe.exists():
        size_mb = exe.stat().st_size / 1024 / 1024
        print(f"\n完成！产物: {exe}  ({size_mb:.1f} MB)")
    else:
        dir_size = sum(
            f.stat().st_size for f in (DIST / "zhengtong").rglob("*") if f.is_file()
        ) / 1024 / 1024
        print(f"\n完成！产物目录: {DIST / 'zhengtong'}  ({dir_size:.1f} MB)")

    # 5. 清理 spec
    spec_path.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())