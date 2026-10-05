"""在**真文件系统**上验断点续跑。

## 为什么非验不可

D-112 写着「假实现一旦更宽松，就不再是可信参照物」。
本轮修的缺陷 8 与 9 全是幂等与读回——**而它们此前只在
`FakeStorage` 上验过**。假实现的 `read_text` 读内存字典，
真实现读磁盘；`put_artifact` 落盘的路径格式是否与
`read_text` 读的完全一致，只有真跑才知道。

这类 bug 的典型形态：假实现绿、真实现一跑就
`文件不存在`——正是缺陷 1 当初的形态。

## 三件事

1. **首跑落盘** → 检查 `data/<run-id>/` 真有文件
2. **同运行号续跑** → 检查请求数 0、产物内容与首跑逐字一致
3. **换运行号重跑** → 检查不是「一律不请求」（防过度续跑）
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

#: 项目根（脚本在 `scripts/` 下，故上溯一层）。
项目 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(项目 / "src"))

from zhengtong.adapters.fakes import FakeLlm  # noqa: E402
from zhengtong.adapters.fs_storage import FsStorage  # noqa: E402
from zhengtong.adapters.paths import Workspace, ensure_dirs  # noqa: E402
from zhengtong.domain.ids import RunId  # noqa: E402
from zhengtong.domain.tier import TierId  # noqa: E402
from zhengtong.orchestration.orchestrate import 编排器  # noqa: E402

失败: list[str] = []


def 断(条件: bool, 说明: str) -> None:
    print(f"  {'✓' if 条件 else '✗'} {说明}")
    if not 条件:
        失败.append(说明)


class _数请求(FakeLlm):
    def __init__(self) -> None:
        super().__init__()
        self.n = 0

    def complete(self, *a: object, **k: object):  # type: ignore[no-untyped-def]
        self.n += 1
        return super().complete(*a, **k)  # type: ignore[arg-type]


def 主() -> int:
    根 = Path(tempfile.mkdtemp(prefix="真续跑-"))
    try:
        shutil.copytree(项目 / "content", 根 / "content")
        ensure_dirs(根)

        仓 = FsStorage(Workspace(根))

        print("═" * 64)
        print("1　首跑落盘（真文件系统）")
        print("═" * 64)
        一 = _数请求()
        编排器(llm=一, storage=仓, 运行号=RunId("r-真"), 模型名="假").跑到终(
            档位=TierId.CONSULT, 需求="就本市老旧小区改造工作给出咨询意见")
        断(一.n > 0, f"首跑发了 {一.n} 次请求")

        目录 = 根 / "data" / "r-真"
        断(目录.is_dir(), f"{目录} 建出来了")
        落 = sorted(p.name for p in 目录.glob("*.md"))
        print(f"     盘上：{落}")
        断(bool(落), "盘上有产物")

        首 = {n: (目录 / n).read_text(encoding="utf-8") for n in 落}
        断(all(v.strip() for v in 首.values()), "首跑每个产物都有内容")

        态 = 仓.load_run_state(RunId("r-真"))
        断(态.is_ok, "state.json 读得回")
        if 态.is_ok:
            s = 态.unwrap()
            断(bool(s.completed_call_ids), f"账上有 {len(s.completed_call_ids)} 条已完成")
            断(bool(s.artifacts), f"账上有 {len(s.artifacts)} 条产物")

        print("\n" + "═" * 64)
        print("2　同运行号续跑")
        print("═" * 64)
        二 = _数请求()
        编 = 编排器(llm=二, storage=仓, 运行号=RunId("r-真"), 模型名="假")
        got = 编.跑到终(档位=TierId.CONSULT, 需求="就本市老旧小区改造工作给出咨询意见")
        断(got.is_ok, f"续跑成功：{got.error.detail[:60] if got.is_error else ''}")
        断(二.n == 0, f"续跑发了 {二.n} 次请求（应为 0）")

        续 = {n: (目录 / n).read_text(encoding="utf-8") for n in 落}
        for 名 in 首:
            断(名 in 续, f"{名} 续跑后仍在")
            断(续.get(名, "").strip() != "", f"{名} 续跑后非空 ← 最关键")
            断(续.get(名) == 首[名], f"{名} 与首跑逐字一致")
            print(f"     {名}: 首 {len(首[名])} 字 → 续 {len(续.get(名, ''))} 字")

        print("\n" + "═" * 64)
        print("3　换运行号必须真跑（防「过度续跑」）")
        print("═" * 64)
        三 = _数请求()
        编排器(llm=三, storage=仓, 运行号=RunId("r-新"), 模型名="假").跑到终(
            档位=TierId.CONSULT, 需求="另一个需求")
        断(三.n > 0, f"新运行号发了 {三.n} 次请求（不应被上次的账跳过）")

        return 1 if 失败 else 0
    finally:
        shutil.rmtree(根, ignore_errors=True)


if __name__ == "__main__":
    代码 = 主()
    print("\n" + "═" * 64)
    if 失败:
        print(f"✗ {len(失败)} 条没过：")
        for f in 失败:
            print(f"   - {f}")
    else:
        print("✓ 真文件系统上的续跑全部通过")
    raise SystemExit(代码)
