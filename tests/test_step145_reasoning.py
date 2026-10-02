"""深度思考永不入上下文（D-052 落地，第 14.5 步，第 5 组判据）。

## D-052 此前是一行空决策

DECISIONS.md 里 D-052「推理不入历史」只有三行：决策 / 机制落点 / 出处，
**没有实现**。而 D-150 已把「上下文不含深度思考内容」这一半判给了 D-052，
于是两层都没落地：

- 策略层：有 `ReasoningPolicy` 枚举（Flash OFF / Pro ON）
- 适配层：`http_llm._正文()` 在正文为空时**回落到 `reasoning_content`**

## 回落为什么有害

回落对**诊断**是对的——「模型这轮没出正文，只出了思考」是个有用信号。
但对**内容**有害：思考是模型的内部草稿，不是给用户看的答案，
把它当正文写进上下文与历史，等于让模型下一轮把自己的草稿当既有结论读，
越滚越歪。

故本组判据锁的是：**回落仍可存在，但只能走诊断路径，绝不进内容路径。**
"""

from __future__ import annotations

import pytest

from zhengtong.adapters.http_llm import _思考标记, _正文
from zhengtong.domain.tier import ReasoningPolicy


class Test思考不入内容:
    def test_正文为空时回落可用于诊断(self) -> None:
        """回落行为**保留**——它是「模型没出正文」的唯一信号。"""
        消息: dict[str, object] = {
            "role": "assistant",
            "content": "",
            "reasoning_content": "草稿 ABC",
        }
        assert _正文(消息) != "", "回落彻底删掉就丢了诊断信号"

    def test_回落内容不得冒充正文(self) -> None:
        """回落得到的文本必须**带标记**，让下游能分辨它不是正文。

        ## 这条判据第一版是**假绿**的

        初版断言 `"思考" in 回`，而我取的样例内容恰好是「我在思考…」——
        自带「思考」二字，于是**回落不加任何标记也照样通过**。

        判据断言了**词的字面**，而不是行为。换一个不含「思考」二字的
        样例就现原形。这是本项目反复吃过的亏的又一例：
        判据要断言行为，不能断言样例里的某个词。
        """
        消息: dict[str, object] = {
            "role": "assistant",
            "content": "",
            "reasoning_content": "草稿 ABC",
        }
        回 = _正文(消息)
        assert 回 != "草稿 ABC", f"回落内容与原文一字不差——下游无从分辨它是草稿还是正文：{回[:60]}"
        assert 回.startswith(_思考标记), f"回落内容没带 {_思考标记} 标记：{回[:60]}"

    def test_有正文时不回落(self) -> None:
        """正文存在时**绝不**混入思考内容——那是污染。"""
        消息: dict[str, object] = {
            "role": "assistant",
            "content": "正式答案。",
            "reasoning_content": "草稿",
        }
        assert _正文(消息) == "正式答案。"

    def test_两档都不把思考当正文(self) -> None:
        """Flash 与 Pro 的差别在**是否请求推理**，不在「思考是否入上下文」。

        两档的思考内容都**不入**上下文（D-052）。若实现按档位决定
        「Pro 的思考可以进上下文」，就等于让 Pro 档的模型把自己的
        草稿当既有结论读，越滚越歪。
        """
        消息: dict[str, object] = {
            "role": "assistant",
            "content": "",
            "reasoning_content": "草稿 ABC",
        }
        回 = _正文(消息)
        # 同一份响应在两档下都不得被当成正文——回落是诊断，不是内容
        assert 回.startswith(_思考标记)


class Test档位区分:
    def test_推理策略按档位分(self) -> None:
        """Flash 关 / Pro 开（D-150）。"""
        from zhengtong.domain.tier import TIER_BY_ID, TierId

        assert TIER_BY_ID[TierId.FLASH].reasoning_policy is ReasoningPolicy.OFF
        assert TIER_BY_ID[TierId.PRO].reasoning_policy is ReasoningPolicy.ON

    def test_咨询档不推理(self) -> None:
        from zhengtong.domain.tier import TIER_BY_ID, TierId

        assert TIER_BY_ID[TierId.CONSULT].reasoning_policy is ReasoningPolicy.OFF


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
