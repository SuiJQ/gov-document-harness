"""本地假模型端点：扮演 OpenAI 兼容服务，供端到端冒烟用。

## 它是什么，不是什么

**是**：一个按 OpenAI 风格应答的 HTTP 服务，让程序能**完整跑起来**——
装配、装配提示词、发请求、解析响应、走状态机、过门禁、落盘、驱动 TUI。

**不是**：对真实模型的模拟。它**永远答得好**，
所以它测不出「模型不按格式调工具」「模型编造」这类问题。
那要等真端点那一轮。

## 为什么按 D-004 原样收

D-004：程序**不拼接、不改写 URL**。所以这个服务只认 `POST /v1/chat/completions`，
客户端填什么路径我就得提供什么路径——不能让程序去迁就端点。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class _模型:
    """按阶段应答的内容库。

    同一阶段被多次问到时给出同一段，避免程序因随机性而表现不一——
    **冒烟测试要的是可复现**，不是逼真。
    """

    def __init__(self) -> None:
        self.调用次数 = 0
        self.收到的请求: list[dict[str, Any]] = []
        self._锁 = threading.Lock()

    def 应答(self, 请求体: dict[str, Any]) -> dict[str, Any]:
        with self._锁:
            self.调用次数 += 1
            self.收到的请求.append(请求体)

        系统 = " ".join(
            str(m.get("content", ""))
            for m in 请求体.get("messages", [])
            if m.get("role") == "system"
        )
        最后一条 = ""
        for m in reversed(请求体.get("messages", [])):
            if m.get("role") == "user":
                最后一条 = str(m.get("content", ""))
                break

        正文 = self._按关键词(系统, 最后一条)
        return {
            "id": "chatcmpl-假",
            "object": "chat.completion",
            "created": 0,
            "model": 请求体.get("model", "假模型"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": 正文},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 80, "total_tokens": 180},
        }

    def _门禁行式(self, 系统: str) -> str | None:
        """门禁取证阶段的行式结论；非该阶段返回 `None`。

        **靠「本轮只由你取证」块识别阶段**，不靠阶段名——
        阶段名是内部标识，可能改名；而这块只有指派非空时才渲染，
        它的存在本身就是「这是一次门禁取证」的可靠信号。

        **只答被指派的门禁**：多答会被 `解析结论` 按
        「该角色是否有权为这道门禁签字」丢弃（D-084），
        白写不说，还会让冒烟误判成「有结论却被判缺席」。
        """
        标 = "本轮**只由你取证**的门禁"
        if 标 not in 系统:
            return None
        清单 = 系统.split(标, 1)[1].split("：", 1)[1].split("\n", 1)[0]
        编号 = [x.strip() for x in 清单.split("、") if x.strip().startswith("G")]
        if not 编号:
            return None
        return "\n".join(
            f"{g} | 通过 | 证据：（假模型）第 {i} 段已复核（本条为冒烟桩证据）"
            for i, g in enumerate(编号, start=1)
        )

    def _按关键词(self, 系统: str, 诉求: str) -> str:
        """按系统提示与诉求里的角色关键词挑内容。

        **挑不出就返回明确的话，而不是空串**——空正文会让下游崩，
        那样就分不清是模型的问题还是程序的问题了。
        """
        # **门禁取证要先判，且必须排在关键词分支之前**（第 14 步 5b）。
        #
        # 装配器按 `装载条件.限阶段` 在 GATING 阶段装入「门禁取证输出契约」
        # 与「本轮**只由你取证**的门禁」两块。契约要求行式输出：
        # `门禁号 | 结论 | 证据：…`——而原先这里对「门禁」关键词一律回
        # 一句散文（「裁决：红线检查通过…」），**那一句读不出任何结论**。
        #
        # 后果：用它冒烟时 12 道门禁全缺席，D-221 缺席判据转用户裁决，
        # 冒烟跑到门禁段就断。而它**答得「好」**——散文本身完全合理，
        # 所以问题不在这个服务的能力，而在它不知道契约。
        行式 = self._门禁行式(系统)
        if 行式 is not None:
            return 行式

        全 = 系统 + 诉求
        if "裁决" in 全 or "门禁" in 全 or "审核" in 全:
            return "裁决：红线检查通过，无 E1–E14 命中问题。"
        if "审稿" in 全 or "审核意见" in 全 or "退回" in 全:
            return "审稿意见：结构完整，建议微调过渡句。"
        if "大纲" in 全 or "提纲" in 全:
            return "一、总体要求\n二、主要措施\n三、组织保障\n\n以上大纲供主理人审定。"
        if "方向" in 全 or "确认单" in 全:
            return "写作方向确认单：主题、范围、格式已列明，请主理人确认。"
        if "成果" in 全 or "汇总" in 全 or "归档" in 全:
            return "阶段性成果：正文已成稿，请主理人阅定。"
        if "正文" in 全 or "成文" in 全 or "起草" in 全 or "撰写" in 全:
            return (
                "关于防汛工作的通知\n\n"
                "各有关单位：\n\n"
                "为做好今年防汛工作，现就有关事项通知如下。\n"
                "一、切实加强组织领导。\n"
                "二、认真排查隐患。\n"
                "三、强化值班值守。\n\n"
                "特此通知。\n"
            )
        return "（假模型）已收到请求，将按要求办理。"


class _处理(BaseHTTPRequestHandler):
    # 由 `start` 在建子类时注入（见文件末尾）。
    #
    # **默认值给 `None` 而不是裸注解**：裸注解 `模型: _模型` 只是声明，
    # `__init__` 里并没有赋值——真跑起来若某条路径没经过 `start` 的注入，
    # 就在 `self.模型.应答` 处炸 `AttributeError`。给出默认值后，
    # 炸的地方变成下面那句显式检查，且能说清是谁没注入。
    模型: _模型 | None = None

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """**别打访问日志**——会淹没冒烟报告里真正要看的东西。

        形参名**必须照基类写**（`format` / `*args`），不能图中文可读
        改成 `格式` / `*参数`：覆盖方法时形参名是契约的一部分，
        基于pyright 会判签名不兼容（实测过，改中文名立刻报错）。
        """

    def do_POST(self) -> None:  # noqa: N802
        长度 = int(self.headers.get("content-length", "0"))
        原始 = self.rfile.read(长度) if 长度 else b"{}"
        try:
            请求体 = json.loads(原始.decode("utf-8"))
        except ValueError:
            self.send_error(400, "不是合法 JSON")
            return
        if self.模型 is None:  # 走到这里说明 `start` 没注入——那是本脚本的 bug
            self.send_error(500, "服务未注入模型")
            return
        if self.模型 is None:  # 走到这里说明 `start` 没注入——那是本脚本的 bug
            self.send_error(500, "服务未注入模型")
            return
        响应 = self.模型.应答(请求体)
        载荷 = json.dumps(响应, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(载荷)))
        self.end_headers()
        self.wfile.write(载荷)


def start(路径: str = "/v1/chat/completions") -> tuple[str, _模型, ThreadingHTTPServer]:
    """起服务。返回 `(完整 URL, 模型, 服务)`。"""
    模型 = _模型()

    class 子(_处理):
        pass

    子.模型 = 模型
    服务 = ThreadingHTTPServer(("127.0.0.1", 0), 子)
    threading.Thread(target=服务.serve_forever, daemon=True).start()
    端口 = 服务.server_address[1]
    return f"http://127.0.0.1:{端口}{路径}", 模型, 服务
