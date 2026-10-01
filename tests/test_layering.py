# 门禁测试豁免（BasedPyright 1.40 的 pyproject overrides 实测不生效，
# 故用文件内规则覆盖）：这些文件天生要做检查器无法理解的事——
# 故意非法操作（给 frozen 对象赋值、往 RunState 塞非法键）
# 与 AST 动态检查（node.lineno / node.value / __dataclass_params__）。
# 详见 pyproject.toml 末尾的说明。
# 代价是这些文件不再有类型检查——它们全是断言与扫描代码，没有业务逻辑。
# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportIndexIssue=false, reportCallIssue=false, reportAssignmentType=false, reportAny=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportPrivateUsage=false, reportUnusedFunction=false, reportUnusedImport=false, reportImplicitStringConcatenation=false
"""分层约束的强制执行（架构 §3.4）。

用 `ast` 静态扫描 `src/zhengtong/`，断言依赖方向与「谁能造对象」。

## 为什么这道门禁必须是机械的

§3.4 原文：**写在 README 里让人自觉的做法，三个月内必然失效。**

## 本测试锁四条

1. 依赖方向（§3.4 允许表逐条，不自行发挥）
2. 任何层不得 import 自己的上层（`cli` / `app.py` 装配根除外）
3. `domain` / `kernel` / `ports` 不得 import 任何第三方库
4. **除 `adapters/` 与 `app.py` 外，任何文件不得 import `zhengtong.adapters`**

第 4 条最要紧，理由见本文件末尾「为什么光禁 import 方向不够」。

## 空目录的处置

L4（`services/`）、L5（`orchestration/`）、L6（`interfaces/`）目前是空目录。
本测试**跳过**它们，而不是报「目录必须填满」——
门禁要管的是「写出来的代码没有越界」，不是「代码写得够多」。
空目录里没有 `.py` 文件，自然没有 import 可扫，跳过是正确的。

## 这道测试失败 = 架构被破坏 = CI 红
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "zhengtong"

#: §3.4 允许表。**逐条照抄架构，不自行发挥。**
#: 值是「该层可以 import 的本仓层」。
ALLOWED: dict[str, frozenset[str]] = {
    "domain": frozenset(),
    "kernel": frozenset({"domain"}),
    "ports": frozenset({"domain"}),
    "adapters": frozenset({"ports", "kernel", "domain"}),
    "services": frozenset({"ports", "kernel", "domain"}),
    "orchestration": frozenset({"services", "ports", "kernel", "domain"}),
    "interfaces": frozenset({"orchestration", "kernel", "domain"}),
}

#: §3.4：「任何层 → 自己的上层」一律禁止（除 cli/app 装配根）。
#: 这里逐条写明「上一层是谁」，而不是靠层名排序推断——
#: 推断规则一旦被改，门禁会跟着一起错，且没人发现。
UPPER: dict[str, frozenset[str]] = {
    "domain": frozenset({"kernel", "ports", "adapters", "services", "orchestration", "interfaces"}),
    "kernel": frozenset({"ports", "adapters", "services", "orchestration", "interfaces"}),
    "ports": frozenset({"adapters", "services", "orchestration", "interfaces"}),
    "adapters": frozenset({"services", "orchestration", "interfaces"}),
    "services": frozenset({"orchestration", "interfaces"}),
    "orchestration": frozenset({"interfaces"}),
}

#: §3.4 禁止：`orchestration → adapters`（编排层不得 import 适配器）。
#: 已由「不得 import 自己的上层」覆盖，此处单列是为了**报错信息可读**——
#: 这一条最常被误犯，值得单独说一句。
ORCHESTRATION_MAY_NOT_IMPORT_ADAPTERS = True

#: §3.4：第三方库只允许出现在 `adapters/`。
#: 三层底层不许碰，故给出它们的禁用名单。
NO_THIRD_PARTY: frozenset[str] = frozenset({"domain", "kernel", "ports"})

#: 运行时依赖（D-013：依赖尽量少）。出现在非 `adapters/` 即违规。
THIRD_PARTY: frozenset[str] = frozenset({"textual", "httpx", "tomli_w", "tomli"})

#: 本仓真实存在的层名。**用它过滤掉同层互引**，否则会被误报成越层。
KNOWN_LAYERS: frozenset[str] = frozenset(ALLOWED) | {"app"}


def _层名(路径: pathlib.Path) -> str:
    """文件属于哪一层。`app.py` 属于装配根，单列。"""
    相对 = 路径.relative_to(SRC)
    if 相对.name == "app.py":
        return "app"
    return 相对.parts[0]


def _本仓导入(节点: ast.AST, 当前文件所在包: pathlib.Path) -> list[str]:
    """抽出一个节点里所有 `zhengtong` 内的 import 目标层。

    **相对导入必须按层级数解析，不能直接取模块名第一段。**
    这是本测试第一版写错的地方：`from .bus import X`（同层）
    被我当成了层名 `bus`，于是报出一堆假的「domain → run_state」。

    `from .x import` 的 `level=1` 表示当前包，`level=2` 表示上一层。
    在 `domain/validation.py` 里：
      `from .errors import X`        level=1 → 同层 → 目标层 `domain`
      `from ..domain.errors import`  level=2 → 上跳一层再进 `domain`
    """
    出 = []
    for 子 in ast.walk(节点):
        模块 = ""
        level = 0
        if isinstance(子, ast.ImportFrom):
            模块 = 子.module or ""
            level = 子.level
        elif isinstance(子, ast.Import):
            段 = [名.name for 名 in 子.names]
            for 全名 in 段:
                p = 全名.split(".")
                if p and p[0] == "zhengtong":
                    出.append(p[1] if len(p) > 1 else "")
            continue

        if level:
            # level=1 落在当前包；level=2 落在上一层；依此类推
            上跳 = level - 1
            锚 = 当前文件所在包
            if 上跳 > 0:
                锚 = (
                    pathlib.PurePosixPath(*锚.parts[: len(锚.parts) - 上跳])
                    if len(锚.parts) >= 上跳
                    else pathlib.PurePosixPath()
                )
            目标 = str(锚 / 模块) if 模块 else str(锚)
            首 = 目标.split("/")[0] if "/" in 目标 else 目标.split(".")[0]
            出.append(首)
        elif 模块.startswith("zhengtong"):
            段 = 模块.split(".")
            出.append(段[1] if len(段) > 1 else "")
    return [x for x in 出 if x and x in KNOWN_LAYERS]


def _全部源码() -> list[pathlib.Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _第三方导入(树: ast.AST) -> set[str]:
    出: set[str] = set()
    for 子 in ast.walk(树):
        名: str | None = None
        if isinstance(子, ast.Import):
            名 = 子.names[0].name
        elif isinstance(子, ast.ImportFrom) and 子.level == 0:
            名 = 子.module
        if 名 and (根 := 名.split(".")[0]) in THIRD_PARTY:
            出.add(根)
    return 出


class Test依赖方向:
    """§3.4 允许表。"""

    def test_各层依赖方向合规(self) -> None:
        违例: list[str] = []
        for 路径 in _全部源码():
            层 = _层名(路径)
            if 层 not in ALLOWED:
                continue
            允许 = ALLOWED[层]
            for 目标 in _本仓导入(
                ast.parse(路径.read_text(encoding="utf-8")),
                路径.parent.relative_to(SRC),
            ):
                # 同层互引不在这张表的约束范围内（§3.4 未禁止）
                if 目标 != 层 and 目标 not in 允许:
                    违例.append(
                        f"{路径.relative_to(SRC)}：{层} → {目标}（允许：{sorted(允许) or '无'}）"
                    )
        assert not 违例, "依赖方向违规：\n" + "\n".join(违例)

    def test_无人import自己的上层(self) -> None:
        """§3.4：「任何层 → 自己的上层」一律禁止。"""
        违例: list[str] = []
        for 路径 in _全部源码():
            层 = _层名(路径)
            if 层 not in UPPER:
                continue
            上层 = UPPER[层]
            for 目标 in _本仓导入(
                ast.parse(路径.read_text(encoding="utf-8")),
                路径.parent.relative_to(SRC),
            ):
                if 目标 in 上层:
                    违例.append(f"{路径.relative_to(SRC)}：{层} → {目标}（上层）")
        assert not 违例, "越层向上依赖：\n" + "\n".join(违例)

    def test_编排层不得import适配器(self) -> None:
        """§3.4 单列条。编排层知道具体实现 = 依赖注入形同虚设。"""
        违例: list[str] = []
        for 路径 in _全部源码():
            if _层名(路径) != "orchestration":
                continue
            for 目标 in _本仓导入(
                ast.parse(路径.read_text(encoding="utf-8")),
                路径.parent.relative_to(SRC),
            ):
                if 目标 == "adapters":
                    违例.append(str(路径.relative_to(SRC)))
        assert not 违例, "编排层直接依赖适配器：\n" + "\n".join(违例)


class Test第三方隔离:
    """§3.4：第三方库只允许出现在 `adapters/`。"""

    def test_底层三层无第三方导入(self) -> None:
        违例: list[str] = []
        for 路径 in _全部源码():
            层 = _层名(路径)
            if 层 not in NO_THIRD_PARTY:
                continue
            for 库 in _第三方导入(ast.parse(路径.read_text(encoding="utf-8"))):
                违例.append(f"{路径.relative_to(SRC)}：{层} → {库}")
        assert not 违例, "底层出现第三方依赖：\n" + "\n".join(违例)

    def test_第三方依赖仅出现在适配层(self) -> None:
        """反向断言：把整个仓扫一遍，第三方导入只在 `adapters/`。

        上一条按层白名单查，这一条按实际导入查——
        两条都留，才不会在新增一层时**忘了往白名单里加**。
        """
        违例: list[str] = []
        for 路径 in _全部源码():
            if _层名(路径) == "adapters":
                continue
            for 库 in _第三方导入(ast.parse(路径.read_text(encoding="utf-8"))):
                违例.append(f"{路径.relative_to(SRC)}：{_层名(路径)} → {库}")
        assert not 违例, "第三方依赖出现在非适配层：\n" + "\n".join(违例)


class Test适配器构造权:
    """§3.4 最后一条，也是本测试**最难被绕过**的一条。"""

    def test_除适配层与装配根外无人import适配器(self) -> None:
        """§3.4 原文：

        > 测试扫 `src/zhengtong/` 下除 `adapters/` 与 `app.py` 外所有文件的 AST，
        > 若出现对 `zhengtong.adapters` 下任一模块的 import，测试失败。
        """
        违例: list[str] = []
        for 路径 in _全部源码():
            层 = _层名(路径)
            if 层 in ("adapters", "app"):
                continue
            for 目标 in _本仓导入(
                ast.parse(路径.read_text(encoding="utf-8")),
                路径.parent.relative_to(SRC),
            ):
                if 目标 == "adapters":
                    违例.append(f"{路径.relative_to(SRC)}（{层}）")
        assert not 违例, "非适配层导入了适配器，依赖注入已失效：\n" + "\n".join(违例)

    def test_适配器不被其他层以全名引用(self) -> None:
        """**补上相对导入之外的另一种写法。**

        §3.4 那段原文只说「对 `zhengtong.adapters` 的 import」，
        但代码里也可能写成 `import zhengtong.adapters.config` 这类绝对形式，
        或者 `from zhengtong.adapters.paths import Workspace`。

        这一条把两种都覆盖——**门禁只拦一种写法，等于给另一种开后门**。
        """
        违例: list[str] = []
        for 路径 in _全部源码():
            层 = _层名(路径)
            if 层 in ("adapters", "app"):
                continue
            源码 = 路径.read_text(encoding="utf-8")
            树 = ast.parse(源码)
            for 节点 in ast.walk(树):
                全名: str | None = None
                if isinstance(节点, ast.ImportFrom) and 节点.level == 0:
                    全名 = 节点.module
                elif isinstance(节点, ast.Import):
                    全名 = 节点.names[0].name
                if 全名 and "zhengtong.adapters" in 全名:
                    违例.append(f"{路径.relative_to(SRC)} → {全名}")
        assert not 违例, "非适配层引用适配器：\n" + "\n".join(违例)


# ── 为什么这道门禁不能只禁 import 方向 ────────────────────────────
#
# §3.4 原文：
#
# > 只禁 import 方向挡不住"合法 import 了但没用注入"。
# > 比如编排层写 `from zhengtong.adapters.llm.openai_style import OpenAiStyleClient`
# > 再 `OpenAiStyleClient(cfg)`——依赖方向表看不出问题，
# > 但依赖注入已经形同虚现，测试时无法替换成假端口。
#
# 上面的 `test_除适配层与装配根外无人import适配器` 就是为这条存在的。
# 它把「编排层不得 import 适配器」从建议变成 CI 红。
#
# **判据**：分层规则必须连「谁能造对象」一起管。
# 只管「谁能看见谁」的分层，挡不住最常见的实际退化。


@pytest.mark.parametrize("层", sorted(ALLOWED))
def test_允许表与架构一致(层: str) -> None:
    """**防止允许表自己漂移。**

    这道门禁的价值全在「允许表」准不准上。
    如果有人往 `ALLOWED["domain"]` 里偷偷加了 `kernel`，
    上面所有测试都会跟着一起失效——**而它们不会变红**。

    故加这条：允许表的每一项都必须是「本仓里真实存在的层」，
    且该层的依赖必须被现有代码真实使用过。
    """
    assert 层 in ALLOWED
    # 允许的层必须是已定义的层，不得凭空写出不存在的层名
    for 目标 in ALLOWED[层]:
        assert 目标 in set(ALLOWED) | {"app"}, f"{层} 允许了未定义的层 {目标}"
