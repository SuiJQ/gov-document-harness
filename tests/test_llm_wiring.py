"""真实 LLM 适配器**接进装配**的契约（第 6 步零信任复查补上）。

## 复查查出的问题

第 6 步写完 `HttpLlm` + `HttpxTransport` 后自查：

    grep -rn "HttpLlm" src/ --include="*.py" | grep -v http_llm.py
    → 零引用

**适配器在生产代码里没有任何调用方。** 实测 `build_app_from_workspace`：

    配置读到了: qwen3.8-27b
    端口里的 llm 是: FakeLlm        ← 真实配置配假模型
    端口里的 clock 是: FakeClock

即：**用户现在运行程序，模型调用走的是假实现**，
返回一段写死的假内容。第 6 步的判据「探测跑通真实端点」
只证明了**脚本层面**通，没证明**运行路径**通。

**与 D-160 完全同型**：那次是 `build_app_from_workspace` 32 行没测，
这次是适配器造好了没接线。**同一个错误犯了两次。**

## 本文件断言什么

真实运行入口拿到的必须是真实现；测试要显式传假实现时不受影响。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zhengtong.adapters.config import Config, save
from zhengtong.adapters.fakes import FakeLlm
from zhengtong.adapters.http_llm import HttpLlm
from zhengtong.adapters.local_secrets import LocalSecrets
from zhengtong.adapters.paths import ensure_dirs
from zhengtong.adapters.system_clock import SystemClock
from zhengtong.app import build_app_from_workspace

假密钥 = "sk-test0123456789abcdefghijklmnopqrstuvwxyz"


@pytest.fixture()
def 工作区(tmp_path: Path) -> Path:
    ws = ensure_dirs(tmp_path / "ws").unwrap()
    got = save(
        ws,
        Config(
            endpoint="https://example.invalid/v1/chat/completions",
            model="some-model",
            api_key=假密钥,
            context_k=128,
        ),
    )
    assert got.is_ok
    return ws.root


# ═══ 一、真实入口必须接真实现 ═══


def test_llm端口是真适配器(工作区: Path) -> None:
    """**第 6 步最要紧的一条**：真实配置配真模型。"""
    app = build_app_from_workspace(ensure_dirs(工作区).unwrap()).unwrap()
    assert isinstance(app.ports.llm, HttpLlm), (
        f"真实运行入口拿到的是 {type(app.ports.llm).__name__}——用户会以为在调真模型，实际拿到假内容"
    )


def test_时钟端口是真时钟(工作区: Path) -> None:
    """时钟同理：假时钟下 `run-YYYYMMDD-HHMMSS` 每次都一样，
    断点续跑会认错运行（D-027 的可复现前提是「真时钟能被替换」，
    而不是「默认就是假」）。"""
    app = build_app_from_workspace(ensure_dirs(工作区).unwrap()).unwrap()
    assert isinstance(app.ports.clock, SystemClock)


def test_适配器用的是配置里的端点与模型(工作区: Path) -> None:
    """接上了还不够——**得用配置里的值**。
    端口默认是另一个端点的话，接线等于没接。"""
    app = build_app_from_workspace(ensure_dirs(工作区).unwrap()).unwrap()
    llm = app.ports.llm
    assert isinstance(llm, HttpLlm)
    assert llm.endpoint == "https://example.invalid/v1/chat/completions"
    assert llm.model == "some-model"


def test_适配器的密钥源是真实那个(工作区: Path) -> None:
    """密钥经 `secrets` 端口拿，**适配器自己不存一份**。

    （`HttpLlm` 不实现 `SecretsPort`——`redaction_patterns` 属于
    密钥源，不属于用密钥的人。上一版断言错在这儿。）
    """
    app = build_app_from_workspace(ensure_dirs(工作区).unwrap()).unwrap()
    assert isinstance(app.ports.secrets, LocalSecrets)
    assert app.ports.secrets.get_api_key() == 假密钥
    片段 = app.ports.secrets.redaction_patterns()
    assert 片段 and all(假密钥 not in p for p in 片段), "片段泄露了完整密钥"


# ═══ 二、显式传假实现时不得被覆盖 ═══


def test_显式传假实现不被覆盖(工作区: Path) -> None:
    """**接线不能变成强制**。测试与离线演练要能塞假实现，
    塞了就被真适配器顶掉的话，测试全在真网络上跑。"""
    app = build_app_from_workspace(ensure_dirs(工作区).unwrap(), llm=FakeLlm()).unwrap()
    assert isinstance(app.ports.llm, FakeLlm)
    assert not isinstance(app.ports.llm, HttpLlm)


def test_显式传真时钟不被覆盖(工作区: Path) -> None:
    from zhengtong.adapters.fakes import FakeClock

    app = build_app_from_workspace(
        ensure_dirs(工作区).unwrap(), clock_override=FakeClock()
    ).unwrap()
    assert isinstance(app.ports.clock, FakeClock)


# ═══ 三、真实端点与假端点不可混 ═══


def test_真实项目录不被污染() -> None:
    """**接线用真端点，但绝不能把真配置写进仓库或临时目录**。

    这条盯的是「为了测接线而复制配置」时容易犯的错：
    复制到 `tmp` 是安全的，复制到仓库里就是 D-158 的违规。
    """
    仓库 = Path(__file__).resolve().parent.parent
    assert not (仓库 / "tests" / "system").exists(), "测试目录里出现了 system/"
    assert not (仓库 / "tests" / "config.toml").exists()


def test_接线不写任何文件(工作区: Path) -> None:
    """装配是纯读——**不产出、不改写**。

    **显式传 `FakeLlm`**：本条验的是「装配不写文件」，
    若用真适配器就会真发网络请求到 `example.invalid`（不存在），
    失败原因会盖掉要验的东西。**一个测试只该验一件事。**
    """
    前 = sorted(p.name for p in 工作区.rglob("*"))
    with build_app_from_workspace(ensure_dirs(工作区).unwrap(), llm=FakeLlm()).unwrap() as app:
        from zhengtong.app import run_once

        assert run_once(app).is_ok
    后 = sorted(p.name for p in 工作区.rglob("*"))
    assert 前 == 后, f"装配/运行过程写入了文件：{set(后) - set(前)}"


def test_关闭会释放连接池(工作区: Path) -> None:
    """**`App.close()` 必须关掉适配器持有的连接池**。

    第 6 步接线补的：真实 LLM 持有 HTTP 连接池，
    而 `App.close()` 原先只停总线——每次装配都新建一个传输层，
    连接会一路泄漏。

    **用组合而不是继承**：`HttpLlm` 与 `SystemClock` 都标了 `@final`
    （适配器不该被继承）。让测试去继承它，等于在测一个
    「只对测试开放」的脆弱接口——真有人继承了，类型检查就会拦下。
    这里的做法是塞一个**记 close 的假传输**进真实 `HttpLlm`：
    既守住 final，又验的正是「连接池有没有被释放」。
    """

    class _记传输:
        """假传输：只记 `close` 有没有被调用。"""

        def __init__(self) -> None:
            self.关过: bool = False

        def post_json(
            self, url: str, headers: dict[str, str], body: dict[str, object]
        ) -> tuple[int, dict[str, object]]:
            raise RuntimeError("本测试不发起请求")

        def post_stream(self, url: str, headers: dict[str, str], body: dict[str, object]) -> int:
            _ = (url, headers, body)  # 同上
            raise RuntimeError("本测试不发起请求")

        def close(self) -> None:
            self.关过 = True

    传输 = _记传输()
    llm = HttpLlm(
        endpoint="https://example.invalid/v1/chat/completions",
        model="m",
        transport=传输,  # type: ignore[arg-type] —— 结构性满足 TransportPort
        secrets=LocalSecrets.from_workspace(ensure_dirs(工作区).unwrap()),
        clock=SystemClock(),
    )
    装配 = build_app_from_workspace(ensure_dirs(工作区).unwrap(), llm=llm)
    assert 装配.is_ok
    with 装配.unwrap() as app:
        _ = app
        assert not 传输.关过, "还没到 close 就关了"
    assert 传输.关过, "退出上下文后连接池没被释放"


def test_传输层可关闭(工作区: Path) -> None:
    """真传输持有 HTTP 连接。**必须有办法关**，
    否则长跑会耗尽连接池。本条锁住「适配器暴露了关闭入口」。"""
    app = build_app_from_workspace(ensure_dirs(工作区).unwrap()).unwrap()
    llm = app.ports.llm
    assert isinstance(llm, HttpLlm)
    # 没发过请求时关闭也必须安全
    llm.close()
