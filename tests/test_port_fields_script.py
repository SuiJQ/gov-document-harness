"""`check_port_fields.py` 自身的判据。

## 为什么一个检查脚本也需要判据

它决定「哪些端口字段算零消费」，而**判错的后果是双向的**：

- **漏报**：真有零消费的字段被放过，缺陷继续潜伏
- **误报**：报出一个必然红的条目，用红灯掩盖真实状态

第二种更隐蔽——它会让人习惯性忽略这个脚本的输出，
于是它真正抓到东西时也没人看。

D-295 撞上的正是第二种：新增 `探测结论.项序`（一个 `ClassVar`）后，
脚本把它报成零消费候选，而它按定义就该被所有代码读。
那不是代码有问题，是**脚本口径有问题**。

所以修完必须留判据，否则下次有人把 ClassVar 支持删了，
没人会知道。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

脚本 = Path(__file__).resolve().parents[1] / "scripts" / "check_port_fields.py"


def _载入():
    spec = importlib.util.spec_from_file_location("cpf", 脚本)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _字段集(mod) -> set[tuple[str, str]]:
    return {(类, 名) for _, 类, 名 in mod._端口字段()}


class TestClassVar不算实例字段:
    def test_类常量不进字段集(self) -> None:
        """`ClassVar` 不进 `__init__`、不随实例走——它不是 dataclass 字段。

        把它算成字段，脚本就会因为「用户路径里没直接读它」而误报，
        而类常量按定义就该被所有代码读。
        """
        mod = _载入()
        assert ("探测结论", "项序") not in _字段集(mod)

    def test_实例字段仍然进字段集(self) -> None:
        """**别把 ClassVar 支持写成「什么都不收」**——
        那会让整个脚本静默失效（空集骗过自己）。"""
        mod = _载入()
        收 = _字段集(mod)
        assert ("探测结论", "通过项") in 收
        assert ("LlmResponse", "raw_finish_reason") in 收
        assert ("ValidationIssue", "at") in 收

    def test_字段集非空(self) -> None:
        """D-143 的教训：事实源必须先断言「它非空」，
        否则它可以被悄悄改成空集骗过自己。"""
        mod = _载入()
        assert len(_字段集(mod)) > 20, f"只收到 {_字段集(mod)}，是收窄过头了"


class Test基线不被本轮改动弄脏:
    def test_脚本自身跑通(self) -> None:
        """**改判据脚本必须自己先过**——
        否则一个坏口径能一直留在仓库里。"""
        mod = _载入()
        断言 = mod.main()
        # 退出码只可能因为「有零消费候选」而非 0；
        # 当前已全部定性，故应为 0。
        assert 断言 == 0, "端口字段检查在当前工作区报红"

    def test_白名单条目都有理由(self, tmp_path: Path) -> None:
        """白名单是**人工判断的沉淀**，空理由等于没判断。"""
        mod = _载入()
        for 名, 理由 in mod.白名单.items():
            assert 理由.strip(), f"白名单「{名}」没写理由"

    def test_已修条目都有理由(self) -> None:
        mod = _载入()
        for 名, 理由 in mod.已修.items():
            assert 理由.strip(), f"已修「{名}」没写理由"


@pytest.mark.parametrize("字段", ["通过项"])
def test_通过项确有其消费(字段: str) -> None:
    """白名单条目必须在真实代码里被读到，不能只是「看着像被用了」。"""
    源 = (Path(__file__).resolve().parents[1] / "src" / "zhengtong" / "ports" / "llm.py").read_text(
        encoding="utf-8"
    )
    assert f"self.{字段}" in 源, f"探测结论 取() 没读 {字段}"
    # 且那个方法真被外部调到
    消费 = (
        Path(__file__).resolve().parents[1] / "src" / "zhengtong" / "cli" / "main.py"
    ).read_text(encoding="utf-8")
    assert ".取(" in 消费, "没有任何地方调用 探测结论.取()——白名单理由已过期"
