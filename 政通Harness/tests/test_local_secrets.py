"""`LocalSecrets` 真件判据——**真凭据适配器此前零判据**。

## 为什么这组判据是新增的

体检阶段 3 的替身→真件对照表里，`LocalSecrets` 是**唯一真件零判据**的：

| 替身 | 替身判据 | 真件判据 |
| --- | --- | --- |
| `FakeSecrets` | 3 处 | `LocalSecrets` **0 处** |

真运行路径上，用户填的 API key 就是经它从 `system/config.toml`
读出来的。而这一段**从未被执行过**——真凭据读取、TOML 解析、
首尾空白处理、脱敏片段的取舍，全靠替身（`FakeSecrets` 直接返回常量）。

D-112 说得明白：假实现的价值在于行为与真实现一致，一旦更宽松，
它就不再是可信的参照物。零真件判据时，连「是否更宽松」都无法回答。

## 每条判据挡的是一个真实失败

- **读得出**：TOML 解析 + `api_key` 取值路径。读不出则真运行请求头为空。
- **`repr` 不泄密**：`print(sec)` 或调试器展开对象时，
  默认 dataclass `repr` 会把字段原样打出来——**密钥直接上屏**。
  真件靠 `repr=False` 挡住，这条判据是那个决定的守门。
- **脱敏给片段不给全文**：日志里要能匹配到，
  但存进规则集的是可还原性很低的前后缀。
- **短密钥不脱敏**：碎片太短会大面积误伤正常文本，
  那比不脱更糟——真泄密会被淹没。
- **配置损坏不抛**：`load` 读不出来当「没填」，
  真正的问题由 `Config.validate` 报给用户，而不是启动时炸栈。
"""

from __future__ import annotations

from pathlib import Path

from zhengtong.adapters.local_secrets import LocalSecrets
from zhengtong.adapters.paths import Workspace


def _工作区(根: Path, 内容: str) -> Workspace:
    """造一个只写 `system/config.toml` 的工作区。

    **注意配置是分节的**：`api_key` 在 `[llm]` 段下
    （`Config.from_toml` 从 `llm` 节取值，`config.py:138-143`）。
    写成顶层 `api_key = ...` 会读成空——**而这恰好是判据最容易
    写错、然后误报成「真件坏了」的地方**。
    """
    根.mkdir(parents=True, exist_ok=True)
    (根 / "system").mkdir(parents=True, exist_ok=True)
    _已写 = (根 / "system" / "config.toml").write_text(内容, encoding="utf-8")
    assert _已写 > 0, "配置没写成功，后面的判据就会因读不到而误报成「真件坏了」"
    return Workspace(根)


def test_真凭据能从配置读出(tmp_path: Path) -> None:
    """**主路径**：真运行上用户填的 key 就是这样进来的。

    此前这段零执行——替身 `FakeSecrets` 直接返回常量，
    于是「读 TOML → 取 api_key」这条链上任何错都没人发现。
    """
    ws = _工作区(tmp_path, '[llm]\napi_key = "sk-真实密钥-abcdefghijkl"\n')
    assert LocalSecrets.from_workspace(ws).get_api_key() == "sk-真实密钥-abcdefghijkl"


def test_首尾空白被裁掉(tmp_path: Path) -> None:
    """用户从网页复制 key 常带空白/换行。

    不裁的话请求头里就是 `Bearer  sk-xxx\\n`——真运行会 401，
    而替身返回常量，**永远测不出这个**。
    """
    ws = _工作区(tmp_path, '[llm]\napi_key = "  sk-带空白-abcdefghijkl  "\n')
    assert LocalSecrets.from_workspace(ws).get_api_key() == "sk-带空白-abcdefghijkl"


def test_repr不得泄密(tmp_path: Path) -> None:
    """**`repr=False` 是真件的设计决定，这里是它的守门**。

    默认 dataclass `repr` 会把 `_key` 原样打出来，
    于是 `print(sec)`、日志里 `f"{sec}"`、调试器展开对象——
    **三种最常见的顺手操作都会让密钥上屏**。

    注意：这测的是 `repr` 与 `str` 两条路径，
    因为 `f"{x}"` 走 `__format__` → `__str__`，
    与 `print(x)` 走的 `__str__` 一致，但和 `repr(x)` 是两条路。
    """
    ws = _工作区(tmp_path, '[llm]\napi_key = "sk-绝不能出现在屏幕-abcdefghijkl"\n')
    sec = LocalSecrets.from_workspace(ws)
    for 文本 in (repr(sec), str(sec), f"{sec}"):
        assert "绝不能出现在屏幕" not in 文本, f"repr/str 泄了密钥：{文本!r}"


def test_脱敏给片段而非全文(tmp_path: Path) -> None:
    """脱敏规则里存的是**前后缀片段**，不是整条密钥。

    整条存进去等于把密钥换个地方再存一遍——
    日志一旦外泄，规则集本身就是泄露源。
    """
    ws = _工作区(tmp_path, '[llm]\napi_key = "sk-abcdefghijklmnopqrstuvwxyz"\n')
    片段 = LocalSecrets.from_workspace(ws).redaction_patterns()
    assert 片段, "够长的密钥必须给出脱敏片段"
    整条 = "sk-abcdefghijklmnopqrstuvwxyz"
    assert all(p != 整条 for p in 片段), "脱敏规则里不得出现整条密钥"
    assert any(p in 整条 for p in 片段), f"片段必须来自密钥本身，实得 {片段}"


def test_短密钥不脱敏(tmp_path: Path) -> None:
    """短于阈值的密钥**不给片段**。

    碎片太短会大面积误伤正常文本，把日志搞得没法看，
    真泄密反而被淹没——那比不脱更糟。
    故返回空元组而不是 `("",)`（空串会匹配所有空位）。
    """
    ws = _工作区(tmp_path, '[llm]\napi_key = "短"\n')
    assert LocalSecrets.from_workspace(ws).redaction_patterns() == ()


def test_配置损坏不抛异常(tmp_path: Path) -> None:
    """**损坏当「没填」**，真正的问题交给 `Config.validate` 报给用户。

    在这里抛异常意味着：用户改坏了配置，程序直接崩栈，
    而不是告诉他哪一行写错了。
    """
    ws = _工作区(tmp_path, "这不是合法的 TOML = = =\n")
    sec = LocalSecrets.from_workspace(ws)
    assert sec.get_api_key() == "", "损坏配置应读成空密钥，而不是抛异常"


def test_配置缺失不抛异常(tmp_path: Path) -> None:
    """连 `config.toml` 都没有时同样读成空。"""
    ws = Workspace(tmp_path / "不存在的根")
    ws.root.mkdir(parents=True, exist_ok=True)
    assert LocalSecrets.from_workspace(ws).get_api_key() == ""
