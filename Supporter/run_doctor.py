"""起网关 → 生成配置 → 跑程序自己的 `--doctor`。

## 这个脚本不碰 Harness 一行代码

它做的是**真人第一次使用程序时做的事**：
准备工作区、填配置、然后启动程序。配置用程序自己提供的
`save()` 写入（那是正式途径，不是私改格式）。

唯一的差别是端点指向本地网关——那是我们要的：
观测「程序到底往模型发了什么」就得有个能看见的对面。

## 网关必须和 doctor 同进程

`--doctor` 是子进程，网关得在它整个运行期间活着。
所以不能「先起网关再另开一条命令跑 doctor」——
那期间网关已死。这一份脚本把三件事放在同一个进程里。
"""

from __future__ import annotations

import shutil
import subprocess
import importlib.util
import sys
import tempfile
from pathlib import Path
from typing import Any

#: 项目根（主体在同级的 `政通Harness/`）。
HARNESS = Path(__file__).resolve().parents[1] / "政通Harness"
sys.path.insert(0, str(HARNESS / "src"))
sys.path.insert(0, str(HARNESS))

# **`smoke_server` 按文件路径加载，不写 `from smoke_server import ...`。**
#
# 裸 import 依赖 `sys.path` 里有 Supporter 目录，类型检查器不认这条
# （`extraPaths` 在主体配置里，为开发工具改主体的检查配置是本末倒置）——
# 基于pyright 直接报 `Import "smoke_server" could not be resolved`。
#
# 用 `extraPaths` 或 `type: ignore` 都能消掉那条报错，但那是把真脆弱藏起来：
# 一旦单独运行本脚本而 sys.path 没设对，它照样断。动态加载则**当场生效**，
# 且不依赖任何检查器配置。
_规格 = importlib.util.spec_from_file_location(
    "smoke_server", Path(__file__).resolve().parent / "smoke_server.py"
)
assert _规格 is not None and _规格.loader is not None, "读不到 smoke_server.py"
_smoke = importlib.util.module_from_spec(_规格)
_规格.loader.exec_module(_smoke)

#: `smoke_server.start` 的真实签名见该文件；此处标注为可调用即可——
# 动态加载拿不到静态类型，标成 `Any` 是**如实**表达，不是掩盖。
start: Any = _smoke.start

from zhengtong.adapters.config import Config, save  # noqa: E402
from zhengtong.app import 工作区  # noqa: E402


def main() -> int:
    端点, 模型, 服务 = start()
    print(f"══ 网关已起：{端点}")

    根 = Path(tempfile.mkdtemp(prefix="doctor-"))
    shutil.copytree(HARNESS / "content", 根 / "content")
    for 子 in ("system", "data", "user"):
        (根 / 子).mkdir(exist_ok=True)

    配置 = Config(endpoint=端点, model="假模型-v1", api_key="local-test-key", context_k=128)
    结果 = save(工作区(根), 配置)
    print(f"══ 配置写入：{'成功' if 结果.is_ok else 结果.error}")
    print(f"══ 工作区：{根}\n")

    程序 = subprocess.run(  # noqa: S603
        [str(HARNESS / ".venv/bin/zhengtong"), "--root", str(根), "--doctor"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    print("════════ 程序输出 ════════")
    print(程序.stdout)
    if 程序.stderr.strip():
        print("──────── stderr ────────")
        print(程序.stderr)
    print(f"════════ 退出码：{程序.returncode} ════════")
    print(f"\n网关共收到 {模型.调用次数} 次请求")
    for i, 体 in enumerate(模型.收到的请求, 1):
        print(
            f"  第{i}次：模型={体.get('model')} 消息数={len(体.get('messages', []))} "
            f"温度={体.get('temperature')} 流式={体.get('stream')}"
        )

    服务.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
