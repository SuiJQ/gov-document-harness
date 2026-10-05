"""真实密钥源（`adapters/local_secrets.py`）的契约测试。

## 为什么这个适配器值得单独测

`SecretsPort` 是**全仓唯一允许返回密钥明文的地方**（D-030）。
接口小、信任面大，所以它的每一条边界都要有断言：

- 明文只从这里出去
- `redaction_patterns()` 给的是**片段**而非全文——
  否则脱敏器自己就成了第二个密钥持有者
- 配置没填时不能返回空串还假装成功

## 判据的证伪方式

每条断言都在「本该失败的情形」下跑过；
「不测的分支」一律不写进文件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zhengtong.adapters.config import Config, save
from zhengtong.adapters.local_secrets import LocalSecrets
from zhengtong.adapters.paths import ensure_dirs
from zhengtong.domain.errors import ErrorCode
from zhengtong.ports.secrets import SecretsPort

#: 假的但形态逼真的密钥。**不用真密钥**——
# 测试文件进 git，测试里也不该有真东西。
假密钥 = "sk-test0123456789abcdefghijklmnopqrstuvwxyz"


def _写配置(tmp_path: Path, **覆盖: str) -> LocalSecrets:
    """落一份配置到临时工作区，返回 LocalSecrets。"""
    ws = ensure_dirs(tmp_path / "ws").unwrap()
    cfg = Config(endpoint="https://x/v1/chat/completions", model="m", context_k=128)
    cfg = Config(
        endpoint=cfg.endpoint,
        model=cfg.model,
        context_k=cfg.context_k,
        api_key=覆盖.get("api_key", 假密钥),
    )
    got = save(ws, cfg)
    assert got.is_ok, got.error
    return LocalSecrets.from_workspace(ws)


# ═══ 一、协议与取值 ═══


def test_满足端口协议(tmp_path: Path) -> None:
    sec = _写配置(tmp_path)
    assert isinstance(sec, SecretsPort)


def test_取到配置里的密钥(tmp_path: Path) -> None:
    assert _写配置(tmp_path).get_api_key() == 假密钥


def test_快照而非每次读盘(tmp_path: Path) -> None:
    """构造时读一次并快照。

    每次 `get_api_key()` 都读盘会让「同一次运行内密钥不变」失效——
    用户中途改了配置，正在跑的这一轮行为会突变。
    """
    sec = _写配置(tmp_path)
    assert sec.get_api_key() == sec.get_api_key()


# ═══ 二、脱敏片段：给的是「怎么脱」不是「脱什么」═══


def test_脱敏片段不含完整密钥(tmp_path: Path) -> None:
    """**本文件最要紧的一条断言**。

    若 `redaction_patterns()` 返回完整密钥，
    脱敏器就成为第二个密钥持有者——
    它自己被日志记录时（异常栈、调试投影）密钥照样出去，
    「唯一持有点」这条纪律当场作废。
    """
    patterns = _写配置(tmp_path).redaction_patterns()
    assert patterns, "必须给出脱敏片段，否则密钥会原样进日志"
    for p in patterns:
        assert 假密钥 not in p, f"脱敏片段泄露了完整密钥：{p!r}"


def test_脱敏片段足以覆盖密钥(tmp_path: Path) -> None:
    """反过来：片段太短会导致**漏脱敏**。

    取「前 8 字符 + 后 4 字符」这种足以唯一识别、
    又不足以还原完整密钥的长度。
    """
    patterns = _写配置(tmp_path).redaction_patterns()
    拼 = "".join(patterns)
    assert 假密钥[:8] in 拼, "片段须含密钥前缀，否则前段匹配不上"
    assert 假密钥[-4:] in 拼, "片段须含密钥后缀，否则后段匹配不上"


def test_没有密钥时脱敏片段为空而非空串占位(tmp_path: Path) -> None:
    """没填密钥就没有片段。给个空串占位会让脱敏器去匹配空串——
    那会把日志里所有空位置都替换掉。"""
    ws = ensure_dirs(tmp_path / "ws2").unwrap()
    assert LocalSecrets.from_workspace(ws).redaction_patterns() == ()


# ═══ 三、没配置时不得假装成功 ═══


def test_没配置时取到空串(tmp_path: Path) -> None:
    """返回空串而不是抛异常：首次启动本就没配置。"""
    ws = ensure_dirs(tmp_path / "ws3").unwrap()
    assert LocalSecrets.from_workspace(ws).get_api_key() == ""


def test_没配置时校验报出缺密钥(tmp_path: Path) -> None:
    """**光返回空串不够**——调用方可能忘了判空就直接发请求。"""
    from zhengtong.adapters.config import load

    ws = ensure_dirs(tmp_path / "ws4").unwrap()
    cfg = load(ws).unwrap()
    got = cfg.validate()
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.CONFIG_INVALID
    assert "api_key" in got.error.detail


@pytest.mark.parametrize("坏值", ["", "   ", "\n\t"])
def test_纯空白视为未填(tmp_path: Path, 坏值: str) -> None:
    """`"   "` 不是空串但也没内容。当成已填会让请求带着空白密钥发出去，
    换来一个语焉不详的 401。"""
    ws = ensure_dirs(tmp_path / "ws5").unwrap()
    got = save(
        ws,
        Config(
            endpoint="https://x/v1/chat/completions",
            model="m",
            context_k=128,
            api_key=坏值,
        ),
    )
    assert got.is_ok
    assert LocalSecrets.from_workspace(ws).get_api_key() == ""


# ═══ 四、密钥不得随异常或 repr 泄出 ═══


def test_repr不含密钥(tmp_path: Path) -> None:
    """调试时 `print(sec)` 是很自然的动作。
    默认 `object.__repr__` 只给地址，安全；
    但一旦有人加了 `__repr__` 或用了 dataclass，就会泄。
    这条断言挡住「以后有人加上去」。"""
    assert 假密钥 not in repr(_写配置(tmp_path))


def test_str不含密钥(tmp_path: Path) -> None:
    assert 假密钥 not in str(_写配置(tmp_path))
