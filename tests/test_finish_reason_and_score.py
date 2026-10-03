"""结束原因不合格不许当完成（D-268）+ 检索出口带相关度（D-259）。

## D-268：原判比实际轻

原案写的是「`raw_finish_reason` 写了没人读」。复查发现**真正的问题是
更深一层**：

`_结束原因()` 把未知值归一化成 `STOP`，而 `STOP` 的含义是「正常结束」。
于是 ——

- `FinishReason.LENGTH`（输出被截断）**在全仓生产代码里从未被读过**，
  截断稿被当成正常完成交付出去。`http_llm.py` 自己的注释写着
  「只认 `length` 会把『输出被截断』判成『正常完成』——交付的是残稿」，
  **而那正是当时发生的事**；
- `FinishReason.CONTENT_FILTER`（服务端合规拦截）同样没人读，
  D-032 明写「红线，硬退回」，实际是「照收不误」。

所以这不只是「一个字段没人读」，是**两种坏稿子都会静默交付**。

## D-259：给档位不给原始分数

`SearchHit.score`（BM25）此前只用于排序，产出后即丢弃，
模型看到的只有 `- {ref}：{snippet}`——看不出哪条值得读。

**不把原始浮点摆给模型**：BM25 分数依赖语料规模，跨查询不可比；
原始浮点还会诱发模型在没标定过的量纲上做阈值推理。
改为**相对于本次最高分**的档位（强/中/弱）。
"""

from __future__ import annotations

import pytest

from zhengtong.ports.llm import (
    FinishReason,
    LlmResponse,
    TokenUsage,
    归一化结束原因,
    是已知结束原因,
)
from zhengtong.ports.retriever import SearchHit


def _响应(原因: FinishReason, *, 原文: str = "", 文本: str = "正文") -> LlmResponse:
    return LlmResponse(
        text=文本,
        finish_reason=原因,
        usage=TokenUsage(),
        raw_finish_reason=原文,
    )


class Test归一化与已知性:
    @pytest.mark.parametrize(
        ("原值", "期望"),
        [
            ("stop", FinishReason.STOP),
            ("length", FinishReason.LENGTH),
            ("max_tokens", FinishReason.LENGTH),  # 别名
            ("model_length", FinishReason.LENGTH),  # 别名
            ("CONTENT_FILTER", FinishReason.CONTENT_FILTER),  # 大小写
            ("content-filter", FinishReason.CONTENT_FILTER),  # 连字符
            ("tool_calls", FinishReason.TOOL_CALLS),
            ("function_call", FinishReason.TOOL_CALLS),
        ],
    )
    def test_别名都认(self, 原值: str, 期望: FinishReason) -> None:
        assert 归一化结束原因(原值) is 期望

    def test_未知归一化为STOP但认不出来(self) -> None:
        """**两件事必须分开**：归一化给分流用，`是已知结束原因` 给兜底用。

        只拿归一化结果看不出「真停」与「不认识」的区别——
        而这个区别正是 D-268 的全部要害。
        """
        assert 归一化结束原因("incomplete") is FinishReason.STOP
        assert not 是已知结束原因("incomplete"), "不认识却报成认识"

    def test_已知的都算认识(self) -> None:
        assert 是已知结束原因("length")
        assert 是已知结束原因("STOP")  # 大小写
        assert 是已知结束原因("tool_calls")

    @pytest.mark.parametrize("怪值", [None, 123, [], {}])
    def test_非字符串不算认识(self, 怪值: object) -> None:
        assert not 是已知结束原因(怪值)


class Test编排层拦截:
    """**判据走真实编排路径**，不直接调 `查结束原因`——
    直接调只能证明函数对，证明不了它**接在链路上**（D-233 同型）。"""

    def _编(self, 响应: LlmResponse):  # type: ignore[no-untyped-def]
        from zhengtong.adapters.fakes import FakeLlm, FakeStorage
        from zhengtong.domain.ids import RunId
        from zhengtong.orchestration import 编排器

        return 编排器(
            llm=FakeLlm(scripted=[响应]),
            storage=FakeStorage(),
            模型名="m",
            门禁取证=lambda 门禁: {},
            运行号=RunId("r-fin"),
        )

    def _跑(self, 响应: LlmResponse):  # type: ignore[no-untyped-def]
        from zhengtong.domain.tier import TierId

        编 = self._编(响应)
        return 编.跑到终(档位=TierId.PRO, 需求="就旱情写个通知")

    def test_截断必须拦下(self) -> None:
        """**这是 D-268 的核心**：截断稿不得当作完成交付。"""
        r = self._跑(_响应(FinishReason.LENGTH, 原文="length", 文本="半截稿子"))
        assert r.is_error, "输出被截断却跑通了——残稿会被当完整稿交付"
        assert "E036" in str(getattr(r.error, "code", "")), r.error

    def test_合规拦截必须拦下(self) -> None:
        """D-032：合规拦截是红线，硬退回。"""
        r = self._跑(_响应(FinishReason.CONTENT_FILTER, 原文="content_filter"))
        assert r.is_error, "被合规拦截的内容竟照收"
        assert "E037" in str(getattr(r.error, "code", "")), r.error

    def test_未知结束原因必须拦下(self) -> None:
        """认不出来就不能断定完整——与「只认 length」是同一类错误。"""
        r = self._跑(_响应(FinishReason.STOP, 原文="incomplete"))
        assert r.is_error, "未识别的结束原因被当成了正常完成"
        assert "E038" in str(getattr(r.error, "code", "")), r.error

    def test_报错里带服务端原文(self) -> None:
        """用户要能据此判断自己端点的行为。"""
        r = self._跑(_响应(FinishReason.STOP, 原文="weird_vendor_code"))
        assert r.is_error
        assert "weird_vendor_code" in str(getattr(r.error, "detail", ""))

    def test_正常结束不被新规则拦下(self) -> None:
        """**不能把正常路径也堵死**——否则这条判据毫无意义。

        只断言「没有被 E036/E037/E038 拦下」，**不断言整体跑通**：
        Pro 档跑到门禁段会因「无取证结论」失败（E041），
        那是另一条与本条无关的判据。
        """
        r = self._跑(_响应(FinishReason.STOP, 原文="stop", 文本="完整正文"))
        码 = str(getattr(r.error, "code", "")) if r.is_error else ""
        assert 码 not in {"E036", "E037", "E038"}, f"正常结束被新规则拦下：{r.error}"

    def test_工具调用不算未结束(self) -> None:
        """`tool_calls` 是正常态——它在对照表里，不该被拦。"""

        r = self._跑(
            LlmResponse(
                text="",
                finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(),
                raw_finish_reason="tool_calls",
            )
        )
        assert not r.is_error or "E03" not in str(getattr(r.error, "code", ""))


class Test检索带相关度档位:
    def _搜(self, 分数们: list[float]):

        return self._跑检索(
            tuple(
                SearchHit(ref=f"政策{i}.md", snippet=f"片段{i}", score=s)
                for i, s in enumerate(分数们)
            )
        )

    def _跑检索(self, 命中):
        from zhengtong.adapters.fakes import (
            FakeContentPack,
            FakeRetriever,
            FakeStorage,
            FakeUserFiles,
            FakeValidators,
        )
        from zhengtong.domain.ids import RunId
        from zhengtong.domain.role import RoleId
        from zhengtong.services.tools import handlers

        依 = handlers.工具依赖(
            storage=FakeStorage(),
            user_files=FakeUserFiles(),
            retriever=FakeRetriever(hits=命中),
            content_pack=FakeContentPack(),
            validators=FakeValidators(),
            run_id=RunId("r-1"),
        )
        return handlers.kb_search(依, RoleId.TEAM_LEAD, {"query": "旱情"}).unwrap()

    def test_每条都带档位(self) -> None:
        """数**条目行**（以 `- [` 开头），不要数字符——
        「按相关度降序」这句里也有「序」字相近的词，
        按字符数会被标题行干扰（我第一版就这么数错了）。"""
        文 = self._搜([12.0, 3.0, 0.5])
        条目 = [行 for 行 in 文.splitlines() if 行.startswith("- [")]
        assert len(条目) == 3, 文
        assert all(行.startswith(("- [强]", "- [中]", "- [弱]")) for 行 in 条目), 条目

    def test_最高分恒为强(self) -> None:
        文 = self._搜([12.0, 3.0, 0.5])
        首行 = 文.splitlines()[1]
        assert "强" in 首行, f"最高分那条不是强：{首行}"

    def test_整体都很弱时最高分仍是强(self) -> None:
        """全都很低说明「本次查询整体匹配差」，不是「这条也不强」。

        整体不匹配属「没检索到」，由空结果分支管——
        档位不该在这里越权表达「没找到」。
        """
        文 = self._搜([0.01, 0.005])
        assert "强" in 文.splitlines()[1]

    def test_不给原始浮点(self) -> None:
        """**原始 BM25 分数不摆给模型**：跨查询不可比，且会诱发阈值推理。"""
        文 = self._搜([12.0, 3.0])
        assert "12.0" not in 文, f"把原始分数摆给了模型：{文}"
        assert "3.0" not in 文, f"把原始分数摆给了模型：{文}"

    def test_降序说明存在(self) -> None:
        文 = self._搜([12.0, 3.0])
        assert "降序" in 文

    def test_空结果不产档位(self) -> None:
        文 = self._跑检索(())
        assert "未找到" in 文
        assert "强" not in 文 and "弱" not in 文
