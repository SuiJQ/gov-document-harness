"""起网关 → TUI headless 输入诉求 → 跑咨询档。

三个视角合一：
- 用户：`App.run_test()` + `press()` 模拟按键（adapters/tui.py）
- 维护者：`--debug` 事件桥接输出
- 模型：`smoke_server` 代替被请求的大模型做回答，记录全部请求体
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

HARNESS = Path(__file__).resolve().parents[1] / "政通Harness"
sys.path.insert(0, str(HARNESS / "src"))
sys.path.insert(0, str(HARNESS))

_规格 = importlib.util.spec_from_file_location(
    "smoke_server", Path(__file__).resolve().parent / "smoke_server.py"
)
assert _规格 is not None and _规格.loader is not None
_smoke = importlib.util.module_from_spec(_规格)
_规格.loader.exec_module(_smoke)
start: Any = _smoke.start

from zhengtong.adapters.config import Config, save  # noqa: E402
from zhengtong.app import build_app_from_workspace, 工作区  # noqa: E402
from zhengtong.adapters.tui import ZhengTongApp  # noqa: E402
from zhengtong.presentation.tui import 事件桥接  # noqa: E402


_TIER_INDEX = {"consult": 0, "flash": 1, "pro": 2}
_REQUIREMENTS = {
    "consult": "某单位拟就××事项发文，请出具咨询意见。",
    "flash": "某单位拟就防汛工作发文，请出具成文 Flash。",
    "pro": "某单位拟就防汛工作发文，请出具成文 Pro。",
}


def main() -> int:
    import sys
    档 = sys.argv[1] if len(sys.argv) > 1 else "consult"
    下标 = _TIER_INDEX[档]
    诉求 = _REQUIREMENTS[档]

    端点, 模型, 服务 = start()
    print(f"══ 网关已起：{端点}  档位：{档}")

    根 = Path(tempfile.mkdtemp(prefix=f"{档}-smoke-"))
    shutil.copytree(HARNESS / "content", 根 / "content")
    for 子 in ("system", "data", "user"):
        (根 / 子).mkdir(exist_ok=True)

    配置 = Config(endpoint=端点, model="假模型-v1", api_key="local-test-key", context_k=128)
    结果 = save(工作区(根), 配置)
    print(f"══ 配置写入：{'成功' if 结果.is_ok else 结果.error}")
    print(f"══ 工作区：{根}\n")

    应用 = build_app_from_workspace(工作区(根)).unwrap()

    import asyncio

    async def 跑() -> None:
        global 界面
        界面 = ZhengTongApp(
            桥=事件桥接(调试=True),
            根=根,
            取编排器=应用.编排器,
            闸=应用.问答,
            终端提示=None,
            推理提示=应用.推理提示,
        )
        async with 界面.run_test() as 试点:
            from textual.widgets import TextArea, RadioSet
            from zhengtong.adapters.tui import RequestBox
            await 试点.pause()
            # 用户视角：焦点移到诉求 TextArea
            框 = 界面.query_one(RequestBox)
            框.query_one(TextArea).focus()
            await 试点.pause()
            # 用户视角：切档位（调 _预选，和 on_mount 走同一路径）
            if 下标 > 0:
                界面.query_one(RequestBox)._预选(下标)
                await 试点.pause()
            # 用户视角：输入诉求
            await 试点.press(*诉求)
            await 试点.pause()
            # 用户视角：按 Ctrl+Enter 提交
            await 试点.press("ctrl+enter")
            await 试点.pause()
            # 等 worker 跑完（worker 是后台线程，用 sleep 轮询）
            import asyncio as _asyncio
            for _ in range(120):
                await _asyncio.sleep(0.5)
                if not 界面._跑着:
                    break
            await _asyncio.sleep(0.5)

    asyncio.run(跑())
    应用.close()
    服务.shutdown()

    print(f"\n════════ 程序输出（维护者视角） ════════")
    失败 = 界面._桥.末次失败 if 界面 else ""
    if 失败:
        print(f"❌ 运行失败：{失败}")
    else:
        print(f"✅ 运行成功（无失败记录）")
    print(f"网关共收到 {模型.调用次数} 次请求")
    投影 = 界面._桥.投影
    print(f"\n════════ 事件流（调试投影） ════════")
    for r in 投影.records:
        print(f"  {r}")
    print(f"\n════════ 阶段成果（{len(投影.artifacts)} 件） ════════")
    for 名称, 内容 in 投影.artifacts:
        print(f"  📄 {名称}（{len(内容)} 字）")
    print(f"\n════════ 错误（{len(投影.errors)} 条） ════════")
    for e in 投影.errors:
        print(f"  ❌ {e}")
    for i, 体 in enumerate(模型.收到的请求, 1):
        msgs = 体.get("messages", [])
        系统内容 = "".join(
            str(m.get("content", "")) for m in msgs if m.get("role") == "system"
        )
        print(f"\n  第{i}次请求：")
        print(f"    model={体.get('model')} msgs={len(msgs)} "
              f"temp={体.get('temperature')} stream={体.get('stream')}")
        print(f"    system 含『内容包』: {'内容包' in 系统内容}")
        print(f"    system 含『降级』: {'降级' in 系统内容 or '未接入' in 系统内容}")
        print(f"    末位 role={msgs[-1].get('role') if msgs else None}")
        print(f"    含产物引用: {'产物' in 系统内容 or '引用' in 系统内容}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
