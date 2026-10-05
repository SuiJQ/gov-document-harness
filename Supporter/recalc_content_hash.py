#!/usr/bin/env python3
"""重算内容包 manifest 里的 sha256。

## 为什么需要这个工具

`verify_manifest()` **逐文件验 sha256，对不上直接报错、不降级**
（`content_pack.py` 的设计如此，且是对的——内容包是软数据，
Ruff 与类型检查器都看不见它）。

但全仓**没有**任何脚本能重算它。

于是每一处改动内容包的场合，都只能：

1. 手改文件 → 运行期报「内容包哈希不符（改了没同步 manifest）」
2. 然后——**去哪儿拿正确的哈希？**没有工具，只能自己写代码算一遍。

**声明了校验、却没有配套的修复手段**，是本项目的老毛病：
「声明了入口而模块从不存在」（D-281、D-284 同型）。

> **校验与修复必须成对。**只有校验没有重算，
> 校验就从「保护」变成了「每次都拦路」。

## 算法必须与 `verify_manifest` **逐字节一致**

```python
hashlib.sha256(路径.read_bytes()).hexdigest()
```

**不读文本、不做换行归一、不 strip BOM**——
一读文本就会踩 CRLF/LF 的差异（Windows 与 Linux 生成的行尾不同），
算出另一个哈希，然后永远对不上。

## 用法

```bash
python scripts/recalc_content_hash.py            # 重算全部
python scripts/recalc_content_hash.py --check    # 只报不改（CI 用）
python scripts/recalc_content_hash.py --only prompts/team-lead.md
```

`--check` 有不符时退出码 1，**不改文件**——
让「改了内容包吗」这个问题在门禁里能被自动回答。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

仓库根 = Path(__file__).resolve().parents[1] / "政通Harness"
清单路径 = 仓库根 / "content" / "manifest.json"


def 重算(相对: str) -> str:
    """算一个文件的 sha256。**读原始字节**，与 `verify_manifest` 同算法。"""
    return "sha256:" + hashlib.sha256((仓库根 / "content" / 相对).read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    解析器 = argparse.ArgumentParser(description="重算内容包 manifest 的 sha256")
    解析器.add_argument("--check", action="store_true", help="只报不改，有不符时退出码 1")
    解析器.add_argument("--only", metavar="相对路径", help="只处理这一个文件")
    参数 = 解析器.parse_args(argv)

    if not 清单路径.is_file():
        sys.stderr.write(f"找不到 {清单路径}\n")
        return 2

    清单 = json.loads(清单路径.read_text(encoding="utf-8"))
    文件表 = 清单.get("files")
    if not isinstance(文件表, dict):
        sys.stderr.write("manifest 缺 files\n")
        return 2

    待办 = [参数.only] if 参数.only else list(文件表)
    不符: list[str] = []
    for 相对 in 待办:
        if 相对 not in 文件表:
            sys.stderr.write(f"manifest 里没有 {相对}\n")
            return 2
        新值 = 重算(相对)
        旧值 = 文件表[相对]
        if 新值 == 旧值:
            continue
        不符.append(相对)
        if not 参数.check:
            文件表[相对] = 新值

    if not 不符:
        print("全部一致，无需改动。")
        return 0

    for 相对 in 不符:
        print(f"{'会改' if not 参数.check else '不符'}：{相对}")
    if not 参数.check:
        清单路径.write_text(json.dumps(清单, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已更新 {清单路径.relative_to(仓库根)}")
        return 0
    print("\n跑 `python scripts/recalc_content_hash.py` 修正。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
