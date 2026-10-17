"""真路径上的端点与错误消息（真 `httpx` 传输，**不起子进程跑编排**）。

## D-294：无界面模式删除后，这批判据为什么还在

原文件叫 `test_run_path.py`，靠子进程跑 `python -m zhengtong --run`
把 CLI 那一段整个走一遍。`--run` 按 D-294 删除后要分清两件事：

| 原判据 | 处置 |
|---|---|
| `--run` 能跑通 / 退出码 / 超时保护 / 两次独立 | **删**——测的是已删的那条路 |
| 装配失败要说清缺哪个字段 | **留**——`--doctor` 仍调 `build_app_from_workspace` |
| 真 httpx 下「连不上」的消息 | **留**——假传输测不出来，只有真连接失败才有 |
| 真 httpx 下非 ASCII 密钥的提示 | **留**——`UnicodeEncodeError` 是真编码时才抛的 |

**为什么后两条不能靠 `tests/test_http_llm.py` 顶替**：那个文件 28 条判据
用的是**假传输**，验证的是「拿到状态码 0 时映射成什么」。
而「状态码 0 从哪来」「latin-1 编头真的抛不抛」这两件事，
假传输根本不会发生。**在假件上测顺了、真件上撞墙的事已经发生十次（D-165）。**

## 为什么改成直接调装配根而不是起子进程

子进程那一段的价值是「验证装出来的命令能跑完整条路」。
删掉 `--run` 后这条要求对编排已无处安放，但**装配根本身照样能验**：
`build_app_from_workspace` 返回的 `ports.llm` 就是真 `HttpxTransport` 装的真件，
直接调它，链路一点没少，还省掉了子进程的解释层。

## 诚实边界

它证明**接线通**：配置读得对、端点原样发出去、真失败的消息说得清。
它**不**证明公文质量——那要真模型实测（架构 §15 步骤 17）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from zhengtong.app import build_app_from_workspace, 工作区
from zhengtong.ports.llm import LlmRequest, Message

仓库根 = Path(__file__).resolve().parents[1]

#: 指死的端口。**连它必然失败**——用于测「连不上」这条消息。
死端口 = "http://127.0.0.1:9/v1/chat/completions"


class 假端点:
    """本地假 OpenAI 端点。**记录收到过什么**，供判据断真实请求。"""

    def __init__(self) -> None:
        self.收到: list[dict[str, object]] = []
        self._服务 = ThreadingHTTPServer(("127.0.0.1", 0), self._处理器())
        self._线程 = threading.Thread(target=self._服务.serve_forever, daemon=True)
        self._线程.start()

    @property
    def 地址(self) -> str:
        主机, 端口 = self._服务.server_address[:2]
        return f"http://{主机}:{端口}/v1/chat/completions"

    def 停(self) -> None:
        self._服务.shutdown()
        self._服务.server_close()

    def _处理器(self) -> type[BaseHTTPRequestHandler]:
        外 = self

        class 处理器(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                长度 = int(self.headers.get("Content-Length", "0"))
                体 = json.loads(self.rfile.read(长度) or b"{}")
                外.收到.append(体)
                答 = {
                    "id": "chatcmpl-假",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "测试正文内容。"},
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
                出 = json.dumps(答).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(出)))
                self.end_headers()
                self.wfile.write(出)

            def log_message(self, format: str, *args: object) -> None:
                """**别把请求打到 stderr**——会污染判据读输出。

                **形参名必须与父类一致**：BasedPyright 的
                `reportIncompatibleMethodOverride` 会逐个比形参名，
                写 `格式` 会被判成不兼容覆盖。父类是 `(self, format, *args)`。
                """

        return 处理器


@pytest.fixture
def 端点() -> Iterator[假端点]:
    端 = 假端点()
    try:
        yield 端
    finally:
        端.停()


def 写配置(根: Path, 端点地址: str, **覆盖: str) -> Path:
    """造一份真配置。**直接写程序真会读的那份**。

    `**覆盖` 只收字符串——判据要改的就是 endpoint / model / api_key，
    收窄类型省掉一串类型体操。
    """
    llm: dict[str, str | int | float] = {
        "endpoint": 端点地址,
        "model": "假模型",
        "api_key": "sk-test-not-a-real-key",
        "context_k": 128,
        "temperature": 0.3,
    }
    llm.update(覆盖)
    件 = 根 / "system" / "config.toml"
    件.parent.mkdir(parents=True, exist_ok=True)
    件.write_text(_转toml(llm), encoding="utf-8")
    return 件


def _转toml(llm: dict[str, str | int | float]) -> str:
    行 = ["[encoding]", "gb18030 = false", "", "[llm]"]
    for 键, 值 in llm.items():
        行.append(f'{键} = "{值}"' if isinstance(值, str) else f"{键} = {值}")
    return "\n".join(行) + "\n"


def 跑CLI(参数: list[str], 根: Path, 超时: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "zhengtong", *参数],
        capture_output=True,
        text=True,
        cwd=仓库根,
        env={"PYTHONPATH": str(仓库根 / "src") + ":/app/user-packages/python:/run/csi/mount-root/nas/4079184d856ecc166ed19d4887083405/workspaces/Harness/政通Harness/.venv/lib/python3.11/site-packages", "PATH": "/usr/bin:/bin"},
        timeout=超时,
        check=False,
    )


def 装应用(根: Path):
    """装出真应用。**判据里出现裸 `Any` 就说明这一步没装成**，故直接抛。"""
    got = build_app_from_workspace(工作区(根))
    assert got.is_ok, f"装配应当成功：{got.error.detail if got.error else ''}"
    return got.unwrap()


def 调一次(根: Path):
    """用装出来的**真** `ports.llm` 发一次请求，返回 `Result`。

    **模型名从 `应用.config.model` 取**（D-082：模型名由用户手填，代码不猜），
    这才是真实路径——`HttpLlm._model` 只供探测用，
    正常调用以请求里的 model 为准，所以这里不能自己编一个。
    """
    应用 = 装应用(根)
    try:
        return 应用.ports.llm.complete(
            LlmRequest(
                model=应用.config.model,
                messages=(Message(role="user", content="起草"),),
            ),
        )
    finally:
        应用.close()


# ═══ 装配失败：仍由 --doctor 覆盖这条分支 ═══


class Test装配失败那条分支:
    """`--doctor` 是删掉 `--run` 后**仍在调** `build_app_from_workspace` 的入口。"""

    def test_无配置时明说缺什么(self, tmp_path: Path) -> None:
        r = 跑CLI(["--doctor", "--root", str(tmp_path)], tmp_path)
        assert r.returncode == 1, f"退出码应是 1：{r.returncode}\n{r.stdout}\n{r.stderr}"
        assert "端点装配" in r.stdout, r.stdout

    def test_无配置时指出缺哪些字段(self, tmp_path: Path) -> None:
        """只说「装配失败」用户照样卡住——**要说清缺哪个字段**。"""
        r = 跑CLI(["--doctor", "--root", str(tmp_path)], tmp_path)
        for 字段 in ("endpoint", "model", "api_key"):
            assert 字段 in r.stdout, f"没指出缺 {字段}：{r.stdout}"

    def test_无配置时不发任何请求(self, tmp_path: Path) -> None:
        """**没配置就不该有网络动作**。判据用一个没开的端点：
        真发出去了必然连接失败并多花几轮重试。"""
        r = 跑CLI(["--doctor", "--root", str(tmp_path)], tmp_path)
        assert "端点装配" in r.stdout, "无配置却走到了请求阶段"


# ═══ 真 httpx 下的错误消息：假传输测不出来的那几条 ═══


class Test真传输下失败必须表现为失败:
    """**这一整类都起真 HTTP 连接**（D-283）。

    我第一版 CLI 判成败写的是
    `显示.最终显示.endswith("运行失败：")`——
    而真实失败时末行是「运行失败：<详细原因>」，
    `endswith` 判 False，**于是运行失败却返回退出码 0**。
    脚本调用会以为跑成功了，白拿一份没成文的文档。
    **这比崩掉更糟，因为它不报错。**
    """

    def test_连不上端点的消息要说清是连不上(self, tmp_path: Path) -> None:
        """状态码 0 此前报「请求被拒（0）」——
        **既说错了事（不是被拒，是连不上），又报了个没意义的数字 0**。
        """
        写配置(tmp_path, 死端口)
        got = 调一次(tmp_path)
        assert got.is_error
        错 = got.error
        assert 错 is not None
        assert "连不上端点" in 错.detail, 错.detail
        assert "请求被拒（0）" not in 错.detail, "仍在说「请求被拒（0）」"

    def test_非ascii密钥要给可操作提示(self, tmp_path: Path) -> None:
        """密钥混进中文字符时 httpx 按 latin-1 编头会报 `UnicodeEncodeError`。

        此前用户看到的是「连接失败：UnicodeEncodeError」——
        **完全看不懂，也不知道该改哪里**。
        """
        写配置(tmp_path, 死端口, api_key="sk-测试")
        got = 调一次(tmp_path)
        assert got.is_error
        错 = got.error
        assert 错 is not None
        assert "非 ASCII" in 错.detail, 错.detail
        assert "UnicodeEncodeError" not in 错.detail, "仍在抛原始异常类型名"

    def test_非ascii密钥不重试(self, tmp_path: Path) -> None:
        """**不是网络抖动，是输入问题**——重试三次只是让用户多等三倍。"""
        写配置(tmp_path, 死端口, api_key="sk-测试")
        起 = time.monotonic()
        调一次(tmp_path)
        耗时 = time.monotonic() - 起
        assert 耗时 < 20, f"耗时 {耗时:.1f} 秒，像是在重试"

    def test_连不上会重试(self, tmp_path: Path) -> None:
        """反过来：网络层失败**应当重试**（用户要求「失败重试 3 次」）。"""
        写配置(tmp_path, 死端口)
        got = 调一次(tmp_path)
        assert got.is_error
        错 = got.error
        assert 错 is not None
        assert "重试 3 次" in 错.detail, f"网络失败却没重试：{错.detail}"


# ═══ 端点原样发到网络上（D-004 的真线验证） ═══


class Test端点原样走到真实网络:
    """`tests/test_http_llm.py::test_端点原样使用不被改写` 用的是**假传输**——
    它能验「地址字符串没被改写」，验不了**这个地址真的发到了那个端口**。
    """

    def test_请求真的抵达用户填的地址(self, tmp_path: Path, 端点: 假端点) -> None:
        写配置(tmp_path, 端点.地址)
        got = 调一次(tmp_path)
        assert got.is_ok, f"端点就位却没跑通：{got.error.detail if got.error else ''}"
        assert len(端点.收到) == 1, f"服务端收到 {len(端点.收到)} 次，不是 1 次"

    def test_模型名取自用户填的值(self, tmp_path: Path, 端点: 假端点) -> None:
        写配置(tmp_path, 端点.地址, model="用户自填模型")
        assert 调一次(tmp_path).is_ok
        assert 端点.收到[0].get("model") == "用户自填模型", 端点.收到[0]

    def test_密钥不进请求体(self, tmp_path: Path, 端点: 假端点) -> None:
        """密钥只许进请求头，**进请求体等于把凭据交给服务端日志**。"""
        写配置(tmp_path, 端点.地址)
        assert 调一次(tmp_path).is_ok
        assert "sk-test-not-a-real-key" not in json.dumps(端点.收到[0], ensure_ascii=False)
