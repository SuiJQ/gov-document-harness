"""问答闸门：模型提问 → 用户回答 → 强制等待（D-287）。

## 用户拍板的三条（2026-10-03）

1. **无人应答则强制等待**——不是硬失败、不是带着空字段继续
2. **答案结构为字符串**——不改成结构化，等真跑看会不会丢信息
3. **30 种死事件不补**——所以提问**不会出现在事件流里**

第 3 条的直接后果，也是本文件最重要的一条：

> **界面只能靠闸门本身知道「有个问题在等」，没有事件可听。**
> 这是设计的必然，不是缺陷——但必须写下来，
> 否则后人会在事件流里找那个不存在的事件。

## 为什么必须是阻塞式

实测：**编排器里没有任何暂停/等待机制**
（`threading.Event` / `Condition` / 等待 —— 零命中）。

所以「硬停车」不可能实现成「挂起编排器等用户」——
只能让**提问的那一方阻塞**，直到另一线程把答案送来。

代价是 worker 线程被占住。**而这正好符合硬停车的语义**：
车停了就是停了。

## 为什么要有超时（强制等待 ≠ 永久挂死）

「强制等待」的本意是**不许带着空字段继续**（D-007），
不是**让用户被永久挂住**。

`--run` 无界面模式下没有任何人回答，
没有超时的强制等待就是**一个永久挂起的进程**——
用户得知道它在等什么、等多久。

故：默认 30 分钟无应答 → 明确报错停下，**不静默、不降级**。

## 本文件验四件事

1. 有问有答，答案**原样**回到提问方
2. 无人应答时**阻塞**，不是返回空、不是报错、不是跳过
3. 超时后**明确失败**，且说清在等谁、等了多久
4. 并发下不串答——两个提问不会互相拿错答案
"""

from __future__ import annotations

import threading
import time

import pytest

from zhengtong.presentation.gate import 问答闸门


class Test有问有答:
    def test_答案原样回到提问方(self) -> None:
        闸 = 问答闸门()
        闸.挂答复(lambda 字段: "用户答：事由是防汛")
        assert 闸.问(({"key": "D1", "question": "主件是什么"},)) == "用户答：事由是防汛"

    def test_字段送到答复方(self) -> None:
        闸 = 问答闸门()
        收到: list[tuple[dict[str, object], ...]] = []
        闸.挂答复(lambda f: (收到.append(f), "好")[-1])
        闸.问(({"key": "D1"}, {"key": "D2"}))
        assert len(收到) == 1 and len(收到[0]) == 2, f"字段没原样送到：{收到!r}"

    def test_无字段也能答(self) -> None:
        闸 = 问答闸门()
        闸.挂答复(lambda _f: "好")
        assert 闸.问(()) == "好"


class Test无人应答必须强制等待:
    """**第 1 条拍板的核心。**不许带着空字段继续（D-007）。"""

    def test_无人应答时阻塞而非立即返回(self) -> None:
        闸 = 问答闸门(超时秒=2.0)
        起 = time.monotonic()
        with pytest.raises(TimeoutError):
            闸.问(({"key": "D1"},))
        耗时 = time.monotonic() - 起
        assert 耗时 >= 1.5, f"只等了 {耗时:.2f} 秒——那不是强制等待，是立刻放弃"

    def test_无人应答不得返回空字符串(self) -> None:
        """**最危险的一种失败**：返回空串，模型以为「用户没意见」，
        于是按自己的猜测继续写——而 D-007 禁止编造。"""
        闸 = 问答闸门(超时秒=1.0)
        with pytest.raises(TimeoutError):
            闸.问(({"key": "D1"},))

    def test_超时说清在等谁等了多久(self) -> None:
        """D-027 三件事：失败在哪、报了什么、怎么处理。"""
        闸 = 问答闸门(超时秒=1.0)
        with pytest.raises(TimeoutError) as 错:
            闸.问(({"key": "D1"},))
        信息 = str(错.value)
        assert "1.0" in 信息 or "1 秒" in 信息, f"没说清等了多久：{信息}"

    def test_没挂答复也算无人应答(self) -> None:
        """挂了但答案为空，等同于没人。"""
        闸 = 问答闸门(超时秒=1.0)
        闸.挂答复(lambda _f: "")
        with pytest.raises(TimeoutError):
            闸.问(({"key": "D1"},))


class Test并发不串答:
    """**worker 线程只有一条，但别把这条当保障。**"""

    def test_两个提问各拿各的答案(self) -> None:
        闸 = 问答闸门(超时秒=5.0)
        结果: dict[str, str] = {}

        def 提问(名: str) -> None:
            try:
                结果[名] = 闸.问(({"key": 名},))
            except TimeoutError:
                结果[名] = "超时"

        def 答复(字段: tuple[dict[str, object], ...]) -> str:
            名 = str(字段[0]["key"])
            return f"答案给{名}"

        闸.挂答复(答复)
        线程们 = [threading.Thread(target=提问, args=(f"D{i}",)) for i in range(1, 4)]
        for t in 线程们:
            t.start()
        for t in 线程们:
            t.join(timeout=10)
        assert 结果 == {"D1": "答案给D1", "D2": "答案给D2", "D3": "答案给D3"}, 结果

    def test_重复挂答复覆盖旧的(self) -> None:
        """换界面时不该留着上一个的回调。"""
        闸 = 问答闸门(超时秒=2.0)
        闸.挂答复(lambda _f: "旧的")
        闸.挂答复(lambda _f: "新的")
        assert 闸.问(({"key": "D1"},)) == "新的"


class Test答复方抛异常时怎么办:
    """**答复方就是界面代码，它会崩。**崩了不能把提问方永久挂住。"""

    def test_答复方崩了要能救回来(self) -> None:
        闸 = 问答闸门(超时秒=5.0)
        闸.挂答复(lambda _f: (_ for _ in ()).throw(RuntimeError("界面炸了")))
        with pytest.raises(RuntimeError):
            闸.问(({"key": "D1"},))
        # 换一个好答复方，之后还能问
        闸.挂答复(lambda _f: "正常了")
        assert 闸.问(({"key": "D1"},)) == "正常了"
