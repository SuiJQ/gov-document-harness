"""工具派发：三重检查 + 一条审计（架构 §7.4「权限模型」）。

```
每次调用三重检查 + 一条审计记录：

1. 角色白名单 —— 调 flow.ask_user 的不是主理人 → 拒绝。
2. 路径白名单 —— 交给 WorkspaceGuard（见下）。
3. 草稿纸上限 —— 每人每次任务最多 3 个文件，超出自动清最早的（D-042）。
4. 审计 —— 工具名、角色、时间、参数（脱敏）、结果摘要、裁决，全部进 journal。
```

## 顺序为什么是这个顺序

**未知工具 → 角色白名单 → 草稿纸 → 执行 → 审计。**

先查「这个工具存在吗」再查「你能不能用」：不存在的工具对任何角色都该被拒，
把两种拒绝混成一个码会让排障时分不清是打错名字还是越权。
（`TOOL_UNKNOWN` 与 `TOOL_NOT_ALLOWED` 分开就是这个理由。）

**拒绝也走审计**——越权尝试恰恰最该被看见。
D-165 的教训是「零件都测过、测试全绿，真跑起来那条路没接」；
一个静默拒绝的工具层会制造同样的盲区。
"""

from __future__ import annotations

from ...domain.errors import ErrorCode, Result
from ...domain.role import RoleId
from ...domain.tool import ToolDomain
from .audit import 审计器
from .handlers import 处理器表, 工具依赖, 记事件回调, 转达回调, 问用户回调
from .registry import TOOL_SPECS, 取工具


class 工具运行器:
    """派发工具调用。**三重检查在此，handler 只管干活。**"""

    def __init__(self, *, 依赖: 工具依赖, 审计: 审计器) -> None:
        self._d = 依赖
        self._审计 = 审计

    @property
    def 审计(self) -> 审计器:
        return self._审计

    def 接问用户(self, 问: 问用户回调 | None) -> None:
        """后补「问用户」回调（D-287）。

        ## 为什么和 `接记事件` 一样是「后补」

        同一个鸡生蛋：工具层先于编排器建好，
        而问用户要接到**界面侧的闸门**上——
        闸门由 `app.py` 持有，装配时一并递给两边。

        与 `接记事件` 分成两个方法而不是合成一个 `接回调`：
        **取值时机不同**——记事件在编排器建好后立刻定，
        问用户在界面起来之前可能还没有答复方，
        传 `None` 就表示「此刻无人应答」。
        """
        self._d.问用户 = 问

    def 接记事件(self, 记: 记事件回调) -> None:
        """后补事件回调（D-266）。

        ## 为什么要「后补」而不是构造时传

        工具层在装配时先建好，编排器（含编排回调）后建——
        **鸡生蛋**。故装配根在建好编排器后回来补这个字段。

        从 app 访问 `runner._依赖` 会破坏封装，故留这一公开口子。
        """
        self._d.记事件 = 记

    def 接转达(self, 转达: 转达回调) -> None:
        """后补插话转达（D-012）。

        与 `接记事件` 同一个鸡生蛋：工具层先建、编排器后建，
        而 `flow.inject_directive` 的回调要持有编排器的指令队列。

        **此前没有这个口子**，`工具依赖.转达` 恒为 `None`——
        模型调插话只得到「未接线」，而且**不报错**，
        用户的插话静默消失（与 D-286 问用户同型）。
        """
        self._d.转达 = 转达

    def _发事件(self, kind: str, **载荷: object) -> None:
        """发 `role.tool_call` / `tool.denied`（D-266）。

        ## 为什么不直接给审计器加「落盘」

        架构 §7.4 要求审计「全部进 journal」，而 `EventKind.ROLE_TOOL_CALL`
        与 `TOOL_DENIED` **早已定义、注释也写明了语义**——缺的只是发射点。
        另写一套落盘逻辑等于把事件机制复制一份。

        ## 为什么载荷用 `**kwargs` 而非 Event 对象

        工具层在 L4，编排层在 L5，**低层不该构造高层的值对象**。
        故只发最简的 `(kind, **载荷)`，由编排层组装成 `Event`。
        未接线时静默跳过——工具层不能因为没接投影就拒绝干活（D-192）。
        """
        记 = self._d.记事件
        if 记 is None:
            return
        记(kind, **载荷)

    def 调用(
        self,
        工具名: str,
        *,
        角色: RoleId,
        参数: dict[str, object],
        启用域: frozenset[ToolDomain] | None = None,
    ) -> Result[str]:
        spec = 取工具(工具名)
        if spec is None:
            return self._审计并返回(
                工具名=工具名,
                角色=角色,
                裁决=f"调用了不存在的工具「{工具名}」（含已砍除的 fs.*）",
                参数=参数,
                错误=ErrorCode.TOOL_UNKNOWN,
            )

        # 第一重：角色白名单。
        # 未知名字先于权限查——否则「打错名字」会被报成「越权」，排障时误导方向。
        if 角色 not in spec.allowed_roles:
            return self._审计并返回(
                工具名=工具名,
                角色=角色,
                裁决=f"{角色.value} 无权调用「{工具名}」（仅限 {sorted(r.value for r in spec.allowed_roles)}）",
                参数=参数,
                错误=ErrorCode.TOOL_NOT_ALLOWED,
            )

        # 第二重：**档位工具域**（D-389）。
        #
        # 档位过滤此前只作用于「发给 API 的工具声明」——
        # 那只是「不告诉模型有这个工具」。实测：咨询档声明里没有
        # `validate.run`，但主理人硬调它**照样执行成功**。
        #
        # 而模型发来不在清单里的工具是**常态而非异常**（幻觉、上下文污染、
        # 沿用上一轮的调用）。声明过滤管不住这条，只能在执行侧拦。
        #
        # `启用域=None` 表示「不按档位检查」：默认 None 让既有直接调用
        # （测试、审计器自检）行为不变，与 `参数` 一样是可选实参。
        #
        # **排在角色白名单之后**：档位错比角色错更「基础」，
        # 但一个既不在本档也不该由该角色调的工具，
        # 报「本档不适用」比报「无权」更贴近用户看到的现象。
        if 启用域 is not None and spec.域 not in 启用域:
            return self._审计并返回(
                工具名=工具名,
                角色=角色,
                裁决=(
                    f"本档位未启用「{spec.域.value}」类工具，不得调用「{工具名}」（{角色.value}）"
                ),
                参数=参数,
                错误=ErrorCode.TOOL_NOT_ALLOWED,
            )

        处理 = 处理器表.get(工具名)
        if 处理 is None:  # pragma: no cover - 由 test_每个工具都有实现 守住
            # 注册表加了工具却忘了写实现：这是编码错误，必须炸出来而不是静默跳过。
            raise RuntimeError(f"工具「{工具名}」在注册表里但没有实现")

        # 第二重（路径白名单）由 handler 内部的 StoragePort / UserFilesPort 执行（D-076），
        # 工具层不重复校验也不自己拼路径。

        # 第三重（草稿纸上限）在 draft.submit 内部——只有它需要。
        结果 = 处理(self._d, 角色, 参数)
        if 结果.is_error:
            错 = 结果.error
            assert 错 is not None  # is_error 为真时必有 error（见 Result 定义）
            return self._审计并返回(
                工具名=工具名,
                角色=角色,
                裁决=f"调用「{工具名}」失败：{错.code}",
                参数=参数,
                错误=错.code,
                原文=错.detail,
            )
        self._审计.记(
            工具名=工具名,
            角色=角色,
            允许=True,
            裁决=f"允许 {角色.value} 调用「{工具名}」",
            参数摘要=_摘要(参数),
            结果摘要=结果.unwrap(),
        )
        self._发事件(
            "role.tool_call",
            工具名=工具名,
            角色=角色.value,
            裁决=f"允许 {角色.value} 调用「{工具名}」",
            参数摘要=_摘要(参数),
            结果摘要=结果.unwrap(),
        )
        return 结果

    def _审计并返回(
        self,
        *,
        工具名: str,
        角色: RoleId,
        裁决: str,
        参数: dict[str, object],
        错误: ErrorCode,
        原文: str = "",
    ) -> Result[str]:
        """拒绝与失败都要留痕。**静默失败等于把问题藏起来。**"""
        self._发事件(
            "tool.denied",
            工具名=工具名,
            角色=角色.value,
            裁决=裁决,
            参数摘要=_摘要(参数),
            错误码=错误.value,
        )
        self._审计.记(
            工具名=工具名,
            角色=角色,
            允许=False,
            裁决=裁决,
            参数摘要=_摘要(参数),
            结果摘要=原文,
            错误码=str(错误),
        )
        return Result.fail(错误, 裁决)


def _摘要(参数: dict[str, object]) -> str:
    """参数摘要。**不进敏感值**——只记键名与长度，不记内容。

    审计记录最终进 journal（D-035）。记内容就等于把材料正文抄进日志，
    而日志的保留与脱敏策略都和用户材料不是一回事（D-161：文档不得嵌凭据片段）。
    """
    return "，".join(f"{k}={len(str(v))}字符" for k, v in sorted(参数.items()))


__all__ = ["TOOL_SPECS", "工具运行器"]
