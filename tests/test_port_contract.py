# 门禁测试豁免（BasedPyright 1.40 的 pyproject overrides 实测不生效，
# 故用文件内规则覆盖）：这些文件天生要做检查器无法理解的事——
# 故意非法操作（给 frozen 对象赋值、往 RunState 塞非法键）
# 与 AST 动态检查（node.lineno / node.value / __dataclass_params__）。
# 详见 pyproject.toml 末尾的说明。
# 代价是这些文件不再有类型检查——它们全是断言与扫描代码，没有业务逻辑。
# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportIndexIssue=false, reportCallIssue=false, reportAssignmentType=false, reportAny=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportPrivateUsage=false, reportUnusedFunction=false, reportUnusedImport=false, reportImplicitStringConcatenation=false
"""端口契约金样（架构 §12 机制 5）。

## 判据出处

§12 机制 5 原文：

> 端口（`ports/`）的方法签名变更 = 需要重构的实现适配器。
> 本项目不追求插件化，故**不追求端口数量稳定**，但**要求契约稳定**。
> 故：`ports/` 的公开方法签名存为金样文件，改签名必须同步改金样，
> 且金样变更必须在提交信息里说明理由。

## 为什么这道门禁值得存在

实现可以随意重构（这是第 5 步定的规矩）：改实现不改端口，不该让测试变红。

但**改端口**是另一回事——它意味着「所有实现者都要动」。
把签名存成金样的两个实际作用：

1. 端口不该给 `*args` / `**kwargs` 留后路（D-099 的纪律）。
   没有这道门禁，加 `**kwargs` 不会有任何报警，
   而那正是下一次重构必然要回来处理的坑。
2. 它是 D-099 的机械执行，不依赖自觉。

## 本测试抓什么

1. **金样与当前签名一致** —— 改端口不改金样会红
2. **端口不含 `*args` / `**kwargs`** —— D-099 的机械执行
3. **返回类型必须显式标注** —— 隐式返回等于契约不完整
4. **金样文件非空且覆盖全部端口** —— 防呆

## 第 4 条为什么必须存在

金样文件是本门禁唯一的"事实源"。
它若被人手改成空文件，`test_金样与当前签名一致` 就会用**空集骗过自己**——
签名对得上空文件，于是永远全绿。

这正是 D-143 的教训：一个表面像在约束、实际与目标无关的断言。
**判据：门禁的事实源必须有"它非空"的断言，否则它可以悄悄失效。**
"""

# ── 反证记录（这道门禁被攻击过 4 次）─────────────────────────
#   1. 改端口签名不改金样                  → 1 条红
#   2. 给端口加 **kwargs（D-099 后门）     → 1 条红
#   3. 删金样一行                          → 1 条红
#   4. **金样比对改成 assert True**（架空自己）→ ❌ 当时**全过**
#
# 第 4 条是横切审查查出的：当时的防呆是「金样文件非空」，
# 而被架空的是**比对动作**——文件还在，比对没了，门禁照过。
# 现由 test_gate_selfcheck.py::test_比对能发现新增方法 补上。

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "zhengtong"
PORTS = SRC / "ports"
GOLDEN = pathlib.Path(__file__).parent / "golden" / "ports_contract.txt"

#: 端口模块 → 端口类名。**显式列出**，不靠扫目录。
#: 扫目录会把 `__init__.py`、测试辅助之类也算进来，
#: 而金样要的是「契约」，不是「文件里有什么」。
端口清单: dict[str, str] = {
    "clock": "ClockPort",
    "content_pack": "ContentPackPort",
    "llm": "LlmPort",
    "retriever": "RetrieverPort",
    "secrets": "SecretsPort",
    "storage": "StoragePort",
    "user_files": "UserFilesPort",
    "validators": "ValidatorPort",
}


def _快照() -> dict[str, str]:
    """当前所有端口方法的签名 → 字符串。**这是金样的唯一来源。**"""
    import importlib

    出: dict[str, str] = {}
    for 模名, 类名 in 端口清单.items():
        模块 = importlib.import_module(f"zhengtong.ports.{模名}")
        类 = getattr(模块, 类名)
        for 成员 in sorted(dir(类)):
            # `__init__` 不在列——那是 Protocol 继承来的 object.__init__，
            # 不是对外契约的一部分。把它列进去会让金样失去意义。
            if 成员.startswith("_"):
                continue
            值 = getattr(类, 成员, None)
            if not callable(值):
                continue
            签 = str(inspect.signature(值))
            签 = 签.replace("self, ", "").replace("self", "")
            出[f"{模名}.{类名}.{成员}{签}"] = 签
    return 出


def _读金样() -> list[str]:
    if not GOLDEN.exists():
        return []
    return [
        行.strip()
        for 行 in GOLDEN.read_text(encoding="utf-8").splitlines()
        if 行.strip() and not 行.startswith("#")
    ]


class Test金样一致性:
    def test_金样与当前签名一致(self) -> None:
        """**改端口不改金样 → 变红。**"""
        当前 = set(_快照())
        金样 = set(_读金样())
        少 = sorted(当前 - 金样)
        多 = sorted(金样 - 当前)
        assert not 少, "新增端口方法但没写进金样：\n" + "\n".join(少)
        assert not 多, "金样里的方法已不存在（改签名/删方法须同步改金样）：\n" + "\n".join(多)

    def test_金样非空且覆盖全部端口(self) -> None:
        """**防呆：金样本身不能是空的或残缺的。**

        没有这条，金样被清空后 `test_金样与当前签名一致`
        会因「两边都是空」而永远通过——
        **一道悄悄失效的门禁比没有门禁更坏**，
        因为它让人以为这件事有人管。
        """
        金样 = _读金样()
        assert 金样, "金样文件为空或不存在——门禁会因此永远通过，沦为假绿"
        for 模名 in 端口清单:
            命中 = [行 for 行 in 金样 if 行.startswith(f"{模名}.")]
            assert 命中, f"金样里没有 {模名} 的任何方法——是漏写还是模块改名了？"


class Test端口纪律:
    """D-099：端口方法的参数必须显式列出。"""

    def test_端口不得含变长参数(self) -> None:
        """**`*args` / `**kwargs` 是契约的后门。**

        有了它，调用方可以传任何参数而类型检查不报；
        实现方也可以悄悄多收参数而不必改端口——
        于是「端口签名变了要通知所有实现者」这条约定失效。
        """
        违例 = [名 for 名, 签 in _快照().items() if "*" in 签]
        assert not 违例, (
            "端口方法含 *args / **kwargs：\n"
            + "\n".join(sorted(违例))
            + "\n\nD-099：端口方法的参数必须显式列出。"
            "变长参数会让签名变更无法被发现。"
        )

    def test_返回类型必须显式标注(self) -> None:
        """**没有返回标注 = 契约不完整。**

        D-102 要求 `ports/` 用 `from __future__ import annotations` + 字符串标注，
        所以这里查的是「有没有标注」，不是「标注是否具体」。
        """
        违例: list[str] = []
        for 模名, 类名 in 端口清单.items():
            文件 = PORTS / f"{模名}.py"
            树 = ast.parse(文件.read_text(encoding="utf-8"))
            for 类节点 in ast.walk(树):
                if not isinstance(类节点, ast.ClassDef) or 类节点.name != 类名:
                    continue
                for 节点 in 类节点.body:
                    if not isinstance(节点, ast.FunctionDef | ast.AsyncFunctionDef):
                        continue
                    if 节点.name.startswith("_"):
                        continue
                    if 节点.returns is None:
                        违例.append(f"{模名}.{类名}.{节点.name}")
        assert not 违例, (
            "端口方法缺返回类型标注：\n"
            + "\n".join(sorted(违例))
            + "\n\n无标注的方法，改了返回类型不会有任何测试报警。"
        )

    def test_端口模块不得导入适配层(self) -> None:
        """**这道其实属于 `test_layering`，但端口是契约的边界，值得再守一次。**

        端口一旦 import 适配器，依赖方向就反了：
        上层为了用端口会跟着 import，于是整个分层塌掉。
        """
        违例: list[str] = []
        for 文件 in sorted(PORTS.glob("*.py")):
            树 = ast.parse(文件.read_text(encoding="utf-8"))
            for 子 in ast.walk(树):
                模块 = ""
                if isinstance(子, ast.ImportFrom):
                    模块 = 子.module or ""
                    if 子.level and 模块.startswith("adapters"):
                        违例.append(f"{文件.name} 相对导入了 adapters")
                elif isinstance(子, ast.Import):
                    for 名 in 子.names:
                        if "adapters" in 名.name.split("."):
                            违例.append(f"{文件.name} 导入了 {名.name}")
        assert not 违例, "端口层导入了适配层：\n" + "\n".join(sorted(set(违例)))


# ── 这道门禁的判据自述 ──────────────────────────────────────────
#
# §12 机制 5 的原话是「改签名必须同步改金样，且变更须说明理由」。
# 前半句能机器化（就是 `test_金样与当前签名一致`），
# 后半句「说明理由」**不能机器化**——它靠提交信息与人的自觉。
#
# 这里不假装能管住它。把不可自动化的部分说清楚，
# 比写一条「看起来在管、实际什么都没管」的断言要好。
# D-143 的教训：断言要断言「该断言的目标」。
#
# 另：`_快照` 里对 `__init__` 的排除也是一次刻意的取舍——
# 它确实存在，但它来自 `Protocol`，不是任何端口自己声明的契约。
# 把它列进金样只会让金样变脏，且掩盖真正的签名变更。
@pytest.mark.parametrize("模名", sorted(端口清单))
def test_每个端口模块都存在且可导入(模名: str) -> None:
    """**防呆：端口模块本身没丢。**

    如果某个模块被删了，`端口清单` 里的 import 会在收集阶段直接报错——
    但那会让整个测试文件无法收集，报错信息指向 import 而不是「模块没了」。
    拆成参数化后，失败会指名道姓地说是哪个端口。
    """
    assert (PORTS / f"{模名}.py").exists(), f"端口模块 {模名}.py 不存在"
