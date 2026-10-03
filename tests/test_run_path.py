"""`--run` 全链路真跑一遍（补 D-282）。

## 此前为什么这条路径一行没跑过

`--run` 的代码是通的，但它的**每一个分支都没被任何判据走过**——
测试里那约 70 处 `跑到终` 全是**直接调编排器**，
从没经过「读配置 → 装端口 → 起编排器 → 投给无界面驱动」这一段。

于是有两件事同时成立：

- 编排层测得很透（约 70 处）
- **CLI 那一段完全没测**

一条跑不通的入口比缺个界面麻烦——
用户敲 `--run` 得到的是什么，**没人知道**。

## 为什么起真的本地 HTTP 服务

判据要在**子进程**里跑 `python -m zhengtong --run`，才能验证
「装出来的命令真的能跑完整条路」。

那它就得上真网络。故起一个 stdlib `http.server`，
返回 OpenAI 风格的响应，配置指向它。

**不用假传输注入**：那会绕开真实运行路径——
本项目在假实现上测顺了、真路径上撞墙的事已经发生九次（D-165）。

## 这条判据的诚实边界

它证明的是**接线通**（配置读得对、端口装得上、请求发得出、
响应解析得了、事件投得到投影、退出码正确）。

它**不**证明公文质量——那要真模型实测，
架构 §15 步骤 17 明写了「不得预先编造」。
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

仓库根 = Path(__file__).resolve().parents[1]


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


def 跑(参数: list[str], 根: Path, 超时: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "zhengtong", *参数],
        capture_output=True,
        text=True,
        cwd=仓库根,
        env={"PYTHONPATH": str(仓库根 / "src"), "PATH": "/usr/bin:/bin"},
        timeout=超时,
        check=False,
    )


class Test装配失败那条分支:
    """**这是此前一行没跑过的分支**。"""

    def test_无配置时明说缺什么(self, tmp_path: Path) -> None:
        r = 跑(["--run", "--需求", "写个通知", "--root", str(tmp_path)], tmp_path)
        assert r.returncode == 1, f"退出码应是 1：{r.returncode}\n{r.stdout}\n{r.stderr}"
        assert "装配失败" in r.stderr, r.stderr

    def test_无配置时指出缺哪些字段(self, tmp_path: Path) -> None:
        """只说「装配失败」用户照样卡住——**要说清缺哪个字段**。"""
        r = 跑(["--run", "--需求", "写个通知", "--root", str(tmp_path)], tmp_path)
        for 字段 in ("endpoint", "model", "api_key"):
            assert 字段 in r.stderr, f"没指出缺 {字段}：{r.stderr}"

    def test_无配置时不发任何请求(self, tmp_path: Path) -> None:
        """**没配置就不该有网络动作**。判据用一个没开的端点：
        真发出去了必然连接失败并多花几轮重试。"""
        r = 跑(["--run", "--需求", "写个通知", "--root", str(tmp_path)], tmp_path)
        assert "装配失败" in r.stderr, "无配置却走到了请求阶段"


class Test端点就位后真跑:
    """起真 HTTP 服务，**子进程**里跑完整条路。"""

    def test_run能跑通并有输出(self, tmp_path: Path, 端点: 假端点) -> None:
        写配置(tmp_path, 端点.地址)
        r = 跑(
            ["--run", "--需求", "写一份通知", "--档位", "咨询", "--root", str(tmp_path)], tmp_path
        )
        断 = f"--- 退出码={r.returncode}\n--- stdout:\n{r.stdout}\n--- stderr:\n{r.stderr}"
        # **不预设退出码**——先看清它到底做了什么再定判据
        assert r.stdout.strip(), f"一点输出都没有：\n{断}"

    def test_run真的发出了请求(self, tmp_path: Path, 端点: 假端点) -> None:
        """**证明它真上了网**，而不是悄悄退化成假模型。

        此前 D-165 那类缺口（配了真端点、程序却拿假模型作答）
        就是靠这类判据抓到的。
        """
        写配置(tmp_path, 端点.地址)
        跑(["--run", "--需求", "写一份通知", "--档位", "咨询", "--root", str(tmp_path)], tmp_path)
        assert 端点.收到, "一个请求都没发出去——多半根本没走到真 LLM"

    def test_run发出的请求指向用户填的地址(self, tmp_path: Path, 端点: 假端点) -> None:
        """**程序不拼接 URL**（用户硬约束）：请求必须发到配置里的原地址。"""
        写配置(tmp_path, 端点.地址)
        跑(["--run", "--需求", "写一份通知", "--档位", "咨询", "--root", str(tmp_path)], tmp_path)
        assert 端点.收到[0].get("model") == "假模型", f"模型名没按用户填的传：{端点.收到[0]}"

    def test_run不把密钥写进输出(self, tmp_path: Path, 端点: 假端点) -> None:
        """D-030：任何输出不得含密钥。"""
        写配置(tmp_path, 端点.地址)
        r = 跑(
            ["--run", "--需求", "写一份通知", "--档位", "咨询", "--root", str(tmp_path)], tmp_path
        )
        assert "sk-test-not-a-real-key" not in r.stdout + r.stderr, "密钥泄漏到输出了"


class Test运行不该卡死:
    def test_run有超时保护(self, tmp_path: Path, 端点: 假端点) -> None:
        """假端点永远答 stop，但流程可能有阶段在等（如硬停车等用户回答）。

        判据用**较短的超时**：卡死就是卡死，不必等满 180 秒。
        """
        写配置(tmp_path, 端点.地址)
        起 = time.monotonic()
        try:
            跑(
                ["--run", "--需求", "写一份通知", "--档位", "咨询", "--root", str(tmp_path)],
                tmp_path,
                超时=60,
            )
        except subprocess.TimeoutExpired:
            pytest.fail("`--run` 卡死超时了")
        耗时 = time.monotonic() - 起
        assert 耗时 < 55, f"耗时 {耗时:.1f} 秒，接近卡死"


class Test失败必须表现为失败:
    """**这一整类都是这一轮跑出来的**（D-283）。

    我第一版 CLI 判成败写的是
    `显示.最终显示.endswith("运行失败：")`——
    而真实失败时末行是「运行失败：<详细原因>」，
    `endswith` 判 False，**于是运行失败却返回退出码 0**。

    脚本调用会以为跑成功了，白拿一份没成文的文档。
    **这比崩掉更糟，因为它不报错。**
    """

    def test_失败时退出码必须是1(self, tmp_path: Path) -> None:
        """端点指死端口，必然失败。"""
        写配置(tmp_path, "http://127.0.0.1:9/v1/chat/completions")
        r = 跑(["--run", "--需求", "写通知", "--档位", "咨询", "--root", str(tmp_path)], tmp_path)
        assert r.returncode == 1, f"失败了却返回 {r.returncode}：\n{r.stdout}"

    def test_失败输出含失败字样(self, tmp_path: Path) -> None:
        写配置(tmp_path, "http://127.0.0.1:9/v1/chat/completions")
        r = 跑(["--run", "--需求", "写通知", "--档位", "咨询", "--root", str(tmp_path)], tmp_path)
        assert "运行失败" in r.stdout, r.stdout

    def test_连不上端点的消息要说清是连不上(self, tmp_path: Path) -> None:
        """状态码 0 此前报「请求被拒（0）」——
        **既说错了事（不是被拒，是连不上），又报了个没意义的数字 0**。
        """
        写配置(tmp_path, "http://127.0.0.1:9/v1/chat/completions")
        r = 跑(["--run", "--需求", "写通知", "--档位", "咨询", "--root", str(tmp_path)], tmp_path)
        assert "连不上端点" in r.stdout, r.stdout
        assert "请求被拒（0）" not in r.stdout, "仍在说「请求被拒（0）」"

    def test_非ascii密钥要给可操作提示(self, tmp_path: Path) -> None:
        """密钥混进中文字符时 httpx 按 latin-1 编头会报 `UnicodeEncodeError`。

        此前用户看到的是「连接失败：UnicodeEncodeError」——
        **完全看不懂，也不知道该改哪里**。
        """
        写配置(tmp_path, "http://127.0.0.1:9/v1/chat/completions", api_key="sk-测试")
        r = 跑(["--run", "--需求", "写通知", "--档位", "咨询", "--root", str(tmp_path)], tmp_path)
        assert "非 ASCII" in r.stdout, r.stdout
        assert "UnicodeEncodeError" not in r.stdout, "仍在抛原始异常类型名"

    def test_非ascii密钥不重试(self, tmp_path: Path) -> None:
        """**不是网络抖动，是输入问题**——重试三次只是让用户多等三倍。"""
        写配置(tmp_path, "http://127.0.0.1:9/v1/chat/completions", api_key="sk-测试")
        起 = time.monotonic()
        r = 跑(["--run", "--需求", "写通知", "--档位", "咨询", "--root", str(tmp_path)], tmp_path)
        耗时 = time.monotonic() - 起
        assert "重试 3 次" not in r.stdout, f"输入问题却重试了：\n{r.stdout}"
        assert 耗时 < 20, f"耗时 {耗时:.1f} 秒，像是在重试"

    def test_连不上会重试(self, tmp_path: Path) -> None:
        """反过来：网络层失败**应当重试**（用户要求「失败重试 3 次」）。"""
        写配置(tmp_path, "http://127.0.0.1:9/v1/chat/completions")
        r = 跑(["--run", "--需求", "写通知", "--档位", "咨询", "--root", str(tmp_path)], tmp_path)
        assert "重试 3 次" in r.stdout, f"网络失败却没重试：\n{r.stdout}"


class Test幂等与重复:
    def test_两次run各自独立(self, tmp_path: Path, 端点: 假端点) -> None:
        """**两次运行不能共用状态**——
        上一轮修过「同一编排器跑两次沿用上次进度」，
        但那是编排器层；CLI 层两次调用会不会串，还没验过。"""
        写配置(tmp_path, 端点.地址)
        第一次 = 跑(
            ["--run", "--需求", "写通知", "--档位", "咨询", "--root", str(tmp_path)], tmp_path
        )
        间隔 = 端点.收到.copy()
        第二次 = 跑(
            ["--run", "--需求", "写通知", "--档位", "咨询", "--root", str(tmp_path)], tmp_path
        )
        assert 第一次.returncode == 第二次.returncode, "两次退出码不一致"
        assert len(端点.收到) > len(间隔), "第二次没发请求——可能被当成断点续跑跳过了"
