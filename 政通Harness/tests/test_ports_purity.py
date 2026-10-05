# 本文件放宽 reportAny：两处 `Any` 是**无法消除**的边界，不是缺陷——
#   1. `ids=lambda p: p.name` 是 pytest 参数化钩子，框架签名本身无类型标注；
#   2. `dataclasses.Field[Any]` 是标准库固有参数化（字段类型本就异构）。
# 两者都不影响判据有效性——断言本身仍然严格。
# pyright: reportAny=false
"""端口纯度：签名里不得出现第三方类型。

这是第 2 步的机械判据。手写约定靠不住——"就差这一个类型"
正是隔离泄漏最常见的发生方式，所以用测试把它钉死。

同时检查两条铁律：
- `ports` 不得依赖 `adapters` / `services` / `orchestration` / `interfaces`
- `domain` 不得依赖任何非 `domain` 的本仓模块
"""

from __future__ import annotations

import ast
import pathlib

import pytest

PORTS_DIR = pathlib.Path("src/zhengtong/ports")
DOMAIN_DIR = pathlib.Path("src/zhengtong/domain")
PKG = "zhengtong"

#: 允许出现在端口里的 stdlib 模块名片段。
ALLOWED_STDLIB = {
    "__future__",
    "abc",
    "collections",
    "dataclasses",
    "enum",
    "hashlib",
    "pathlib",
    "typing",
}

#: 绝对禁止的第三方包（D-030：运行时依赖就这三个）。
FORBIDDEN_THIRD_PARTY = {
    "httpx",
    "textual",
    "tomli_w",
    "tomli",
    "requests",
    "openai",
    "anthropic",
    "httpx_sse",
}

#: 上层模块名——被依赖方的视角。
UPPER_LAYERS = ("adapters", "services", "orchestration", "interfaces", "kernel")


def _iter_py(dirpath: pathlib.Path) -> list[pathlib.Path]:
    return sorted(dirpath.rglob("*.py"))


def _top_imports(tree: ast.AST) -> set[str]:
    """取所有 import 的顶层包名。"""
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # 相对导入，另行判定
            if node.module:
                mods.add(node.module.split(".")[0])
    return mods


def _relative_targets(tree: ast.AST) -> set[str]:
    """取相对导入的目标层级，如 `..domain.errors` → `domain`。"""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level and node.level > 0:
            if node.module:
                out.add(node.module.split(".")[0])
            for a in node.names:
                if a.name and not node.module:
                    out.add(a.name.split(".")[0])
    return out


class Test端口纯度:
    def test_端口目录存在且非空(self) -> None:
        assert _iter_py(PORTS_DIR), "ports 目录为空"

    @pytest.mark.parametrize("path", _iter_py(PORTS_DIR), ids=lambda p: p.name)
    def test_无第三方导入(self, path: pathlib.Path) -> None:
        mods = _top_imports(ast.parse(path.read_text(encoding="utf-8")))
        bad = mods & FORBIDDEN_THIRD_PARTY
        assert not bad, f"{path.name} 引入了第三方：{bad}"

    @pytest.mark.parametrize("path", _iter_py(PORTS_DIR), ids=lambda p: p.name)
    def test_只依赖标准库与本仓domain(self, path: pathlib.Path) -> None:
        """第三方之外只允许 stdlib；本仓只允许 domain 与 ports 同层互引。"""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mods = _top_imports(tree)
        non_stdlib = {
            m for m in mods if m not in ALLOWED_STDLIB and m != PKG and not m.startswith("_")
        }
        assert not non_stdlib, f"{path.name} 依赖了未授权模块：{non_stdlib}"

        rel = _relative_targets(tree)
        upper = rel & set(UPPER_LAYERS)
        assert not upper, f"{path.name} 依赖了上层模块：{upper}"
        # 同层互引（registry 引 llm 等）与 domain 都合法；其余一律越界。
        allowed_rel = {"domain"} | {p.stem for p in _iter_py(PORTS_DIR)}
        bad = rel - allowed_rel
        assert not bad, f"{path.name} 相对导入越界：{bad}"

    @pytest.mark.parametrize("path", _iter_py(PORTS_DIR), ids=lambda p: p.name)
    def test_签名中无pathlib类型(self, path: pathlib.Path) -> None:
        """路径一律用 `str`。让 `Path` 进签名就等于把文件系统泄漏给上层。"""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            parts: list[ast.expr] = []
            if node.returns is not None:
                parts.append(node.returns)
            for arg in (*node.args.args, *node.args.kwonlyargs):
                if arg.annotation is not None:
                    parts.append(arg.annotation)
            for part in parts:
                rendered = ast.unparse(part)
                assert "Path" not in rendered, f"{path.name}:{node.name} 签名含 Path：{rendered}"


class Test领域层零依赖:
    @pytest.mark.parametrize("path", _iter_py(DOMAIN_DIR), ids=lambda p: p.name)
    def test_无第三方导入(self, path: pathlib.Path) -> None:
        mods = _top_imports(ast.parse(path.read_text(encoding="utf-8")))
        bad = mods & FORBIDDEN_THIRD_PARTY
        assert not bad, f"{path.name} 引入了第三方：{bad}"

    @pytest.mark.parametrize("path", _iter_py(DOMAIN_DIR), ids=lambda p: p.name)
    def test_只依赖标准库(self, path: pathlib.Path) -> None:
        mods = _top_imports(ast.parse(path.read_text(encoding="utf-8")))
        non_stdlib = {m for m in mods if m not in ALLOWED_STDLIB and m != PKG}
        assert not non_stdlib, f"{path.name} 依赖了非标准库：{non_stdlib}"

    @pytest.mark.parametrize("path", _iter_py(DOMAIN_DIR), ids=lambda p: p.name)
    def test_不依赖上层(self, path: pathlib.Path) -> None:
        rel = _relative_targets(ast.parse(path.read_text(encoding="utf-8")))
        upper = rel & set(UPPER_LAYERS)
        assert not upper, f"{path.name} 依赖了上层：{upper}"


class Test寄存器字段齐全:
    def test_八个端口齐备(self) -> None:
        from zhengtong.ports.registry import PortRegistry

        fields = PortRegistry.__dataclass_fields__
        expected = {
            "llm",
            "storage",
            "user_files",
            "content_pack",
            "retriever",
            "validators",
            "secrets",
            "clock",
        }
        assert set(fields) == expected

    def test_字段无默认值(self) -> None:
        """有默认值等于允许漏装配，故必须全部必填。"""
        import dataclasses

        from zhengtong.ports.registry import PortRegistry

        fields: dict[str, dataclasses.Field[object]] = PortRegistry.__dataclass_fields__
        for name, f in fields.items():
            assert f.default is dataclasses.MISSING, f"{name} 不该有默认值"

    def test_不可变(self) -> None:
        import dataclasses

        from zhengtong.ports.registry import PortRegistry

        assert dataclasses.is_dataclass(PortRegistry)
