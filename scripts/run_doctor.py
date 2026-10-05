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
import sys
import tempfile
from pathlib import Path

#: 项目根（脚本在 `scripts/` 下，故上溯一层）。
HARNESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARNESS / "src"))
sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from smoke_server import start  # noqa: E402

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
        print(f"  第{i}次：模型={体.get('model')} 消息数={len(体.get('messages', []))} "
              f"温度={体.get('temperature')} 流式={体.get('stream')}")

    服务.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
