"""check.sh 报告顺序判据（D-414）。

## 为什么这道判据非有不可

`Supporter/check.sh` 是一键门禁。**报告顺序就是门禁语义的一部分**：

- 「全部通过。」这句话印出去，读者就按这句话下结论。
- 而脚本开头有 `set -e`，**任何一支检查挂掉都会让脚本在那一行直接退出**，
  所以严格说「全部通过」出现在**第 N 支之后**，本身意味着前 N 支过了。
- 但前提是它得出现在**最后一支之后**。若它排在中间、后面还有检查，
  那些检查挂了脚本照样 `set -e` 退出、且 exit 非 0——可屏幕最后一句
  是「全部通过。」，读者按屏幕下结论，于是「失败」被显示成「成功」。

这正是本项目栽过两回的报告—事实脱钩：
- D-242：basedpyright 一有 warning 就 return 1，其后所有检查从未执行，
  而每轮「check.sh 全绿」的记录里它们根本没跑过。
- D-281：`--fix` 声明了但一用就崩，因为没人走过那个分支。

## 判据（静态，不真跑脚本）

从脚本里取出「全部通过」那**一句完整命令**（`echo "全部通过。"`），
断言它之后**再无检查命令**。检查命令以 `python3 ../Supporter/`、
`.venv/bin/python ../Supporter/`、`.venv/bin/python -m pytest` 或
`"$V/...` 开头。

真跑一遍的成本高（全量 pytest + 类型检查），不适合常驻；这里只锁
「顺序」这一条结构契约。真要验脚本能不能跑，有 `test_supporter_independence.py`
那条 `test_移走Supporter后测试仍全绿`（它 shell 出去真跑）。
"""

from __future__ import annotations

from pathlib import Path

仓库根 = Path(__file__).resolve().parents[1]  # Harness/政通Harness
Supporter = 仓库根.parent / "Supporter"


def _检查命令(行: str) -> bool:
    """这一行算不算「执行检查」的命令。"""
    s = 行.strip()
    if not s or s.startswith("#"):
        return False
    # 只挑**真实执行检查/验证**的命令；`cd`、`echo`、`if` 等不算。
    前缀 = (
        "python3 ../Supporter/",
        ".venv/bin/python ../Supporter/",
        ".venv/bin/python -m pytest",
        '"$V/python" -m pytest',
        '"$V/ruff"',
        '"$V/basedpyright"',
    )
    return any(s.startswith(p) for p in 前缀)


def _脚本文本() -> str:
    p = Supporter / "check.sh"
    assert p.is_file(), f"check.sh 不存在：{p}"
    return p.read_text(encoding="utf-8")


def test_全部通过印在最后一支检查之后() -> None:
    """「全部通过。」之后不能再有检查命令。"""
    文本 = _脚本文本()
    行们 = 文本.splitlines()
    定位 = [i for i, ln in enumerate(行们) if '"全部通过。"' in ln]
    assert len(定位) == 1, f"应恰好一句「全部通过」，现有 {len(定位)} 处：{定位}"
    印处 = 定位[0]

    之后 = 行们[印处 + 1 :]
    残留 = [ln for ln in 之后 if _检查命令(ln)]
    assert not 残留, (
        f"「全部通过。」印在 {印处 + 1} 行，其后还有 {len(残留)} 支检查，"
        "它们挂掉时脚本 exit 非 0 但屏幕最后一句是「全部通过。」——\n"
        "报告与事实脱钩（D-242 / D-281 同型）。残留命令：\n  "
        + "\n  ".join(残留)
    )


def test_全部通过印在脚本最末命令() -> None:
    """「全部通过」必须是最后一条命令，其后只许有空行/注释。"""
    文本 = _脚本文本()
    行们 = 文本.splitlines()
    非空 = [i for i, ln in enumerate(行们) if ln.strip() and not ln.strip().startswith("#")]
    assert 非空, "脚本全空？"
    最末命令行 = 行们[非空[-1]]
    assert '"全部通过。"' in 最末命令行, (
        f"脚本最后一条命令是 `{最末命令行.strip()}`，不是「全部通过。」"
    )
