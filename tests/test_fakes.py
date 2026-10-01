"""`fakes.py` 测试 —— 全部测试的基线。

## 为什么这个文件的测试格外要紧

`fakes.py` 是**所有测试的参照物**。它若与真实实现行为不一致，
那么基于它写下的断言全部可能是错的——**包括本项目其他测试文件的断言**。

所以判据比"覆盖率"更高一档：不要求每行都被执行，
而要求**每个假实现的行为都能说出理由**，且理由与真实现一致。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zhengtong.adapters import fakes
from zhengtong.adapters.fs_storage import FsStorage
from zhengtong.adapters.paths import Workspace, ensure_dirs
from zhengtong.domain.errors import ErrorCode
from zhengtong.domain.event import Span
from zhengtong.domain.role import RoleId
from zhengtong.ports.llm import FinishReason, LlmRequest, LlmResponse, TokenUsage
from zhengtong.ports.retriever import SearchHit


def _req(role: str | None = None, gate: str | None = None, rnd: int | None = None) -> LlmRequest:
    return LlmRequest(
        model="测试模型",
        messages=(),
        temperature=0.3,
        span=Span(role=role, gate=gate, round_no=rnd),
    )


def _resp(text: str, cid: str) -> LlmResponse:
    return LlmResponse(
        text=text,
        finish_reason=FinishReason.STOP,
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
        call_id=cid,
    )


# ═══ 缺陷 A：call_id 用位置序号，违反 D-065 ═══


def test_call_id不随调用次数变化() -> None:
    """**已证实缺陷**。原实现 `f"fake-{len(self.calls)}"` 用位置序号，
    而 D-065 明文规定 `call_id` 「不由角色、轮次或位置序号生成」。

    后果不是"不够真"，而是**基于它的幂等测试恒真**：
    断点续跑重放同一逻辑调用，真实现给同一 call_id（幂等生效），
    假实现给新序号（看着像新调用），于是"重放是否幂等"这条判据失效。
    """
    llm = fakes.FakeLlm()
    ids = {llm.complete(_req("范正章")).unwrap().call_id for _ in range(5)}
    assert len(ids) == 1, f"同一次逻辑调用得到 {len(ids)} 个 call_id：{ids}"


def test_不同角色得到不同call_id() -> None:
    llm = fakes.FakeLlm()
    范 = llm.complete(_req("范正章")).unwrap().call_id
    陈 = llm.complete(_req("陈纲举")).unwrap().call_id
    assert 范 != 陈, "不同角色的调用被判为同一次"


def test_不同门禁得到不同call_id() -> None:
    llm = fakes.FakeLlm()
    a = llm.complete(_req("范正章", gate="G0")).unwrap().call_id
    b = llm.complete(_req("范正章", gate="G3")).unwrap().call_id
    assert a != b


def test_不同轮次得到不同call_id() -> None:
    llm = fakes.FakeLlm()
    a = llm.complete(_req("范正章", rnd=1)).unwrap().call_id
    b = llm.complete(_req("范正章", rnd=2)).unwrap().call_id
    assert a != b


def test_不同温度得到不同call_id() -> None:
    """温度是"这次调用做了什么"的判别因子之一（D-066 各角色可不同温度）。"""
    llm = fakes.FakeLlm()
    a = llm.complete(LlmRequest(model="m", messages=(), temperature=0.3)).unwrap().call_id
    b = llm.complete(LlmRequest(model="m", messages=(), temperature=0.9)).unwrap().call_id
    assert a != b


def test_无span时仍能生成call_id() -> None:
    got = fakes.FakeLlm().complete(_req()).unwrap().call_id
    assert got.startswith("fake-")
    assert len(got) > 5


def test_重放同一请求call_id一致() -> None:
    """这才是 D-065 幂等记账真正依赖的性质。"""
    llm = fakes.FakeLlm()
    req = _req("陈纲举", gate="G3", rnd=2)
    assert llm.complete(req).unwrap().call_id == llm.complete(req).unwrap().call_id


# ═══ 缺陷 B：脚本用尽后静默回落 ═══


def test_严格模式下脚本用尽报错() -> None:
    """**已证实缺陷**。原实现队列用尽后静默给桩文本——
    编排层"多发一次请求"这种真 bug 会在假环境里照跑通过，
    接上真模型才炸。**假实现不得比真实现宽松。**"""
    llm = fakes.FakeLlm(scripted=[_resp("甲", "1")], strict_script=True)
    assert llm.complete(_req()).is_ok
    second = llm.complete(_req())
    assert second.is_error
    assert second.error is not None
    assert second.error.code is ErrorCode.LLM_REQUEST_FAILED


def test_严格模式错误信息须说清原因() -> None:
    llm = fakes.FakeLlm(scripted=[_resp("甲", "1")], strict_script=True)
    _ = llm.complete(_req())
    got = llm.complete(_req())
    assert got.is_error
    assert got.error is not None
    assert "脚本已用尽" in got.error.detail


def test_非严格模式仍能跑通() -> None:
    """第 4 步要能"跑得通"，默认宽松是有意的——
    但**两种模式都必须有测试**，否则默认会悄悄变成唯一模式。"""
    llm = fakes.FakeLlm(scripted=[_resp("甲", "1")])
    assert llm.complete(_req()).is_ok
    assert llm.complete(_req()).is_ok


def test_脚本按序消费() -> None:
    llm = fakes.FakeLlm(scripted=[_resp("第一", "1"), _resp("第二", "2")])
    assert llm.complete(_req()).unwrap().text == "第一"
    assert llm.complete(_req()).unwrap().text == "第二"


def test_每次调用都被记录() -> None:
    """编排层测试靠 `calls` 断言"传对了东西"。"""
    llm = fakes.FakeLlm()
    _ = llm.complete(_req("范正章"))
    _ = llm.complete(_req("陈纲举"))
    assert [c.span.role for c in llm.calls if c.span] == ["范正章", "陈纲举"]


def test_失败请求也被记录() -> None:
    """失败的那次调用同样要留痕，否则查不出编排在哪一步失败。"""
    llm = fakes.FakeLlm(fail_with=ErrorCode.LLM_REQUEST_FAILED)
    assert llm.complete(_req()).is_error
    assert len(llm.calls) == 1


# ═══ 缺陷 C：假时钟时间倒流 ═══


def test_假时钟拒绝负数睡眠() -> None:
    """**已证实的缺陷**。真实现 `sleep(-1)` 会挂起，
    假实现若照单全收则事件时间戳倒退，破坏 `ts_monotonic` 单调性假设。"""
    with pytest.raises(ValueError, match="负数"):
        fakes.FakeClock().sleep(-1)


def test_假时钟零睡眠可用() -> None:
    """拒绝负数不等于拒绝一切——`sleep(0)` 是合法的"不等待"。"""
    c = fakes.FakeClock()
    before = c.now()
    c.sleep(0)
    assert c.now() == before


def test_假时钟正向推进() -> None:
    c = fakes.FakeClock()
    before = c.now()
    c.sleep(2.5)
    assert c.now() == before + 2.5


def test_假时钟非零起点() -> None:
    """**不用 0 作起点**：真实 Unix 纪元之后，
    用 0 会让"是否设置了时间戳"这类断言失去区分力。"""
    assert fakes.FakeClock().now() > 1_000_000_000


def test_假时钟stamp稳定() -> None:
    c = fakes.FakeClock()
    assert c.stamp() == c.stamp()


# ═══ 假密钥源 ═══


def test_密钥片段不含完整密钥() -> None:
    """脱敏器自己**不能持有密钥全文**（D-030）：
    它自己的日志里也不该出现密钥。"""
    sec = fakes.FakeSecrets(api_key="sk-abcdefghijklmnopqrstuvwxyz")
    patterns = sec.redaction_patterns()
    assert all(sec.api_key not in p for p in patterns)


def test_自定义脱敏片段优先() -> None:
    sec = fakes.FakeSecrets(patterns=("自定义片段",))
    assert sec.redaction_patterns() == ("自定义片段",)


def test_取密钥() -> None:
    assert fakes.FakeSecrets(api_key="k1").get_api_key() == "k1"


# ═══ 假存储 ═══


def test_存储往返() -> None:
    s = fakes.FakeStorage()
    assert s.write_atomic("data/x.txt", "内容").is_ok
    got = s.read_text("data/x.txt")
    assert got.is_ok
    assert got.unwrap() == "内容"


def test_读不存在返回失败() -> None:
    s = fakes.FakeStorage()
    got = s.read_text("data/没有.txt")
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.FILE_UNREADABLE


def test_不存在项不计入列目录() -> None:
    s = fakes.FakeStorage()
    _ = s.write_atomic("data/r1/a.txt", "A")
    assert s.list_dir("data/r1") == ("a.txt",)


def test_列目录前缀不越界() -> None:
    """`data/r10/...` 不该被 `data/r1` 的前缀匹配捞走。
    这类"字符串前缀当成路径前缀"的错在真实目录上很难复现。"""
    s = fakes.FakeStorage()
    _ = s.write_atomic("data/r1/甲.txt", "A")
    _ = s.write_atomic("data/r10/乙.txt", "B")
    assert s.list_dir("data/r1") == ("甲.txt",)


def test_列目录不递归() -> None:
    """端口契约：列目录**不递归**。"""
    s = fakes.FakeStorage()
    _ = s.write_atomic("data/r1/深层/x.txt", "X")
    assert s.list_dir("data/r1") == ("深层",)


def test_覆盖写() -> None:
    """与真实原子写一致：写两次是覆盖，不是追加。"""
    s = fakes.FakeStorage()
    _ = s.write_atomic("data/x.txt", "第一次")
    _ = s.write_atomic("data/x.txt", "第二次")
    got = s.read_text("data/x.txt")
    assert got.is_ok
    assert got.unwrap() == "第二次"


def test_draft_paper_ok受开关控制() -> None:
    assert fakes.FakeStorage(draft_ok=True).draft_paper_ok("任一角色", 1) is True
    assert fakes.FakeStorage(draft_ok=False).draft_paper_ok("任一角色", 1) is False


# ═══ 假校验器 ═══


def test_默认全过() -> None:
    """第 4 步要能跑到 DONE，故默认全过。"""
    v = fakes.FakeValidators()
    assert v.run_hard_constraint({}).unwrap().passed is True
    assert v.run_structure({}).unwrap().passed is True


def test_硬约束不通过时带阻塞项() -> None:
    """红线不通过必须**可定位**：无锚点即 blocking。"""
    v = fakes.FakeValidators(pass_hard=False)
    got = v.run_hard_constraint({})
    assert got.is_ok
    res = got.unwrap()
    assert res.passed is False
    assert res.issues


def test_记录跑过哪些校验() -> None:
    v = fakes.FakeValidators()
    _ = v.run_hard_constraint({})
    _ = v.run_structure({})
    assert v.ran == ["hard", "structure"]


# ═══ 假内容包 ═══


def test_内容包桩文本带标记() -> None:
    """桩文本必须**可辨认**——否则可能被误当真实产出交付出去。"""
    got = fakes.FakeContentPack().load_role_prompt(RoleId.TEAM_LEAD)
    assert got.is_ok
    assert fakes.STUB_MARK in got.unwrap()


def test_内容包版本() -> None:
    assert fakes.FakeContentPack(version_value="1.2.3").version() == "1.2.3"


# ═══ 假检索器 ═══


def test_检索默认返回空而非编造命中() -> None:
    """**编造召回结果会掩盖检索层的问题**——空结果是诚实的。"""
    got = fakes.FakeRetriever().search("旱情")
    assert got.is_ok
    assert got.unwrap() == ()


def test_检索遵守top_k() -> None:
    hits = tuple(SearchHit(ref=f"r{i}", snippet=f"s{i}", score=1.0) for i in range(5))
    r = fakes.FakeRetriever(hits=hits)
    assert len(r.search("q", 2).unwrap()) == 2
    assert len(r.search("q", 99).unwrap()) == 5


def test_记录建索引用的文档() -> None:
    """测试要能断言"喂给检索器的确实是这些"。"""
    r = fakes.FakeRetriever()
    _ = r.build(("甲", "乙"))
    assert r.built_with == ("甲", "乙")


# ═══ 假用户文件源 ═══


def test_只给文件名不给内容() -> None:
    """D-022/D-023：AI 初始**只**获得文件名，内容必须经 read_text 按需取。"""
    f = fakes.FakeUserFiles()
    got = f.list_registered()
    assert got == ()
    assert not hasattr(got, "content")


def test_内容须按需取且读不到时报错() -> None:
    f = fakes.FakeUserFiles(contents={"甲.txt": "内容"})
    assert f.read_text("甲.txt").unwrap() == "内容"
    assert f.read_text("没有.txt").is_error


# ═══ 导入即校验 ═══


def test_导入时即校验协议() -> None:
    """`_assert_protocols()` 在模块导入时执行。
    本测试本身是它的活证据：模块能被导入，说明八个假实现都满足 Protocol。"""
    assert hasattr(fakes, "FakeClock")


def test_stub_text可辨认() -> None:
    assert fakes.STUB_MARK in fakes.stub_text("某类")


# ── D-142：返回裸值的方法同样要查权限 ──────────────────────────────
#
# D-128/D-129 曾给 `read_text` 补过权限检查，但**只补了返回 Result 的方法**。
# `exists`（bool）与 `list_dir`（tuple）被漏掉了——
# 上层照样会拿它们的返回值做判断，契约一样成立。


@pytest.mark.parametrize(
    "越权路径",
    [
        pytest.param("../../etc/passwd", id="目录穿越"),
        pytest.param("/etc/passwd", id="绝对路径"),
        pytest.param("data/../../x.txt", id="中途折返"),
        pytest.param("..", id="父目录"),
    ],
)
def test_假实现exists对越权路径须与真实现一致(越权路径: str) -> None:
    """**返回类型是 `bool`，但契约与 `Result` 方法一样。**

    原实现 `return rel_path in self.files` 不做任何权限检查，
    对 `../../etc/passwd` 也返回 `True`，而真实实现在同一输入上
    返回 `False`（它走 `_resolve_for_read`，越权即否）。

    后果很具体：上层若据此判断「文件已就绪，可以读取」，
    在假环境里照跑通过，接真适配器才发现读不到——
    **失败点在上层，看不出根因在假实现。**
    这正是 D-112 说的「假实现一旦更宽松，就不再是可信参照物」。
    """
    假 = fakes.FakeStorage()
    假.files["../../etc/passwd"] = "系统文件"
    assert not 假.exists(越权路径), f"{越权路径!r} 在假实现上被判定为存在"


def test_假实现list_dir对越权路径须返回空() -> None:
    """原实现按前缀直接匹配，`list_dir("../../etc")` 返回
    `("x.txt",)`，真实实现返回 `()`。"""
    假 = fakes.FakeStorage()
    假.files["../../etc/x.txt"] = "x"
    assert 假.list_dir("../../etc") == ()
    assert 假.list_dir("..") == ()


def test_假实现权限检查不得影响正常路径() -> None:
    """**修 D-142 不能改坏正常读取。**"""
    假 = fakes.FakeStorage()
    假.files["user/材料.txt"] = "用户材料"
    假.files["data/产出.txt"] = "产出"

    assert 假.exists("user/材料.txt")
    assert 假.exists("data/产出.txt")
    assert 假.list_dir("user") == ("材料.txt",)
    assert 假.list_dir("data") == ("产出.txt",)
    # D-022/D-023：AI 可读 user/，但只能写 data/
    assert 假.read_text("user/材料.txt").is_ok
    assert not 假.write_atomic("user/x.txt", "x").is_ok


def test_假实现与真实实现在同一输入上答案一致(tmp_path: Path) -> None:
    """**这是 D-142 的判据：不是「假实现更严」，而是「两边一致」。**

    只测「假实现拒绝越权」是不够的——真正的风险是**分歧**。
    故这里直接对着真实 `FsStorage` 逐条比答案。
    """
    根 = tmp_path / "ws"
    _ = ensure_dirs(根)
    真 = FsStorage(Workspace(根))
    假 = fakes.FakeStorage()

    for 相对 in ("user/材料.txt", "data/产出.txt"):
        假.files[相对] = "内容"
        _ = (根 / 相对).write_text("内容", encoding="utf-8")

    for 路径 in ("../../etc/passwd", "/etc/passwd", "data/../../x.txt", ".."):
        assert 假.exists(路径) == 真.exists(路径), 路径
    for 路径 in ("../../etc", "..", "data", "user"):
        assert 假.list_dir(路径) == 真.list_dir(路径), 路径
