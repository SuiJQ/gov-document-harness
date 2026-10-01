"""配置读写测试（第 5 步：配置与日志）。

**"读得回来"不等于"读得对"**：本文件两组测试分开验证
序列化往返，以及**用户手改坏了配置时程序怎么反应**——
后者才是配置模块真正要解决的问题。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from zhengtong.adapters import config as cfgmod
from zhengtong.adapters.config import Config, EncodingConfig, config_path, load, save
from zhengtong.adapters.paths import Workspace, ensure_dirs
from zhengtong.domain.errors import ErrorCode

# ═══ 编解码往返 ═══


def test_默认配置_编码为utf8() -> None:
    """D-044：默认 UTF-8。"""
    assert Config().encoding.gb18030 is False
    assert Config().encoding.name() == "utf-8"


def test_编码开关只影响产出文件() -> None:
    """D-044：`gb18030=true` 时改用 GB18030。

    用 GB18030 而非 GBK：GB18030 是超集，生僻字不丢。
    """
    c = Config(encoding=EncodingConfig(gb18030=True))
    assert c.encoding.name() == "gb18030"


def test_往返(tmp_path: Path) -> None:
    _ = ensure_dirs(tmp_path)
    ws = Workspace(tmp_path)
    c = Config(
        encoding=EncodingConfig(gb18030=True),
        endpoint="https://example.invalid/v1/chat/completions",
        model="我的模型名",
        api_key="sk-测试密钥",
        context_k=128,
        temperature=0.3,
    )
    assert save(ws, c).is_ok
    got = load(ws)
    assert got.is_ok
    back = got.unwrap()
    assert back.encoding.gb18030 is True
    assert back.endpoint == c.endpoint
    assert back.model == c.model
    assert back.api_key == c.api_key
    assert back.context_k == 128
    assert back.temperature == 0.3


def test_往返_中文不转义(tmp_path: Path) -> None:
    """配置里有中文，文件必须人能直接读。"""
    _ = ensure_dirs(tmp_path)
    ws = Workspace(tmp_path)
    _ = save(ws, Config(model="通义千问-长文本"))
    raw = config_path(ws).read_text(encoding="utf-8")
    assert "通义千问" in raw, "中文被转义成 \\uXXXX 会让用户无法直接改配置"


def test_配置落在system目录(tmp_path: Path) -> None:
    """配置在 `system/` 下——AI 看不到也改不了的地方（D-017）。"""
    _ = ensure_dirs(tmp_path)
    ws = Workspace(tmp_path)
    _ = save(ws, Config())
    assert config_path(ws).parent.name == "system"
    assert config_path(ws).is_file()


def test_端点原样保存不拼接(tmp_path: Path) -> None:
    """D-004：用户填完整地址，程序原样请求，不拼接不改写。"""
    _ = ensure_dirs(tmp_path)
    ws = Workspace(tmp_path)
    url = "https://api.example.invalid/some/deep/path?x=1"
    _ = save(ws, Config(endpoint=url))
    got = load(ws)
    assert got.is_ok
    assert got.unwrap().endpoint == url, "端点被程序改写了"


# ═══ 首次启动与缺失 ═══


def test_文件不存在返回默认配置(tmp_path: Path) -> None:
    """首次启动本就没有配置，报错只会让用户看到栈回溯。"""
    _ = ensure_dirs(tmp_path)
    got = load(Workspace(tmp_path))
    assert got.is_ok
    assert got.unwrap() == Config()


def test_未填时validate报出缺哪几项() -> None:
    got = Config().validate()
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.CONFIG_INVALID
    assert "endpoint" in got.error.detail
    assert "model" in got.error.detail
    assert "api_key" in got.error.detail


def test_填全了validate通过() -> None:
    c = Config(
        endpoint="https://x.invalid/v1",
        model="m",
        api_key="k",
        context_k=128,
    )
    assert c.validate().is_ok


def test_容量非正数须报错() -> None:
    c = Config(endpoint="https://x.invalid", model="m", api_key="k", context_k=0)
    assert c.validate().is_error


def test_validate不查连通性() -> None:
    """**只查"填了没"，不查"对不对"**。

    在这里做连通性检查会让人以为配置自检过了，实际没测过。
    连通性是第 6 步连接检测的职责。
    """
    端点 = "https://绝对不存在的域名.invalid/v1"
    assert Config(endpoint=端点, model="m", api_key="k", context_k=128).validate().is_ok


# ═══ 用户手改坏了 ═══


def test_非法TOML返回失败而非崩溃(tmp_path: Path) -> None:
    _ = ensure_dirs(tmp_path)
    ws = Workspace(tmp_path)
    _ = config_path(ws).write_text("这不是 TOML = = =", encoding="utf-8")
    got = load(ws)
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.CONFIG_INVALID


def test_分区写成标量不崩(tmp_path: Path) -> None:
    """用户把 `[llm]` 写成 `llm = 5` 是常见手误。"""
    _ = ensure_dirs(tmp_path)
    ws = Workspace(tmp_path)
    _ = config_path(ws).write_text("llm = 5\n", encoding="utf-8")
    got = load(ws)
    assert got.is_ok, "畸形分区应回落默认，不该崩"
    assert got.unwrap() == Config()


def test_容量写成字符串能救回(tmp_path: Path) -> None:
    """`context_k = "128"` 是常见手误，能救就救。"""
    _ = ensure_dirs(tmp_path)
    ws = Workspace(tmp_path)
    _ = config_path(ws).write_text('llm = { context_k = "128" }\n', encoding="utf-8")
    got = load(ws)
    assert got.is_ok
    assert got.unwrap().context_k == 128


def test_容量写成不可救的字符串回落零(tmp_path: Path) -> None:
    _ = ensure_dirs(tmp_path)
    ws = Workspace(tmp_path)
    _ = config_path(ws).write_text('llm = { context_k = "很大" }\n', encoding="utf-8")
    got = load(ws)
    assert got.is_ok
    assert got.unwrap().context_k == 0, "救不回就落零，让 validate 去报"


def test_布尔值不被当成整数() -> None:
    """`context_k = true` 在 Python 里 `isinstance(True, int)` 为真，
    不显式排除会得到 context_k=1 这种看似合法的假值。"""
    assert Config.from_toml({"llm": {"context_k": True}}).context_k == 0


def test_温度缺省为03() -> None:
    """D-066：基准温度 0.3。"""
    assert Config.from_toml({}).temperature == 0.3
    assert Config.from_toml({"llm": {"temperature": 0.7}}).temperature == 0.7


# ═══ 写入安全 ═══


def test_保存走原子写(tmp_path: Path) -> None:
    """保存后不应残留临时文件。"""
    _ = ensure_dirs(tmp_path)
    ws = Workspace(tmp_path)
    _ = save(ws, Config(model="a"))
    _ = save(ws, Config(model="b"))
    leftovers = [p.name for p in (tmp_path / "system").iterdir() if p.name != "config.toml"]
    assert leftovers == [], f"残留临时文件：{leftovers}"
    got = load(ws)
    assert got.is_ok
    assert got.unwrap().model == "b"


def test_配置目录缺失时save失败而非静默建目录(tmp_path: Path) -> None:
    """配置目录是 `system/`，由 `ensure_dirs` 负责建。

    `save` 不隐式建目录：凭空生成目录会掩盖"启动自检没跑"这个真问题。
    """
    got = save(Workspace(tmp_path / "不存在"), Config())
    assert got.is_error
    assert not (tmp_path / "不存在").exists(), "不应隐式创建"


def test_默认文件不得被写成空(tmp_path: Path) -> None:
    """空配置也要写出完整结构：用户打开文件时该看到有哪些可填项。"""
    _ = ensure_dirs(tmp_path)
    ws = Workspace(tmp_path)
    _ = save(ws, Config())
    raw = config_path(ws).read_text(encoding="utf-8")
    assert "[encoding]" in raw
    assert "[llm]" in raw
    assert "gb18030" in raw


# ═══ 密钥 ═══


def test_密钥明文落盘但不在本模块脱敏(tmp_path: Path) -> None:
    """配置层**如实存**，脱敏由 `Redactor` 在发射处做（D-030）。

    两者分工明确：这里若擅自脱敏，用户填的 key 就不能用了；
    那里若不脱敏，密钥会进日志。
    """
    _ = ensure_dirs(tmp_path)
    ws = Workspace(tmp_path)
    _ = save(ws, Config(api_key="sk-真实密钥形状"))
    assert "sk-真实密钥形状" in config_path(ws).read_text(encoding="utf-8")


def test_配置文件名带版本() -> None:
    """D-008：软数据版本化。配置名固定，便于将来并存旧版。"""
    assert cfgmod.CONFIG_NAME == "config.toml"


@pytest.mark.parametrize("gb18030", [True, False])
def test_编码开关往返一致(tmp_path: Path, gb18030: bool) -> None:
    _ = ensure_dirs(tmp_path)
    ws = Workspace(tmp_path)
    _ = save(ws, Config(encoding=EncodingConfig(gb18030=gb18030)))
    got = load(ws)
    assert got.is_ok
    assert got.unwrap().encoding.gb18030 is gb18030


# ═══ D-139：非有限浮点值必须回落默认 ═══════════════════════════════════


@pytest.mark.parametrize(
    "坏值",
    [
        pytest.param("NaN", id="字母NaN"),
        pytest.param("inf", id="inf"),
        pytest.param("-inf", id="负inf"),
        pytest.param("Infinity", id="全拼Infinity"),
        pytest.param("1e999", id="溢出成inf"),
        pytest.param(float("nan"), id="浮点NaN"),
    ],
)
def test_非有限温度回落默认(坏值: object) -> None:
    """**用户只要在配置里打一个字母或一个足够大的数就能触发。**

    原实现直接 `float(value)`，而 `float("NaN")`、`float("inf")`、
    `float("1e999")` 全都成功，于是 `temperature` 变成 NaN/inf
    并一路流进 API 请求。

    实测后果：`json.dumps({"temperature": nan})` 产出
    `{"temperature": NaN}`——**这不是合法 JSON**，
    多数服务端直接 400 拒绝，且报错与真实原因完全无关，
    用户会以为是模型或端点的问题。

    另一重后果：D-066 规定基准温度 0.3 是为可复现，
    NaN 让可复现彻底失效。

    且 `validate()` 只查必填项与 `context_k`，**从不查 temperature**，
    这条路径上原本没有任何兜底。
    """
    got = Config.from_toml({"llm": {"temperature": 坏值}})
    assert math.isfinite(got.temperature), f"{坏值!r} 透传成了 {got.temperature!r}"
    assert got.temperature == 0.3, "应回落 D-066 的基准温度"


def test_合法温度不受影响() -> None:
    """**修 D-139 不能改坏正常值。**"""
    for 好值, 期望 in [(0.3, 0.3), (0.7, 0.7), ("0.3", 0.3), (0, 0.0), (1, 1.0)]:
        got = Config.from_toml({"llm": {"temperature": 好值}})
        assert got.temperature == 期望


def test_温度始终可安全序列化() -> None:
    """`json.dumps` 不得产出 `NaN`——那是非法 JSON。"""
    for 坏值 in ["NaN", "inf", "1e999"]:
        got = Config.from_toml({"llm": {"temperature": 坏值}})
        文本 = json.dumps({"temperature": got.temperature})
        assert "NaN" not in 文本 and "Infinity" not in 文本, 文本


# ═══ D-140：容许 128k 这类自然写法 ══════════════════════════════════════


@pytest.mark.parametrize(
    ("原值", "期望"),
    [
        pytest.param("128k", 128, id="小写k"),
        pytest.param("128K", 128, id="大写K"),
        pytest.param("128 K", 128, id="带空格"),
        pytest.param("128", 128, id="纯数字"),
        pytest.param(128, 128, id="整数"),
    ],
)
def test_容量容许带K单位(原值: object, 期望: int) -> None:
    """**用户界面问的就是「上下文容量多少 K」**，
    配置里写 `128k` 是极自然的写法。

    原实现只认纯数字，于是静默回落 0，报错却说
    「须为正整数」——而用户填的 `128k` **正是一个正整数**，
    用户只会反复重填，看不出是自己多打了 k。
    """
    got = Config.from_toml({"llm": {"context_k": 原值}})
    assert got.context_k == 期望


@pytest.mark.parametrize("坏值", ["128M", "1G", "128.5", "abc", "k128", ""])
def test_有歧义的容量写法不猜(坏值: str) -> None:
    """**只放行 K，不放行 M/G**。

    K 是本配置唯一约定的单位，无歧义；而放行 M 就会引入
    「128M 到底是 128 还是 131072」这种歧义——猜错比报错更糟。
    """
    got = Config.from_toml({"llm": {"context_k": 坏值}})
    assert got.context_k == 0, f"{坏值!r} 不该被猜成 {got.context_k}"
    assert (
        not Config(endpoint="e", model="m", api_key="k", context_k=got.context_k).validate().is_ok
    )


def test_容量为正整数时validate通过() -> None:
    """`128k` 现在能通过校验——这是 D-140 的目的。"""
    c = Config.from_toml(
        {"llm": {"endpoint": "e", "model": "m", "api_key": "k", "context_k": "128k"}}
    )
    assert c.context_k == 128
    assert c.validate().is_ok
