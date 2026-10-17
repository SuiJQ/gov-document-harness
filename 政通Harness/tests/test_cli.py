"""命令行入口真的能启动（第 16 步补齐，D-281）。

## 此前为什么完全没有判据

`pyproject.toml` 早早就声明了

```toml
[project.scripts]
zhengtong = "zhengtong.cli.main:main"
```

而 `zhengtong.cli` **从未存在**。于是：

- 装出来的命令一启动就 `ModuleNotFoundError`
- 全仓没有 `__main__.py`、没有 `def main()`
- **`test_step16_presentation.py` 那 21 条判据全绿**——
  它们测的确实是真东西（三驱动同源），
  但**没有一条问过「程序能不能启动」**

这是本项目「东西齐了但没接上」里最严重的一例：
它让「第 16 步完成」这个结论**与「程序可用」毫无关系**。

本文件的核心判据就是那一条：
**声明的入口必须真的可导入且可调用。**
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

仓库根 = Path(__file__).resolve().parents[1]
入口 = "zhengtong.cli.main:main"


class Test声明的入口真的存在:
    """**这条判据以前完全没有**，而它正是「启动即崩」的唯一防线。"""

    def test_pyproject声明的入口可导入(self) -> None:
        声明 = tomllib.loads((仓库根 / "pyproject.toml").read_text(encoding="utf-8"))
        目标 = 声明["project"]["scripts"]["zhengtong"]
        assert 目标 == 入口, f"入口改了但本测试没跟着改：{目标}"
        模块名, 函数名 = 目标.split(":")
        模块 = __import__(模块名, fromlist=[函数名])
        assert callable(getattr(模块, 函数名)), f"{目标} 不可调用"

    def test_包级入口存在(self) -> None:
        """便携 zip **解压即用、不做 pip install**，
        那时没有 console script，唯一能启动的是 `python -m zhengtong`。"""
        assert (仓库根 / "src" / "zhengtong" / "__main__.py").is_file()

    def test_cli包不碰textual(self) -> None:
        """`--doctor` 与 `--run` **必须**在没有 textual 的环境里可用
        （D-003：便携运行时依赖越少越好）。

        判据直接读源码文本——这是**唯一**能证明
        「import 语句不会在模块加载时执行 textual」的办法。
        """
        源码 = (仓库根 / "src" / "zhengtong" / "cli" / "main.py").read_text(encoding="utf-8")
        顶层 = [
            行
            for 行 in 源码.splitlines()
            if 行.startswith(("import ", "from ")) and "textual" in 行
        ]
        assert not 顶层, f"cli/main.py 顶层 import 了 textual：{顶层}"


class Test真能启动:
    """**起真子进程**，不调函数——函数能被调用不等于程序能启动。"""

    def _跑(self, 参数: list[str], 根: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "zhengtong", *参数],
            capture_output=True,
            text=True,
            cwd=仓库根,
            env={"PYTHONPATH": str(仓库根 / "src") + ":/app/user-packages/python:/run/csi/mount-root/nas/4079184d856ecc166ed19d4887083405/workspaces/Harness/政通Harness/.venv/lib/python3.11/site-packages", "PATH": "/usr/bin:/bin"},
            timeout=120,
            check=False,
        )

    def test_无参数显示帮助(self, tmp_path: Path) -> None:
        """静默退出对用户等于「程序没反应」，故必须显示帮助。"""
        r = self._跑(["--root", str(tmp_path)], tmp_path)
        assert r.returncode == 0, r.stderr
        assert "--doctor" in r.stdout

    def test_doctor真跑起来(self, tmp_path: Path) -> None:
        """**本文件最核心的一条**：自检必须真的跑出结论。"""
        r = self._跑(["--doctor", "--root", str(tmp_path)], tmp_path)
        assert "自检结果" in r.stdout, r.stderr
        assert r.returncode in (0, 1), f"退出码异常：{r.returncode}\n{r.stderr}"

    def test_doctor会自建三目录(self, tmp_path: Path) -> None:
        """D-017：用户把 `data/` `user/` 全删过之后再启动，必须建回来。"""
        self._跑(["--doctor", "--root", str(tmp_path)], tmp_path)
        for 名 in ("system", "data", "user"):
            assert (tmp_path / 名).is_dir(), f"{名} 没建回来"

    def test_doctor逐项失败都带处置(self, tmp_path: Path) -> None:
        """D-027 三件事：失败在哪、报了什么、**用户可以怎么处理**。

        判据断的是**措辞**而不只是「有没有失败」——
        只说「配置未填」而不说怎么改，用户照样卡住。
        """
        r = self._跑(["--doctor", "--root", str(tmp_path)], tmp_path)
        assert "请按下列处置处理：" in r.stdout, r.stdout
        assert "config.toml" in r.stdout, "处置里没说要建哪个文件"

    def test_空工作区自检不通过(self, tmp_path: Path) -> None:
        """没有配置时**必须**判未通过。
        报「全部通过」比报失败糟得多——用户会以为可以开写了。"""
        r = self._跑(["--doctor", "--root", str(tmp_path)], tmp_path)
        assert r.returncode == 1, f"空工作区竟判通过：\n{r.stdout}"


class Test参数错在哪一步说得清:
    def test_已删的run报参数错退出码2(self, tmp_path: Path) -> None:
        """`--run` 已按 D-294 删除。**删掉的东西必须报参数错，不能静默接受**——
        argparse 认不出的参数会自动退 2 并打印用法，这是契约的一部分。"""
        r = subprocess.run(
            [sys.executable, "-m", "zhengtong", "--run", "--root", str(tmp_path)],
            capture_output=True,
            text=True,
            cwd=仓库根,
            env={"PYTHONPATH": str(仓库根 / "src") + ":/app/user-packages/python:/run/csi/mount-root/nas/4079184d856ecc166ed19d4887083405/workspaces/Harness/政通Harness/.venv/lib/python3.11/site-packages", "PATH": "/usr/bin:/bin"},
            timeout=120,
            check=False,
        )
        assert r.returncode == 2, r.stderr
        assert "--run" in r.stderr, f"没说清哪个参数不认：{r.stderr}"

    def test_坏档位不静默回落(self) -> None:
        """用户选 Pro 却跑成咨询，比报错糟得多（D-082 同理）。"""
        from zhengtong.cli.main import 取档位

        assert 取档位("豪华版") == (None, 取档位("豪华版")[1])
        assert "不认识" in 取档位("豪华版")[1]

    @pytest.mark.parametrize(
        ("原文", "期望值"),
        [
            ("咨询", "consult"),
            ("flash", "flash"),
            ("Flash", "flash"),
            ("pro", "pro"),
            ("", "consult"),
        ],
    )
    def test_档位解析(self, 原文: str, 期望值: str) -> None:
        from zhengtong.cli.main import 取档位

        档, 错 = 取档位(原文)
        assert 错 == "", f"不该报错：{错}"
        assert 档 is not None
        assert 档.value == 期望值


class TestTui缺依赖时区分成因:
    """**D-027 的关键**：程序缺口与环境缺口要说不同的话。

    我第一版一律报「textual 没装」，而本地 textual 明明已装、
    真错是 `tui_app` 还不存在——用户照着提示装一遍，问题原样不变。

    判据直接测**纯函数**，不起子进程也不做导入体操：
    `_tui_app` 尚不存在时真跑子进程只会走到「程序缺口」那一支，
    另一支注入起来比重写一遍还脆——**那条分支曾经没人测过，
    正是它把用户的处置指向了错误的方向**。
    """

    def test_缺textual给安装办法(self) -> None:
        from zhengtong.cli.main import _tui缺依赖提示

        出 = _tui缺依赖提示("textual")
        assert "pip install textual" in 出, 出

    def test_缺textual子模块也算(self) -> None:
        """`textual.app` 之类也要算「textual 没装」，不能落到程序缺口那支。"""
        from zhengtong.cli.main import _tui缺依赖提示

        assert "pip install textual" in _tui缺依赖提示("textual.app")

    def test_程序缺口不推给用户装东西(self) -> None:
        """**关键**：程序自己的缺口，不能让用户去装依赖。"""
        from zhengtong.cli.main import _tui缺依赖提示

        出 = _tui缺依赖提示("zhengtong.tui_app")
        assert "pip install" not in 出, f"把程序缺口说成了环境问题：{出}"
        assert "程序自身" in 出, 出

    def test_程序缺口要给可用出路(self) -> None:
        """说清不行之后，得告诉用户现在还能用什么。

        D-294 起出路只有 `--doctor`——**出路上不许留已删的参数**，
        照着提示敲一次却报参数错，比不给提示更糟。"""
        from zhengtong.cli.main import _tui缺依赖提示

        出 = _tui缺依赖提示("zhengtong.tui_app")
        assert "--doctor" in 出, 出
        assert "--run" not in 出, f"出路上还留着已删的 --run：{出}"

    def test_两支提示不相同(self) -> None:
        from zhengtong.cli.main import _tui缺依赖提示

        assert _tui缺依赖提示("textual") != _tui缺依赖提示("zhengtong.tui_app")
