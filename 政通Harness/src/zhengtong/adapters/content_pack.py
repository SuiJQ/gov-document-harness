"""真内容包适配器（第一轮增量检查补上）。

## 为什么现在才写

第 12 步交付了内容包（21 个文件、192 项检查全绿），
而 `adapters/` 里**从来没有过 `content_pack.py`**——
`PortRegistry.content_pack` 拿到的永远是 `FakeContentPack`。

于是真实运行路径上：
- 角色提示词是 `【桩】xxx 的角色提示词（假实现产出…）`
- 规则条目是 `【桩】参考 xxx`
- 而 `prompting` 装配器第 11 步辛苦按需装载的，**全是桩**

**这是 D-165 的第十一次**：内容包本该是「唯一不硬编码公文内容」的落点，
而它自己在真实路径上是假的。

## 与假实现的差别（关键）

`FakeContentPack` 返回桩文本，**谁都能从返回值看出它是假的**
（文本里带「假实现产出」字样）。
真适配器返回的是内容包正文——**它与假的长得一模一样**，
这正是假实现危险的地方：一眼看不出接的是真是假。

故本模块**同时提供 `版本()` 与 `是否真实` 标志**，
让「接的是不是真内容包」可以被判据直接查，
而不是靠读返回值猜。

## 不做热重载（D-044）

内容包在运行期只读。改了要重启。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.errors import ErrorCode, Result
from ..domain.role import RoleId


@dataclass(slots=True)
class 本地内容包:
    """从磁盘读的内容包。

    **`verify_manifest()` 必须先跑**：manifest 里登记了每个文件的 sha256，
    内容包被改动而 manifest 没同步时应当报错——
    否则「换了内容包」与「文件被误改」在运行期无法分辨。
    """

    根: Path
    #: 解析后的 manifest。**构造时读一次并缓存**——
    #: 内容包运行期只读（D-044 不做热重载），重复读盘没有意义。
    _清单: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self._清单:
            return
        got = self._读清单()
        if got.is_ok:
            对象 = got.unwrap()
            if isinstance(对象, dict):
                self._清单 = 对象

    # ── 内部 ──

    def 清单(self) -> dict[str, object]:
        return self._清单

    def _读清单(self) -> Result[dict[str, object]]:
        路径 = self.根 / "manifest.json"
        if not 路径.is_file():
            return Result.fail(ErrorCode.FILE_UNREADABLE, f"内容包缺 manifest：{路径}")
        try:
            数据 = json.loads(路径.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as 错:
            return Result.fail(ErrorCode.FILE_UNREADABLE, f"manifest 读不了：{错}")
        if not isinstance(数据, dict):
            return Result.fail(ErrorCode.FILE_UNREADABLE, "manifest 不是对象")
        return Result.ok(数据)

    def _读正文(self, 相对: str) -> Result[str]:
        """读一个内容文件。**路径不许越出内容包根**——
        manifest 里的相对路径虽是自家写的，但它是外部数据（D-030）。"""
        候选 = (self.根 / 相对).resolve()
        根 = self.根.resolve()
        if not str(候选).lower().startswith(str(根).lower()):
            return Result.fail(ErrorCode.PATH_ESCAPE, f"内容包路径越界：{相对}")
        if not 候选.is_file():
            return Result.fail(ErrorCode.FILE_UNREADABLE, f"内容文件不存在：{相对}")
        try:
            return Result.ok(候选.read_text(encoding="utf-8"))
        except OSError as 错:
            return Result.fail(ErrorCode.FILE_UNREADABLE, f"读不了 {相对}：{错}")

    # ── 端口实现 ──

    def load_role_prompt(self, role: RoleId) -> Result[str]:
        角色表 = self.清单().get("roles")
        if not isinstance(角色表, dict):
            return Result.fail(ErrorCode.FILE_UNREADABLE, "manifest 缺 roles")
        条目 = 角色表.get(role.value)
        if not isinstance(条目, dict):
            return Result.fail(ErrorCode.FILE_UNREADABLE, f"manifest 无角色 {role.value}")
        路径 = 条目.get("prompt")
        if not isinstance(路径, str):
            return Result.fail(ErrorCode.FILE_UNREADABLE, f"{role.value} 缺 prompt 路径")
        return self._读正文(路径)

    def load_reference(self, ref: str) -> Result[str]:
        """装载参考条目。

        **查 `references` 的值**——它既可能是 key（`references` 是
        `dict[str, str]`，值是描述），也可能是 `files` 里的路径。
        故先在 `files` 里找，找不到再当路径试——
        **两种都试**是因为 manifest 两种写法都合法，
        猜错一种会让规则条目静默装载不到。
        """
        # `files` 的键**就是相对路径**（`references/error-memory.md`），
        # 值是 `sha256:…`。第一版把值当路径读，故读不到——
        # 而我先写了「两种都试」的注释，实现却只试了一种。
        文件表 = self.清单().get("files")
        if isinstance(文件表, dict) and isinstance(文件表.get(ref), str):
            return self._读正文(ref)
        # 条目名与文件名不同时（如 `error-memory` → `error-memory.md`），
        # 用名字反查一次。
        for 键 in 文件表 if isinstance(文件表, dict) else ():
            if isinstance(键, str) and Path(键).stem == ref:
                return self._读正文(键)
        return self._读正文(f"references/{ref}.md")

    def verify_manifest(self) -> Result[None]:
        """启动时执行一次。**逐文件验 sha256**。

        为什么值得做：内容包是**软数据**，
        Ruff 与类型检查器都看不见它（D-055）。
        改了提示词却没同步 manifest 的哈希，
        运行期察觉不到——而「我改了内容包吗」正是排障时最常问的问题。

        值形如 `sha256:<hex>`。**哈希对不上直接报错**，不降级。
        """
        import hashlib

        文件表 = self.清单().get("files")
        if not isinstance(文件表, dict):
            return Result.fail(ErrorCode.FILE_UNREADABLE, "manifest 缺 files")
        缺: list[str] = []
        不符: list[str] = []
        for 相对, 值 in 文件表.items():
            路径 = self.根 / 相对
            if not 路径.is_file():
                缺.append(相对)
                continue
            期望 = str(值).removeprefix("sha256:") if isinstance(值, str) else ""
            if not 期望:
                continue
            实际 = hashlib.sha256(路径.read_bytes()).hexdigest()
            if 实际 != 期望:
                不符.append(相对)
        if 缺:
            return Result.fail(ErrorCode.FILE_UNREADABLE, f"内容包缺文件：{'、'.join(缺[:5])}")
        if 不符:
            return Result.fail(
                ErrorCode.FILE_UNREADABLE,
                f"内容包哈希不符（改了没同步 manifest）：{'、'.join(不符[:3])}",
            )
        return Result.ok_none()

    def version(self) -> str:
        版本 = self.清单().get("version")
        return 版本 if isinstance(版本, str) else "未知"

    @property
    def 是否真实(self) -> bool:
        """**给判据用**：这个是不是真内容包。

        存在的理由与假实现的 `版本()` 一样——
        真假内容包返回的正文长得一样，
        「接的是真是假」不能靠读返回值猜。
        """
        return True
