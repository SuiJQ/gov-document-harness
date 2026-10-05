"""缺陷 6 与 7：退出键。

## 两个缺陷是同一条链

- `Binding("ctrl+o", "退出")` 从建起就在，但 **`action_退出` 全仓不存在**
  → 退出功能整体不可用（实测连发 40 次进程纹丝不动）
- 界面文案写「Ctrl+Q 退出」，而 `ctrl+q` 绑的是 `noop`
  → 用户照着提示按，按的是一个什么都不干的键

## 为什么这两条能在 1957 条判据全绿的情况下存在

原有判据 `test_ctrl_q按了不退` 断的是**行为**（按了不退出），
写得对、也真的在断。但它断的是「解绑成功」，
**没有一条判据断「退出键真的能退」**——
`action_退出` 不存在这件事，没有任何判据看得见。

更细一层：`test_退出来自O` 只断言 `BINDINGS` 里**有** `ctrl+o`，
不检查它有没有落点。**配置里有 ≠ 按了会动**，
这正是注释里自己写过的话，却只用在了 ctrl+q 那一侧。

## 关键：这条判据不能只查源码

「`hasattr(应用, 'action_退出')`」那种查法会绿——
Textual 允许 action 不存在，只是按下去没反应。
所以这里用 `run_test()` 真按一次 `ctrl+o`，断 `is_running` 变 False。
"""

from __future__ import annotations

import ast
import asyncio
import inspect

from textual.binding import Binding

from zhengtong.adapters import tui_theme as 主题
from zhengtong.adapters.tui import ZhengTongApp
from zhengtong.presentation.tui import 事件桥接


def _绑定表() -> list[Binding]:
    """把 `BINDINGS` 归一化成 `Binding` 列表。

    ## 为什么需要这一步

    textual 的 `BindingType = Binding | tuple[str, str] | tuple[str, str, str]`——
    **元组简写是它自己认可的写法**，但那样 `b.key` / `b.action`
    在类型上不成立（tuple 没有这两个属性）。

    直接写 `getattr(b, "key", None)` 能骗过类型检查，
    却把「写错了形状」和「真写了元组」变成同一件事——
    那正是本文件要抓的缺陷类别。故显式归一化。

    **按长度分开构造，不写 `Binding(*b)`**：展开调用下
    类型检查器无法确认第三个元素是 `description`（`str`）还是
    `show`（`bool`），只能报一串「str 不能赋给 bool」。
    分开写既过类型，又顺带**把畸形绑定挡在这里**——
    三元组以上或一元组会让 `ValueError` 当场炸，
    而不是让一个坏绑定混进 BINDINGS。
    """
    出: list[Binding] = []
    for b in ZhengTongApp.BINDINGS:
        if isinstance(b, Binding):
            出.append(b)
        elif len(b) == 2:
            出.append(Binding(b[0], b[1]))
        elif len(b) == 3:
            出.append(Binding(b[0], b[1], b[2]))
        else:
            raise ValueError(f"BINDINGS 里的绑定既不是 Binding 也不是 2/3 元组：{b!r}")
    return 出


def _去掉文档与注释(源码: str) -> str:
    """剥掉文档串与注释，只留可执行语句。

    ## 为什么必须剥

    这条判据要查「`action_退出` 里有没有 `result=`」。
    而**文档里提到 `result=` 是正常的**——那段话正是在解释
    「为什么不要传 `result=`」。不剥就会红在注释上。

    这正是本轮批评 `test_orchestration.py` 那个毛病：
    **查源码文本的判据会把注释里的字样当真代码。**
    自己写判据时就得先把这道缝堵上。
    """
    树 = ast.parse(_去缩进(源码))
    for 节 in ast.walk(树):
        if isinstance(节, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            首 = 节.body[0] if 节.body else None
            if (
                首 is not None
                and isinstance(首, ast.Expr)
                and isinstance(首.value, ast.Constant)
                and isinstance(首.value.value, str)
            ):
                节.body.pop(0)
    return ast.unparse(树)


def _去缩进(源码: str) -> str:
    """`inspect.getsource` 给出的是**带缩进的方法片段**，
    `ast.parse` 直接吃会报 `IndentationError: unexpected indent`。
    先按公共缩进量左移，再解析。"""
    行 = 源码.splitlines()
    # **不用 `l` 当循环变量**：单字母 `l` 与数字 1 难分辨（E741）。
    非空 = [条 for 条 in 行 if 条.strip()]
    if not 非空:
        return 源码
    缩进 = min(len(条) - len(条.lstrip()) for 条 in 非空)
    return "\n".join(条[缩进:] if 条.strip() else "" for 条 in 行)


def _应用() -> ZhengTongApp:
    """**闸传 `None`**：判据不测提问链路，闸接上反而要造对接口。

    `闸.挂答复(答复)` 收的是**一个函数**，不是一问一答两个参数——
    我第一版按 `(qid, 答复)` 写，签名错了直接 `TypeError`。
    判据不需要闸就别给，接口对不对另说。
    """
    return ZhengTongApp(桥=事件桥接(), 根=None, 取编排器=None, 闸=None)


class Test退出键真的能退:
    def test_ctrl_o按了真退出(self) -> None:
        """**按一次 `ctrl+o`，程序必须真的结束。**

        这条是缺陷 7 的判据。此前 `action_退出` 不存在，
        按 40 次都没反应——而 1957 条判据全绿。
        """

        async def 跑() -> None:
            应用 = _应用()
            async with 应用.run_test() as 试点:
                await 试点.press("ctrl+o")
                await 试点.pause()
                assert not 应用.is_running, (
                    "ctrl+o 按了程序还在跑——退出动作没接上（这正是缺陷 7：action_退出 不存在）"
                )

        asyncio.run(跑())

    def test_退出方法真的存在(self) -> None:
        """存在性单独断一条，便于失败时一眼看出是「没写」还是「写错」。"""
        assert hasattr(ZhengTongApp, "action_退出"), (
            "action_退出 不存在——Binding('ctrl+o','退出') 指向空"
        )

    def test_每个退出绑定都有落点(self) -> None:
        """**每个退出类绑定都得有对应的 action**。

        这条是为「配置里有 ≠ 按了会动」设的：
        `test_退出来自O` 只查 `BINDINGS` 里有这个键，
        查不出它指向的动作存不存在——缺陷 7 就藏在这道缝里。
        """
        退类 = {"退出", "quit"}
        缺 = [
            b.key
            for b in _绑定表()
            if b.action in 退类 and not hasattr(ZhengTongApp, f"action_{b.action}")
        ]
        assert not 缺, f"退出类绑定没有落点：{缺}"


class Test界面文案与真实键位一致:
    def test_文案里的退出键是真的那个(self) -> None:
        """缺陷 6：文案写 `Ctrl+Q`，真键是 `Ctrl+O`。

        用户照界面提示操作。文案与绑定不一致 = 界面在骗人。

        **只查 `compose` 的可执行体**，不查整个类——
        否则注释里写的「原写 Ctrl+Q」会被当成「现在还写着」。
        """
        体 = _去掉文档与注释(inspect.getsource(ZhengTongApp.compose))
        assert "Ctrl+Q 退出" not in 体, "文案还在教用户按 Ctrl+Q——而那个键绑的是 noop"
        assert "Ctrl+O 退出" in 体, "文案的退出键应与 键位表 一致"

    def test_文案与键位表逐字对得上(self) -> None:
        """**从 `键位表` 推文案，不再手写。**

        手写就有下一次分叉——真退出键改了而文案没改，
        同一个毛病会再犯一遍。键位表是唯一事实源。
        """
        assert 主题.键位表["ctrl+o"] == "退出（Out）", "键位表本身变了，先改键位表"
        体 = _去掉文档与注释(inspect.getsource(ZhengTongApp.compose))
        assert "Ctrl+O" in 体, "界面上没告诉用户退出键是什么"

    def test_被解绑的键不出现在文案里(self) -> None:
        """`键位表_未实现`（含 `ctrl+q`）不该出现在文案。"""
        体 = _去掉文档与注释(inspect.getsource(ZhengTongApp.compose))
        for k in 主题.键位表_未实现:
            上 = f"Ctrl+{k.split('+')[1].upper()}"
            assert f"{上} 退出" not in 体, f"文案把「明确不做」的键 {k} 写成了退出键"


class Test退出不影响收尾:
    def test_退出码仍由启动算(self) -> None:
        """**退出码只有一个来源**：`启动` 按 `运行失败` 算。

        `action_退出` 若改用 `exit(result=…)` 传值，
        退出码就有两个来源，两处一改就分叉——
        这正是本轮修掉的缺陷 4 那类毛病，不该重新引入。
        """
        源 = _去掉文档与注释(inspect.getsource(ZhengTongApp.action_退出))
        assert "result=" not in 源, "action_退出 不要传 result——退出码归 启动 一处算"
        assert "self.exit()" in 源, "action_退出 应直接调 self.exit()"
