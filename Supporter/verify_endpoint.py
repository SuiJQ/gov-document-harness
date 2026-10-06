"""真端点验收脚本（`scripts/verify_endpoint.py`）。

## 为什么单独写脚本而不是加进测试

因为它要**真发网络请求**。放进 `pytest` 意味着：

- 每次跑测试都要网络，断了就红
- CI 环境可能没有凭据
- 失败了分不清是代码坏了还是网络抖了

所以它是**手工验收工具**，不进 `check.sh`。
判据「D-024 四项 + D-048 探测跑通真实端点」由它来证明。

## 跑法

    python scripts/verify_endpoint.py

读 `system/config.toml`，跑完五项并打印实测数据。
**不打密钥**，也不打任何含密钥的片段。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from zhengtong.adapters.config import load  # noqa: E402
from zhengtong.adapters.http_llm import HttpLlm  # noqa: E402
from zhengtong.adapters.httpx_transport import HttpxTransport  # noqa: E402
from zhengtong.adapters.local_secrets import LocalSecrets  # noqa: E402
from zhengtong.adapters.paths import ensure_dirs  # noqa: E402
from zhengtong.adapters.system_clock import SystemClock  # noqa: E402
from zhengtong.ports.llm import LlmRequest, Message, ToolSchema  # noqa: E402


def _测(名: str, llm: HttpLlm, 请求: LlmRequest) -> bool:
    起 = time.monotonic()
    got = llm.complete(请求)
    耗时 = time.monotonic() - 起
    if got.is_error:
        err = got.error
        assert err is not None
        print(f"  ✗ {名:12s} 失败（{耗时:.2f}s）  {err.code.value} {err.detail}")
        return False
    r = got.unwrap()
    尾 = f"正文={r.text.strip()[:24]!r}"
    # **usage 是可选的**（D-384）：后端不回报用量时 `r.usage` 为 None，
    # 原先直接 `.reasoning_tokens` 会 AttributeError。
    # 而这里是「请求成功」的分支——崩了就没有第 43-47 行那个失败兜底，
    # 于是成功路径反而会把脚本整崩，和「失败了要报出来」的设计相反。
    if r.usage is not None and r.usage.reasoning_tokens:
        尾 += f" 推理={r.usage.reasoning_tokens}"
    print(f"  ✓ {名:12s} 通过（{耗时:.2f}s）  {尾}")
    return True


def main() -> int:
    根 = Path(__file__).resolve().parent.parent / "政通Harness"
    ws = ensure_dirs(根).unwrap()

    got = load(ws)
    if got.is_error:
        print("✗ 读不到 system/config.toml：", got.error)
        return 1
    cfg = got.unwrap()

    sec = LocalSecrets.from_workspace(ws)
    传输 = HttpxTransport()
    llm = HttpLlm(
        endpoint=cfg.endpoint,
        model=cfg.model,
        transport=传输,
        secrets=sec,
        clock=SystemClock(),
        max_retries=1,
    )

    print(f"端点  {cfg.endpoint}")
    print(f"模型  {cfg.model}    上下文 {cfg.context_k}K")
    # **不打印密钥片段**——D-161 明令文档/日志/工具输出不得出现真实凭据片段。
    # 只报「已配置」与长度：足够确认读到了，又不泄内容。
    print(
        f"密钥  已配置（{len(sec.get_api_key())} 字符，片段数 {len(sec.redaction_patterns())}）\n"
    )

    通过 = True
    print("═══ D-048 五项探测 ═══")
    # 1 网络可达 + 2 鉴权 + 3 模型可调
    通过 &= _测(
        "网络可达",
        llm,
        LlmRequest(
            model=cfg.model,
            messages=(Message(role="user", content="回复一个字：好"),),
            temperature=0.0,
            max_tokens=512,
        ),
    )
    # 4 工具调用
    工具 = LlmRequest(
        model=cfg.model,
        messages=(Message(role="user", content="请调用 ping 工具"),),
        tools=(
            ToolSchema(
                name="ping",
                description="连通性探测工具，直接调用即可",
                parameters={"type": "object", "properties": {}},
            ),
        ),
        max_tokens=512,
    )
    起 = time.monotonic()
    got2 = llm.complete(工具)
    耗时 = time.monotonic() - 起
    if got2.is_error:
        错 = got2.error
        print(
            f"  ✗ 支持工具调用 失败（{耗时:.2f}s）{错.detail if 错 is not None else '无错误对象'}"
        )
        通过 = False
    elif got2.unwrap().tool_calls:
        tc = got2.unwrap().tool_calls[0]
        print(f"  ✓ 支持工具调用 通过（{耗时:.2f}s）  调用 {tc.name} 参数={tc.arguments}")
    else:
        # 服务端支持 tools 但模型没主动调——不阻断（D-048 精神）
        print(f"  ~ 支持工具调用 服务端接受但未触发（{耗时:.2f}s）  正文非空，可继续")
    # 5 推理开关（D-048 取代流式探测的位置）
    #
    # **原第 5 项是流式探测，已整段删除**（D-295）：
    # `HttpLlm.探测流式` 与 `Transport.post_stream` 一并作废，
    # 传输层此后只有 `post_json` 一条路径。
    #
    # 原代码调 `llm.探测流式()`，被 `except Exception` 吞成
    # 「流式能力 不可用」——**它每次都这么打，且不报错**，
    # 于是「一直在跑一个已作废的探测」这件事看不出任何异样。
    # 基于pyright 报 `HttpLlm` 无此属性，才把它翻出来。
    #
    # **本轮补上**（TODO.md 第 1 条）：探测实现在
    # `adapters/reasoning_probe.探测推理`，`--doctor` 已经在调它
    # （`cli/main.py:远端自检项`），这里只是把它接到第 5 项的位置，
    # 让这个脚本的五项真正凑齐、不再无条件打印「五项全通」。
    #
    # **守 `通过` 再跑**：探测要发 5 次请求（基准 + 四种写法），
    # 前 4 项失败时后端已经不可用，没必要再发 5 次。
    # 这与 `llm_probe.探测五项` 的三态语义一致——
    # 「没失败，它是没机会跑」（D-295），不能谎报未通过。
    if 通过:
        from zhengtong.adapters.reasoning_probe import 探测推理

        起 = time.monotonic()
        探 = 探测推理(cfg.model, llm.complete)
        耗时 = time.monotonic() - 起
        if 探.is_error:
            错3 = 探.error
            assert 错3 is not None
            print(
                f"  ✗ 推理开关 失败（{耗时:.2f}s）{错3.code.value} {错3.detail}"
            )
            通过 = False
        else:
            结论 = 探.unwrap()
            print(f"  ✓ 推理开关 通过（{耗时:.2f}s）  {结论.说明()}")
    else:
        print("  ~ 推理开关 未测（前项失败，未发出请求）")

    传输.close()
    print()
    if 通过:
        print("五项全通——D-024 / D-048 判据达成。")
        return 0
    print("有项目未通过。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
