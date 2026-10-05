"""真实文件系统 StoragePort 实现。

**所有路径都先过 `guard.ensure_within`**（D-076）。
本模块不做任何"这个路径看起来没问题"的自行判断——
判断只有一处，就是守卫。散落两处就等于没有守卫。

## 写哪儿、读哪儿

| 目录 | AI 读 | AI 写 | 说明 |
|---|---|---|---|
| `data/` | ✅ | ✅ | AI 产出 |
| `user/` | ✅ | ❌ | 用户文件，只读（D-022） |
| `system/` | ❌ | ❌ | 配置与日志，AI 看不到也改不了 |

越权写入 `user/` 不是"报错"而是**直接拒绝**：D-022 说程序只读 `user/`，
不得自动移动、改写或归档用户文件。
"""

# 本文件放宽 Unknown 类规则，理由同 domain/errors.py 的 D-085：
# `Result.ok(...)` 返回 `Result[Any]`（三方案实测后确认唯一可行解）。
# 只影响静态提示，不影响运行行为。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

from ..domain.artifact import ArtifactRef
from ..domain.errors import AppError, ErrorCode, Result
from ..domain.ids import RunId
from ..domain.run_state import RunState
from . import paths, state_codec
from .atomic import read_text, write_atomic
from .guard import ensure_within
from .paths import Workspace

#: 泛型参数。`Result` 是不变的，跨类型转失败结果必须有它
_T = TypeVar("_T")

#: 草稿纸上限（D-042/D-029）：一个角色最多 3 个在制品
DRAFT_LIMIT = 3


@dataclass(slots=True)
class FsStorage:
    """基于真实文件系统的存储。"""

    ws: Workspace
    encoding: str = "utf-8"
    #: **产物**专用编码（D-235）。运行数据恒为 `encoding`（UTF-8）。
    #:
    #: 用户约束原文：「TXT 默认 UTF-8；配置 `gb18030: true` 时产出改用
    #: GB18030，**但日志和界面始终使用 UTF-8**」。故这两个编码必须分开——
    #: 把配置直接喂 `encoding` 会把 `state.json`（断点续跑要读）也写成
    #: GB18030，UTF-8 工具与控制台读它会乱码。
    产物编码: str = "utf-8"

    # ── 路径解析 ──

    def _resolve_for_read(self, rel_path: str) -> Result[Path]:
        """读：允许 `data/` 与 `user/`。"""
        return self._resolve_any(rel_path)

    def _resolve_for_write(self, rel_path: str) -> Result[Path]:
        """写：**只允许 `data/`**。写 `user/` 视为越权（D-022）。

        ## 审计 D-126：必须在**解析后**判权限，不能看原始字符串

        原实现先拿 `rel_path` 切出顶层目录名（`"data/t/x.txt"` → `"data"`）
        判断在不在 `AI_WRITABLE` 里，再交给 `_resolve_any` 做守卫。

        **这个顺序可以被软链接绕过**：在 `data/` 里放一个指向 `user/`
        的软链接 `data/t`，那么 `data/t/偷写.txt` 的字符串顶层是 `data`
        （放行），而它的**真实落点是 `user/`**——
        实测 AI 成功把文件写进了 `user/`，直接违反 D-022
        （「AI 只能写入 `data/`」）与「程序不得改写用户文件」。

        故改为：**先过守卫拿到解析后的绝对路径，再据其真实落点判权限**。
        守卫本身（`ensure_within`）保证不会逃出工作区根，
        这一层再保证落点在允许的顶层目录内。
        """
        got = self._resolve_any(rel_path)
        if got.is_error:
            return got
        顶层 = _顶层目录(got.unwrap(), self.ws)
        if 顶层 and 顶层 not in paths.AI_WRITABLE:
            return Result.fail(
                ErrorCode.PATH_ESCAPE,
                f"AI 只能写入 {paths.AI_WRITABLE}，实际落点在：{顶层}",
            )
        return got

    def _resolve_any(self, rel_path: str) -> Result[Path]:
        """拼到工作区根下并过守卫。

        **先拼根再守卫**，不是先守卫再拼：守卫要比的是"最终路径是否在根下"，
        单独守卫一个相对路径等于没守卫（`../` 照样能过）。
        """
        got = ensure_within(self.ws.root, self.ws.root / rel_path)
        if got.is_error:
            return got
        target = got.unwrap()
        顶层 = _顶层目录(target, self.ws)
        if 顶层 and 顶层 not in paths.AI_READABLE:
            return Result.fail(ErrorCode.PATH_ESCAPE, f"AI 不可访问该目录：{顶层}")
        return Result.ok(target)

    # ── StoragePort ──

    def write_atomic(self, rel_path: str, content: str) -> Result[None]:
        target = self._resolve_for_write(rel_path)
        if target.is_error:
            return _reraise_as(target, None)
        return write_atomic(target.unwrap(), content, encoding=self.encoding)

    def read_text(self, rel_path: str) -> Result[str]:
        target = self._resolve_for_read(rel_path)
        if target.is_error:
            return _reraise_as(target, str)
        return read_text(target.unwrap(), encoding=self.encoding)

    def exists(self, rel_path: str) -> bool:
        got = self._resolve_for_read(rel_path)
        return got.is_ok and got.unwrap().exists()

    def list_dir(self, rel_path: str) -> tuple[str, ...]:
        """列目录。**不递归、不返回绝对路径**（端口契约）。

        越权或不存在都返回空元组而非失败：
        列目录常用于"有什么可选的"，空列表是合法答案，
        让调用方处理失败反而更麻烦。
        """
        got = self._resolve_for_read(rel_path)
        if got.is_error:
            return ()
        d = got.unwrap()
        if not d.is_dir():
            return ()
        try:
            return tuple(sorted(p.name for p in d.iterdir()))
        except OSError:
            return ()

    def save_run_state(self, state: RunState) -> Result[None]:
        """状态落盘。**每次都写全量**——
        增量更新意味着一次崩溃就要写"补丁的补丁"，得不偿失。"""
        # 编码在 domain/state_codec：枚举键的 JSON 化是领域知识，
        # 放进存储层会让两层都懂一半
        return self._写产物(f"{paths.DATA}/{state.run_id}/state.json", state_codec.dumps(state))

    def load_run_state(self, run_id: RunId) -> Result[RunState]:
        text = self.read_text(f"{paths.DATA}/{run_id}/state.json")
        if text.is_error:
            return _reraise_as(text, RunState)
        return state_codec.loads(text.unwrap())

    def put_artifact(self, ref: ArtifactRef, content: str) -> Result[None]:
        return self._写产物(f"{paths.DATA}/{ref.path}", content, 编码=self.产物编码)

    def _写产物(self, rel_path: str, content: str, *, 编码: str | None = None) -> Result[None]:
        """写产物：**先建父目录，再原子写**。

        ## 审计 D-127：产物目录必须由程序自己创建

        `atomic.write_atomic` 有意**不**隐式建目录
        （理由正当：隐式建目录会让一次打错的路径凭空生成一串空目录）。
        但它把建目录的职责推给了"调用方"，而调用链上**没有一处真的建**：
        `ensure_dirs()` 只有定义、**全仓无调用方**，且它只管
        `data/` `user/` `system/` 三个顶层目录，不管 `data/<run-id>/`。

        实测后果：`save_run_state` 在全新运行时**必然失败**——
        `data/r-001/` 从未创建，原子写报
        `No such file or directory: .../data/r-001/.state.json.tmp`。
        这是状态机持久化的唯一入口，失败即断点续跑无从谈起。

        架构第 843 行明文：「新建文件必然由程序自己在**已校验的**父目录下创建」。
        故在此处显式建，且建的是**已过守卫、已确认落在 `data/` 内**的目录——
        不是让 `write_atomic` 拿一个未校验的路径去建。

        `parents=True`：`data/<run-id>/附件/` 这类多层结构同样要能直接写。
        `exist_ok=True`：重复保存状态不报错（每次都写全量）。
        """
        target = self._resolve_for_write(rel_path)
        if target.is_error:
            return _reraise_as(target, None)
        try:
            target.unwrap().parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return Result.fail(
                ErrorCode.STORAGE_WRITE_FAILED,
                f"无法创建产物目录 {target.unwrap().parent}：{exc}",
            )
        return write_atomic(target.unwrap(), content, encoding=编码 or self.encoding)

    def draft_paper_ok(self, role: str, current_count: int) -> bool:
        """草稿纸是否还能再放一个（D-042 上限 3）。

        `role` **暂不参与判定**：上限是「每个角色各 3 个」，
        由调用方按角色分别计数后传进来。
        这里不按 role 记账，是因为 StoragePort 没有"查某角色已放几个"
        这个方法——真要做就得给端口加接口，那是 L5 编排层的决定，
        不该由存储层替它定。参数保留是为了签名兼容 Protocol。
        """
        _ = role
        return current_count < DRAFT_LIMIT


class _HasError(Protocol):
    """只要求"能拿到 error"。**不用 `Result[Any]`**：
    `Any` 会污染整个返回类型推断，且触发 `reportExplicitAny`。
    声明成 Protocol 后，`Result[Path]`、`Result[str]` 都能满足它，
    而成功值的类型始终与我无关。"""

    @property
    def error(self) -> AppError | None: ...


def _reraise_as(got: _HasError, _t: type[_T] | None = None) -> Result[_T]:
    """把失败结果转成任意目标类型的失败结果。**只搬运不重造**：
    code 与 detail 原样带过，不会丢掉其中之一。

    `Result` 是 `Generic[T]` 且**不变**——`Result[Path]` 既不能当
    `Result[str]` 用，也不能当 `Result[object]` 用。故入参声明为
    只要求 `.error` 的 Protocol：这个函数**只读 error**，
    成功值是什么类型与它无关。

    写成泛型函数而不是为四个方法各抄一份：抄四遍的话，
    "改了三个忘了第四个"是迟早的事，而那种错误只在运行时显形。

    用 `TypeVar` 而非 PEP 695 的 `class _[T]` 语法：后者在 3.11 上是
    语法错误（D-086）。`_t` 参数只为把类型参数传给调用点，运行时不用。
    """
    err = got.error
    assert err is not None
    return Result.fail(err.code, err.detail)


def _顶层目录(target: Path, ws: Workspace) -> str:
    """`target`（**已解析**的绝对路径）在工作区里的顶层目录名。

    ## 为什么必须传已解析的路径

    权限判断的全部意义在于"文件最终落在哪儿"，
    而软链接会让"写的路径"与"落点"分道扬镳。
    故入参必须是 `ensure_within` 处理过的 `realpath + normcase` 结果。

    顶层目录名同样取自解析后的路径——否则 `data/t/x` 里
    那个 `t` 是软链接时，取到的就不是真实落点的顶层了。

    返回空串表示"就是工作区根本身"，调用方据此放行
    （根目录不是某个顶层目录，不该按目录权限拦）。
    """
    base = got_base(ws)
    if target == base:
        return ""
    相对 = target.relative_to(base)
    return 相对.parts[0] if 相对.parts else ""


def got_base(ws: Workspace) -> Path:
    """工作区根的解析后路径。**必须与守卫用同一套归一**，
    否则 `relative_to` 会因大小写或软链差异而误判。"""
    from .guard import resolve

    return resolve(ws.root)
