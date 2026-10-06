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


def main() -> int:
    端点, 模型, 服务 = start()
    print(f"══ 网关已起：{端点}")

    根 = Path(tempfile.mkdtemp(prefix="consult-smoke-"))
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
        界面 = ZhengTongApp(
            桥=事件桥接(调试=True),
            根=根,
            取编排器=应用.编排器,
            闸=应用.问答,
            终端提示=None,
            推理提示=应用.推理提示,
        )
        async with 界面.run_test() as 试点:
            # 用户视角：往诉求框输入
            await 试点.press("tab")  # 焦点移到诉求框
            await 试点.press(*("某单位拟就××事项发文，请出具咨询意见。"))
            await 试点.pause()
            # 用户视角：按 Ctrl+Enter 提交
            await 试点.press("ctrl+enter")
            # 等 worker 跑完（worker 是 daemon 线程，多等几轮）
            for _ in range(60):
                await 试点.pause()
                if not 界面._跑着:
                    break
            await 试点.pause()

    asyncio.run(跑())
    应用.close()
    服务.shutdown()

    print(f"\n════════ 程序输出（维护者视角） ════════")
    print(f"网关共收到 {模型.调用次数} 次请求")
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
