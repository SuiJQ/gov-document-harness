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

        # **工具调用优先**：请求里带 `tools` 且系统提示要求调用工具时，
        # 返回 `tool_calls` 而不是纯文本。这是 FLASH/PRO 档能跑通的关键——
        # 假模型必须真的调用工具，D-Card 字段才能被填上。
        工具调用 = self._选工具调用(系统, 最后一条, 请求体.get("tools", []), 请求体)
        if 工具调用:
            return {
                "id": "chatcmpl-假",
                "object": "chat.completion",
                "created": 0,
                "model": 请求体.get("model", "假模型"),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": 工具调用,
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 80, "total_tokens": 180},
            }

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

    def _选工具调用(self, 系统: str, 最后一条: str, 工具列表: list[dict], 请求体: dict[str, Any]) -> list[dict]:
        """根据系统提示决定是否调用工具、调用哪个。

        **靠契约块识别阶段**，不靠阶段名——阶段名是内部标识可能改名，
        而契约块由装配器按阶段条件渲染，它的存在本身就是阶段信号。

        **优先级**：门禁取证 > D-Card 填写 > 产物写入 > 草稿提交。
        同一阶段可能需要多轮工具调用，这里每次只返回一轮，
        让程序的 `_跑工具` 循环自己驱动下一轮。

        **不重复调用**：若最后一条消息是 `role="tool"`（上一轮工具刚执行完），
        本轮返回纯文本，让模型"看到工具结果后再决定下一步"。
        否则模型会永远返回同一个 tool_calls，直到撞上 `_跑工具` 的次数上限。
        """
        import re
        可用工具 = {t["function"]["name"] for t in 工具列表}

        # 上一轮刚执行过工具 → 本轮不调用，返回纯文本让模型消化结果
        for m in reversed(请求体.get("messages", [])):
            if m.get("role") == "tool":
                return []
            if m.get("role") == "assistant":
                break

        # 门禁取证：GATING 阶段专用，契约要求行式输出 + gate.report 工具
        if "本轮**只由你取证**的门禁" in 系统 and "gate.report" in 可用工具:
            清单 = 系统.split("本轮**只由你取证**的门禁", 1)[1]
            清单 = 清单.split("：", 1)[1].split("\n", 1)[0]
            # **用正则 G\d+ 而非 startswith("G")**：清单里 "G3。请调用…"
            # 这类后缀会让 startswith 误匹配，正则只收 G 后跟数字的。
            编号 = re.findall(r"G\d+", 清单)
            return [
                {
                    "id": f"call_gate_{g.lower()}",
                    "type": "function",
                    "function": {
                        "name": "gate.report",
                        "arguments": json.dumps({
                            "gate_id": g,
                            "conclusion": "通过",
                            "evidence": f"（假模型）{g} 已复核，未见违规项",
                        }, ensure_ascii=False),
                    },
                }
                for g in 编号
            ]

        # D-Card 填写：只在 DIRECTION 阶段回填。
        # **不看阶段名**——intake 阶段的契约也提到"方向确认单"，
        # 只看关键词会提前写入，而 intake 阶段不会解析 D-Card，
        # 导致 DIRECTION 阶段 D-Card 仍为空、门禁 E040 失败。
        # 判据：intake 阶段的产出契约是「需求采集单」，
        # DIRECTION 阶段的产出契约是「方向确认单」——两者互斥。
        if "方向确认单" in 系统 and "shared.write" in 可用工具 and "需求采集单" not in 系统:
            return [{
                "id": "call_dcard",
                "type": "function",
                "function": {
                    "name": "shared.write",
                    "arguments": json.dumps({
                        "name": "方向确认单-D-Card",
                        "kind": "attachment",
                        "content": self._填D卡文本(),
                    }, ensure_ascii=False),
                },
            }]

        # 产物写入：OUTLINE / DRAFTING / FINAL 阶段，契约要求产出阶段成果
        if "产物契约" in 系统 and "shared.write" in 可用工具:
            return [{
                "id": "call_artifact",
                "type": "function",
                "function": {
                    "name": "shared.write",
                    "arguments": json.dumps({
                        "name": "阶段成果",
                        "kind": "draft",
                        "content": self._按关键词(系统, 最后一条),
                    }, ensure_ascii=False),
                },
            }]

        # 草稿提交：DRAFTING 阶段起草人提交正文
        if "draft.submit" in 可用工具:
            return [{
                "id": "call_draft",
                "type": "function",
                "function": {
                    "name": "draft.submit",
                    "arguments": json.dumps({
                        "content": self._按关键词(系统, 最后一条),
                        "version": 1,
                    }, ensure_ascii=False),
                },
            }]

        return []

    def _填D卡文本(self) -> str:
        """生成完整的 D-Card 文本（D1–D9），供 DIRECTION 阶段填写。

        格式与 `_尝试更新d卡` 的正则匹配对齐：`D1 xxx` / `D2 xxx` 等。
        """
        return (
            "D1 主件：通知，关于防汛工作的通知，一式三份\n"
            "D2 配套件：无\n"
            "D3 关键时点：定稿日期 2026-10-20，报送截止 2026-10-22\n"
            "D4 事实边界：不得假设未核实的水文数据；不得引用非官方来源\n"
            "D5 数据状态：降雨量数据来自市气象局（可靠，时效 24h 内）；"
            "水位数据来自水文站（可靠，时效 2h 内）\n"
            "D6 主体排序：市应急管理局 → 市水利局 → 各区县防汛指挥部"
            "（按应急响应级别排序）\n"
            "D7 硬约束清单：格式依 GB/T 9704-2012；事实标注三态；"
            "主体排序 ≥3 时附依据；政策依据全可溯\n"
            "D8 语域特征：正式公文语域，庄重简明，不用修辞夸张\n"
            "D9 例外接口清单：无折中方案，不适用\n"
        )

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
