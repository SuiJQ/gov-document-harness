"""推理开关提示的档位矩阵（D-384）。

## 为什么这组判据必须存在

修复前，OFF 档（咨询/Flash）在拿不到关闭写法时**静默按开启推理跑并计费**：
用户以为没花钱，实际花了，而全程**没有任何出口**。

`domain.reasoning` 把这件事从「一个 0」拆成五种状态之后，
「什么时候该对用户说一句话」就成了一个**可以机械校验**的问题。

## 为什么 Pro 档一律不提示

ON 档本来就不附加关闭字段（后端默认开着），
它的行为**符合预期**，没有偏差需要解释。提示它等于噪音。

## 为什么「无需关闭」也不提示

该模型本就不生成推理内容，用户不会因此多花钱，
也没有任何可行动的偏差。提示它是纯噪音——
早先的实现只判「能关闭」，于是每个用不推理模型的咨询档用户
每次开跑都白看一句「不附加推理控制字段」。
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from zhengtong.adapters.reasoning_cache import 写推理结论
from zhengtong.app import App
from zhengtong.domain.reasoning import 推理开关, 推理状态
from zhengtong.domain.tier import TierId

_会提示 = (推理状态.关闭不了, 推理状态.无法判定, 推理状态.无结论)


class _假App:
    """只借 `App` 的两个方法——它们只碰 `self.根` 与 `self.config.model`。"""

    def __init__(self, 根: Path) -> None:
        self.根 = 根
        self.config = types.SimpleNamespace(model="m")

    推理设置 = App.推理设置
    推理提示 = App.推理提示


def _落盘(tmp_path: Path, 状态: 推理状态) -> _假App:
    写推理结论(
        tmp_path / "system",
        模型="m",
        结论=推理开关(状态=状态, 可用写法=2 if 状态 is 推理状态.能关闭 else 0),
    )
    return _假App(tmp_path)


@pytest.mark.parametrize("状态", list(推理状态))
def test_提示只在该提示的时候出现(tmp_path: Path, 状态: 推理状态) -> None:
    应用 = _落盘(tmp_path, 状态)
    咨询 = 应用.推理提示(TierId.CONSULT)
    闪 = 应用.推理提示(TierId.FLASH)
    if 状态 in _会提示:
        assert 咨询, f"{状态} 该提示却说「一切正常」——用户会以为没花钱"
        assert 闪, "Flash 同为 OFF 档，行为与咨询一致"
        # 与 `推理开关.说明()` 比对，而不是拿枚举值去搜——
        # 提示是给人看的中文，用内部标识去断言它等于断言「给人看的不是人话」。
        assert 咨询.startswith(f"提示：{应用.推理设置().说明()}"), f"提示没说清是哪种情况：{咨询}"
    else:
        assert 咨询 is None, f"{状态} 不该提示（噪音）：{咨询}"
        assert 闪 is None


@pytest.mark.parametrize("状态", list(推理状态))
def test_ON档一律不提示(tmp_path: Path, 状态: 推理状态) -> None:
    """Pro 档本来就不关推理，行为符合预期，没有偏差需要解释。"""
    assert _落盘(tmp_path, 状态).推理提示(TierId.PRO) is None


def test_未探测过时提示指向体检(tmp_path: Path) -> None:
    """「没探测过」是**可行动**的：跑一次 `--doctor` 即可。"""
    应用 = _假App(tmp_path)  # 什么都不落盘
    提示 = 应用.推理提示(TierId.CONSULT)
    assert 提示, "没探测过正是最该说一声的情形"
    assert "未探测过" in 提示, 提示


def test_提示指向本档而不是笼统一句(tmp_path: Path) -> None:
    """提示里要带档位——否则用户不知道是这次运行受影响还是全局设置有问题。"""
    应用 = _落盘(tmp_path, 推理状态.关闭不了)
    咨询 = 应用.推理提示(TierId.CONSULT)
    闪 = 应用.推理提示(TierId.FLASH)
    assert 咨询 != 闪, "两档的提示必须能区分"
    assert 咨询 is not None and 闪 is not None, "两种情形都该提示"
    assert "consult" in 咨询 and "flash" in 闪


def test_能关闭时运行期真的会用上写法(tmp_path: Path) -> None:
    """**闭环判据**：提示说「能关」，运行期就得真有关闭字段。

    否则又是一轮「测了、报告了、用户看得懂，然后丢掉」（D-380）。
    """
    应用 = _落盘(tmp_path, 推理状态.能关闭)
    设置 = 应用.推理设置()
    assert 设置.能关闭
    assert 设置.关闭字段, "状态说能关，却没有关闭字段"
