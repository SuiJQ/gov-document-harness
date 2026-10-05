"""工具的用途域（L0 领域概念）。

## 为什么这个枚举在 domain 而不是 services/tools

档位声明「本档启用哪些工具域」，而 `TierSpec` 在 `domain/tier.py`。
若 `ToolDomain` 住在 `services/tools/spec.py`，`domain` 就得 import `services`
——**依赖方向倒置**，会被 `tests/test_layering.py` 的分层判据直接拦下。

反过来 `services/tools/spec.py` import 它是标准的向下依赖，故选这一侧。

## 它和 `Effect` 的分工

`Effect`（`services/tools/spec.py`）说的是**副作用性质**：
只读 / 产出 / 挂起 / 提问 / 退回 / 纯校验。审计强度看它。

`ToolDomain` 说的是**用途类别**：能不能写稿、能不能跑门禁、能不能提问。
档位差异看它。

**两者正交，不可互相推导**：

- `shared.write`（写公共区）与 `draft.submit`（提交正文）
  **同为 `PUBLISH`**，但咨询档要前者、不要后者；
- `kb.search` 是 `READ`，`validate.run` 是 `PURE`，
  两者都不改外部状态，却分属「知识检索」与「门禁校验」。

拿 `Effect` 顶替 `ToolDomain` 必然过粗：要么为了留 `shared.write`
把 `draft.submit` 一起留给咨询档，要么两者一起砍掉——
而后一种会砍掉咨询档唯一能交东西的通道。
"""

from __future__ import annotations

from enum import StrEnum


class ToolDomain(StrEnum):
    """工具的**用途域**——「这个工具是干什么的那一类事」。

    ## 为什么按域过滤而不是给每档列一份工具清单

    列清单 = 17 个工具名 × 3 档 = 51 项声明。
    新增一个工具要记得改三处，漏一处就是「工具在某档凭空消失」
    或「在某档越权暴露」，而这种漂移**没有任何判据会报**（D-384）。
    按域声明则只需给新工具选一个域，档位侧不用动。
    """

    #: 读内容：知识包、用户文件、检索。
    知识检索 = "知识检索"
    #: 产出正文（提交成稿）。
    产出交付 = "产出交付"
    #: 公共交换区的读写（咨询档也要用——意见一样要落盘）。
    公共交换 = "公共交换"
    #: 成员与主理人之间的消息。
    协作通信 = "协作通信"
    #: 门禁与真实校验器。
    门禁校验 = "门禁校验"
    #: 向用户提问。
    方向提问 = "方向提问"
    #: 休眠、唤醒、状态、指令注入、退回、结果回传。
    运行控制 = "运行控制"

    @classmethod
    def 全部(cls) -> frozenset[ToolDomain]:
        """全部域。**给「什么域都要」的档位用**，省得逐个抄一遍。"""
        return frozenset(cls)


__all__ = ["ToolDomain"]
