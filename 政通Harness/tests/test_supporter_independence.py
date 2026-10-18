"""「脱离 Supporter 也能工作」的三条守门判据。

## 这条约束是什么

用户 2026-10-05 定：Harness 下分两层，`Supporter/` 装开发期工具，
`政通Harness/` 是主体。**Supporter 只是开发阶段的工具和辅助脚本，
实际使用时没有它们也能跑。**

在此之前这句话只存在于用户的原话里，一行代码都不守它。

## 为什么三条都要有

- **第 1 条**守「主体不反向依赖工具」——静态扫，改了立刻红。
- **第 2 条**守「没有它也能跑」——**真把 Supporter 移走跑一遍 pytest**。
  这是唯一能证明这句话的判据；前两条都只是它的必要条件。
- **第 3 条**守内容包清单不牵扯 Supporter——否则换包就断。

第 2 条用**子进程 + 临时移走**实现，代价是几十秒。
但一条跑一次的验收判据换一条硬约束，值得。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

主体 = Path(__file__).resolve().parents[1]
仓库根 = 主体.parent
Supporter = 仓库根 / "Supporter"
_SUP = Supporter.resolve()

_不在场 = pytest.mark.skipif(
    not Supporter.is_dir(), reason="Supporter 不在场（便携 zip 不含开发工具）"
)


class Test主体不依赖工具:
    """第 1 条：静态扫「主体里有没有指向 Supporter 的路径」。"""

    def test_主体里没有Supporter路径(self) -> None:
        """**只扫 `src/` 与 `content/`——不扫 `docs/`。**

        `src` 里的路径字符串可能被真的执行，`content` 是交付物；
        而**文档提到 Supporter 是正常的**：
        `README` 要告诉开发者检查入口在 `../Supporter/check.sh`，
        `ARCHITECTURE` 要记 D-384，`TODO` 要说明欠账。
        扫文档等于**禁止记录这条约束本身**。

        第一版把 `docs` 也扫进去，结果我自己按 D-384 更新
        `ARCHITECTURE.md` 的检查入口路径，判据当场变红——
        而那行改动**正是这条约束要求的**。
        判据把文档当依赖，等于逼人别写文档。
        """
        违规 = []
        for 目录 in ("src", "content"):
            根 = 主体 / 目录
            if not 根.is_dir():
                continue
            for f in 根.rglob("*"):
                if not f.is_file() or f.suffix not in {".py", ".json", ".md", ".toml"}:
                    continue
                正文 = f.read_text(encoding="utf-8", errors="replace")
                for 针 in ("../Supporter", "Supporter/", "Harness/Supporter"):
                    if 针 in 正文:
                        # **不用 `l` 当循环变量**：单字母 `l` 与数字 1 难分辨（E741）。
                        # 我在 `test_tui_exit.py` 修过一次这个，判据里又犯了一次——
                        # 同一个错犯两遍，说明它不是「没记住」，是当时只改了那一处。
                        行 = next(
                            (i for i, 行文 in enumerate(正文.splitlines(), 1) if 针 in 行文), 0
                        )
                        违规.append(f"{f.relative_to(主体)}:{行} 引用了 {针}")
        assert not 违规, "主体引用了开发工具的路径：\n  " + "\n  ".join(违规)

    def test_主体不import工具模块(self) -> None:
        """工具是独立脚本（`smoke_server`、`check_wiring`…），不是包。

        故主体任何 `import` 都不该指到它们。工具真要被主体调用时，
        正确做法是**把能力移进 `src/`**，而不是从 Supporter 借。
        """
        违规 = []
        for f in (主体 / "src").rglob("*.py"):
            for i, 行 in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                s = 行.strip()
                if s.startswith(("import ", "from ")) and "smoke_server" in s:
                    违规.append(f"{f.relative_to(主体)}:{i} {s}")
        assert not 违规, "主体 import 了开发工具：\n  " + "\n  ".join(违规)


@_不在场
class Test没有Supporter也能跑:
    """第 2 条：**真把 Supporter 移走，跑一遍 pytest**。

    这才是那条硬约束的真实证明。

    ## 为什么要真移，而不是写条断言检查路径

    「主体里没有 Supporter 路径」这类静态检查能挡住**直接**依赖，
    挡不住：
    - `sys.path.insert(0, "../Supporter")` 这类运行时拼路径；
    - 通过 `Path.cwd()` 或环境变量绕一圈的间接依赖；
    - 判据自己在缺目录时静默 `continue`（`_待扫文件` 原本就是这么写的，
      现在改成了「明确说出来」）。

    真移走跑一遍，这些全都会现形——**跑不起来就是跑不起来**。
    """

    def test_移走Supporter后测试仍全绿(self) -> None:
        # **藏在同一文件系统内**，不用 pytest 的 `tmp_path`。
        #
        # 第一版用 `tmp_path`（在 `/tmp`），而仓库在另一个挂载点上——
        # `shutil.move` 跨设备时**退化成「逐文件复制 + 删源」**，
        # 不是原子 rename。复制期间子进程 pytest 往目标目录里写了
        # `__pycache__`，等它回头删源目录时目录已非空，
        # 抛 `Directory not empty`，**Supporter 就留在残缺状态**。
        #
        # 教训不只是「换个目录」：跨文件系统的移动会**在目标侧产生副作用**，
        # 而判据自己正是那个写东西的进程。
        藏 = 仓库根 / ".supporter暂存"
        shutil.move(str(Supporter), str(藏))
        try:
            assert not Supporter.is_dir(), "移走失败——这条判据等于没跑"
            r = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/",
                    "-q",
                    "--no-header",
                    "-p",
                    "no:cacheprovider",
                    "-x",
                    # **必须 ignore 本文件**：
                    # 它自己有 `test_主体与Supporter都在`，而这个场景里
                    # Supporter 恰恰**被移走了**——不 ignore 的话子进程必然红，
                    # 而红的原因不是「主体依赖工具」，是判据在检查自己的前提。
                    #
                    # 第一版就踩了这个坑：判据红，报错信息指向
                    # `assert 主体.is_dir() and Supporter.is_dir()`，
                    # 差点被当成真发现去查「主体怎么依赖 Supporter 了」。
                    "--ignore=tests/test_supporter_independence.py",
                    # **同样 ignore test_check_sh_report_order**：
                    # 它读 `Supporter/check.sh` 验报告顺序（D-414）——而这个场景里
                    # Supporter 恰恰被移走了。它红不是「主体依赖工具」，
                    # 是判据在检查一个不存在的前提（同型坑）。
                    "--ignore=tests/test_check_sh_report_order.py",
                ],
                cwd=主体,
                capture_output=True,
                text=True,
                env={
                    "PATH": "/usr/bin:/bin",
                    "PYTHONPATH": "/run/csi/mount-root/nas/4079184d856ecc166ed19d4887083405/workspaces/Harness/政通Harness/src:/app/user-packages/python:/run/csi/mount-root/nas/4079184d856ecc166ed19d4887083405/workspaces/Harness/政通Harness/.venv/lib/python3.11/site-packages",
                },
            )
            末 = (r.stdout + r.stderr)[-2500:]
            assert r.returncode == 0, f"**移走 Supporter 后测试红了**——说明主体确实依赖它。\n{末}"
        finally:
            # **先移回来再断言**：移不回来会把仓库留在残缺状态，
            # 那比判据红严重得多。
            if not Supporter.is_dir() and 藏.is_dir():
                shutil.move(str(藏), str(Supporter))
            assert Supporter.is_dir(), "**Supporter 没能移回来——需要手工恢复**"

    def test_移走期间产生的缓存要清(self) -> None:
        """移走 Supporter 会让 `__pycache__` 里留下指向旧路径的字节码。

        不清的话，下一次跑可能加载到带旧绝对路径的 pyc，
        报出与本次改动无关的 `OSError: could not get source code`——
        我在这次搬迁里就撞上过：206 个 pyc 指向搬迁前的路径。
        """
        脏 = [
            p
            for p in (主体 / "tests").rglob("__pycache__/*.pyc")
            if "政通-harness" in p.read_bytes()[:4096].decode("latin-1", "replace")
        ]
        assert not 脏, f"{len(脏)} 个 pyc 指向旧路径，需清 __pycache__"


@_不在场
class Test内容包不牵扯工具:
    """第 3 条：内容包清单不该牵扯开发工具。"""

    def test_清单不引用Supporter(self) -> None:
        import json

        清单 = 主体 / "content" / "manifest.json"
        assert 清单.is_file(), "内容包清单不在——内容包与程序同放（D-001）"
        键 = json.dumps(json.loads(清单.read_text("utf-8")), ensure_ascii=False)
        assert "Supporter" not in 键, "内容包清单里出现了 Supporter"

    def test_内容包逐文件校验通过(self) -> None:
        """内容包自带 sha256（D-016）。搬目录不该动它，但**得验**。"""
        sys.path.insert(0, str(主体 / "src"))
        from zhengtong.adapters.content_pack import 本地内容包

        got = 本地内容包(根=主体 / "content").verify_manifest()
        # 条件写 `got.error` 而**不是** `got.is_error`：后者不能让类型收窄，
        # `error` 仍被看成 `AppError | None`（BasedPyright 报 reportOptionalMemberAccess）。
        assert got.is_ok, f"内容包 sha256 校验没过：{got.error.detail if got.error else ''}"


class TestSupporter自身定位正确:
    """Supporter 里的 22 个脚本**都假定「我在项目根」**。

    搬到 `Supporter/` 后这个假定全错（`parents[1]` 变成了 `Harness/`）。
    逐个改很容易漏一个，而漏掉的那个**平时不会报错**——
    它只在被单独运行时才炸。故这条判据扫全部脚本的根路径表达式。
    """

    def test_所有脚本都把根指向主体(self) -> None:
        if not Supporter.is_dir():
            pytest.skip("Supporter 不在场")
        漏 = []
        for f in sorted(Supporter.glob("*.py")):
            正文 = f.read_text(encoding="utf-8")
            if "parents[1]" not in 正文 and "parent.parent" not in 正文:
                continue
            if "政通Harness" in 正文:
                continue
            漏.append(f.name)
        assert not 漏, f"这些脚本仍把 parents[1] 当项目根，搬到 Supporter 后全错：{漏}"

    def test_主体与Supporter都在(self) -> None:
        """这条判据自身的前提。红了说明是目录被拆错了，不是代码问题。"""
        assert 主体.is_dir(), f"主体目录不在：{主体}"
        assert Supporter.is_dir(), f"Supporter 目录不在：{Supporter}"
