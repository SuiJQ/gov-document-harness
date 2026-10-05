"""门禁报告必须承载取证信息（D-236）。

## 复盘时才发现的盲区

第 14 步的判据**已经**覆盖了退回、上限、缺席、幂等
（`test_step14_orchestrate_gate.py` 十条）。所以我复盘时说
「只验有调用、不验内容」——**这个判断是错的**，收回。

真正的盲区在**报告本身**。它只有一行：

```python
def _门禁报告(门禁: list[GateId]) -> str:
    return "\\n".join(["【门禁检查】", *(f"- {g.value} {GATES[g].name}" for g in 门禁)])
```

门禁号 + 名字，**仅此而已**。于是三个字段生产零消费：

| 字段 | 谁该看它 | 现状 |
|---|---|---|
| `GateRecord.evidence` | 证据内容 | 只在序列化时读写，**没人看** |
| `rejection_reason` | 为什么不通过 | 驳回了，但报告上看不出原因 |
| `gate_owners(gate)` | 谁取的证（D-049 责任映射） | 生产零引用 |

## 为什么这不是小事

架构 §8.3 要求「这稿是谁的锅？」——门禁 `owner` 是责任映射的唯一依据。
而用户明确要求**双轴台账不得丢失**。

标准模式不展示审核意见（用户约束），所以这份报告是事后唯一能查的东西。
它现在只说「G2 xx检查」——过没过、为什么、谁查的，全都没有。

出问题时用户只能回去翻日志原文，而那正是用户明确说
「不要占太大空间」「不要展示模型间交互」的东西。

## 判据只从外部观察

不查源码里有没有 `owner=`——只读报告正文，看该有的信息在不在。
"""

from __future__ import annotations

from threading import Lock

from zhengtong.domain.gate import FORMAL_GATES, GATES, GateId, gate_owners
from zhengtong.domain.run_state import GateRecord
from zhengtong.domain.tier import TierId
from zhengtong.orchestration import 编排器
from zhengtong.orchestration.gates_forging import 门禁取证请求
from zhengtong.orchestration.orchestrate import 阶段产物


def _编排(
    *,
    驳回: dict[str, int] | None = None,
    证据: dict[str, tuple[str, ...]] | None = None,
    理由: dict[str, str] | None = None,
    缺席: bool = False,
) -> 编排器:
    """装一个编排器，门禁取证按门禁号计次给结论。

    按**门禁号**计次而非调用次序：并行化后每组被调一次且并发，
    靠 `next(队列)` 会让某组的裁定被别的组领走。
    """
    次数: dict[str, int] = {}
    锁 = Lock()

    def 门禁取证(请求: 门禁取证请求) -> dict[str, GateRecord]:
        门禁 = 请求.门禁
        出: dict[str, GateRecord] = {}
        if 缺席:
            # 一条结论都不给（D-221：没人取证 ≠ 取过了都没问题）
            return 出
        with 锁:
            for g in 门禁:
                n = 次数.get(g.value, 0)
                次数[g.value] = n + 1
                限 = (驳回 or {}).get(g.value, 0)
                出[g.value] = GateRecord(
                    gate=g.value,
                    passed=n >= 限,
                    evidence=(证据 or {}).get(g.value, ("未提供证据",)),
                    rejection_reason="" if n >= 限 else (理由 or {}).get(g.value, "编造"),
                )
        return 出

    return 编排器(llm=FakeLlm(), storage=FakeStorage(), 门禁取证=门禁取证)


def _报告(编: 编排器) -> str:
    结果 = 编.跑到终(档位=TierId.PRO, 需求="通知")
    assert not 结果.is_error, f"跑失败：{结果.error}"
    return _从产物(结果.unwrap().产物)


def _从产物(产物: tuple[阶段产物, ...]) -> str:
    找到 = [p for p in 产物 if getattr(p, "名称", "") == "gate-report.md"]
    assert 找到, f"没有 gate-report.md；现有产物：{[getattr(p, '名称', '') for p in 产物]}"
    return "".join(p.内容 for p in 找到)


class Test报告承载取证信息:
    def test_报告含责任角色(self) -> None:
        """**核心判据**：每道门禁必须写明**谁取的证**。

        架构 §8.3「这稿是谁的锅？」问的就是这个；
        `gate_owners` 是责任映射的唯一依据，此前生产零引用。
        """
        from zhengtong.domain.role import display_name

        报告 = _报告(_编排())
        缺 = [
            g.value
            for g in GateId
            if g in FORMAL_GATES
            # G9 的取证方运行期才定（D-084：按所触发的红线归属退回），
            # 它印「（运行期确定）」是**正确**的——判据得认这个语义，
            # 不能要求它写出一个静态角色名。
            and not GATES[g].dynamic_owners
            and not any(display_name(r) in 报告 for r in gate_owners(g))
        ]
        assert not 缺, f"这些门禁没写责任角色：{缺}"
        # 反向：角色必须紧挨着自己的门禁，不是全堆在末尾
        for g in FORMAL_GATES:
            行 = next((ln for ln in 报告.splitlines() if ln.startswith(f"- {g.value} ")), "")
            for r in gate_owners(g):
                if not GATES[g].dynamic_owners:
                    assert display_name(r) in 行, f"{g.value} 这一行没写取证方 {display_name(r)}"

    def test_报告含取证证据(self) -> None:
        """**核心判据**：`evidence` 不能只存不读。

        它现在只在序列化时读写——取证方给的证据**没人看**。
        """
        证据 = {g.value: (f"证据-{g.value}-甲",) for g in FORMAL_GATES}
        报告 = _报告(_编排(证据=证据))
        # G9 取证方运行期才定，正常轮次下无裁定、无证据——
        # 报告印「无结论」是**正确**的，不算缺证据。
        缺 = [
            v[0]
            for k, v in 证据.items()
            if not GATES[GateId(k)].dynamic_owners and v[0] not in 报告
        ]
        assert not 缺, f"这些门禁的证据没进报告：{缺}"

    def test_报告含通过与否的结论(self) -> None:
        """报告要能一眼看出哪道过了、哪道没过。

        现在只印门禁号和名字——过没过完全看不出来。
        """
        报告 = _报告(_编排())
        assert any(k in 报告 for k in ("通过", "未通过", "✓", "✗")), (
            f"报告看不出通过与否：{报告[:200]}"
        )

    def test_驳回时报告写明原因(self) -> None:
        """**本条最要紧**：驳回了，用户得知道**为什么**。

        理由文本由取证方给出，报告里必须能看到——
        否则用户只知道「被退回了」，无法判断是文稿问题还是环境问题。
        """
        编 = _编排(驳回={"G2": 9}, 理由={"G2": "虚构了未给出的日期"})
        结果 = 编.跑到终(档位=TierId.PRO, 需求="通知")
        # 驳回 9 次必然触发 D-050 上限 → 转用户裁决 → 失败。
        # **这条路径上用户唯一能拿到的就是门禁报告**，
        # 所以报告必须留下。
        assert 结果.is_error, "驳回 9 次还不失败——上限没生效"
        报告 = _从产物(_留产物(编))
        assert "虚构了未给出的日期" in 报告, f"驳回原因没进报告，用户无从判断问题在哪：{报告[:300]}"

    def test_角色显示为中文名(self) -> None:
        """**用户约束**：界面显示中文角色名，内部标识才用英文。

        报告是给人看的，不能印 `zheng_chen`。
        """
        from zhengtong.domain.role import ROLES

        报告 = _报告(_编排())
        印出英文 = [r.id.value for r in ROLES if r.id.value in 报告]
        assert not 印出英文, f"报告里漏出了英文角色标识：{印出英文}"


from zhengtong.adapters.fakes import FakeLlm, FakeStorage  # noqa: E402


def _留产物(编: 编排器) -> tuple[阶段产物, ...]:
    """跑失败时，从编排器已记下的产物里取。

    **驳回转用户裁决这条路本来就该产出报告**——用户拿到的是
    「为什么被卡住」，而不是一个错误码了事。
    """
    from zhengtong.domain.phase import PhaseId

    过程 = 编._过程  # pyright: ignore[reportPrivateUsage]
    assert 过程 is not None, "推进过程没了"
    过 = tuple(p for p in 过程.产物 if p.阶段 is PhaseId.GATING)
    assert 过, "GATING 段没留下任何产物——失败路径上用户什么都拿不到"
    return 过


class Test无结论路径也留报告:
    def test_缺席时也留报告并写明哪几道缺(self) -> None:
        """**这条覆盖的是另一条失败路径**（D-236 第三处补漏）。

        取证方一条结论都不给时，原先直接 `return Result.fail`，
        产物列表空着——用户拿到一个错误码，
        不知道**哪几道门禁**没查到结论。

        而这恰恰是最需要报告的情形：无结论通常是
        「无库/无网」，属环境问题，用户得知道是哪几道查不了。
        """
        编 = _编排(缺席=True)
        结果 = 编.跑到终(档位=TierId.PRO, 需求="通知")
        assert 结果.is_error, "一条结论都不给还判通过——D-221 失效了"
        报告 = _从产物(_留产物(编))
        assert "无结论" in 报告, f"报告没标出缺结论的门禁：{报告[:200]}"
