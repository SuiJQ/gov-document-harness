"""产物编码必须真的是用户配的那个（D-235）。

## 这条欠账的形状

用户在约束里明写：「TXT 默认 UTF-8；配置 `gb18030: true` 时产出改用
GB18030，但日志和界面始终使用 UTF-8。」

代码里 `EncodingConfig.name()` 把这项算得清清楚楚，
`FsStorage` 也**有 `encoding` 字段**——但装配根那行是：

```python
storage = FsStorage(ws=ws)      # ← 没传 encoding
```

于是永远用默认 `"utf-8"`。配置里的 `gb18030: true` **从未影响任何输出**。
与 D-231 同型：值在那里，没人往下传。

## 一个额外的坑：不能整仓改编码

用户同时要求「日志和界面始终使用 UTF-8」。
所以**不能**把 `encoding` 字段直接喂配置——那会把
`.state.json`（断点续跑要读）、日志都写成 GB18030，
UTF-8 版的工具和控制台读它们会乱码。

正确做法：**只让产物走配置编码，运行数据恒为 UTF-8。**
端口本就分开了两个入口（`write_atomic` 跑运行数据、
`put_artifact` 落产物），所以这个区分是现成的。

## 判据写真实配置文件，不注入

`build_app_from_workspace` 不收 `config` 参数——它自己读
`system/config.toml`。这是**真实运行的入口**，
故判据也走这条路径：在临时工作区写真配置，看落盘字节。
"""

from __future__ import annotations

import json
from pathlib import Path

from zhengtong.adapters.config import Config
from zhengtong.domain.artifact import ArtifactId, ArtifactKind, ArtifactRef
from zhengtong.domain.run_state import RunId


def _工作区(tmp: Path, 配置文本: str) -> Path:
    """按给定 config.toml 建一个能跑的临时工作区。"""
    (tmp / "system").mkdir(parents=True, exist_ok=True)
    (tmp / "system" / "config.toml").write_text(配置文本, encoding="utf-8")
    return tmp


_配置头 = """
[llm]
endpoint = "https://example.invalid/v1/chat/completions"
model = "m"
temperature = 0.3
api_key = "sk-test-not-a-real-key"
context_k = 128
"""


def _装(根: Path) -> object:
    """从工作区装 App（返回 Result，取出 App）。"""
    from zhengtong.adapters.paths import Workspace
    from zhengtong.app import build_app_from_workspace

    got = build_app_from_workspace(Workspace(root=根))
    assert not got.is_error, f"装配失败：{got.error}"
    app = got.unwrap()
    assert app is not None
    return app


def _落一个产物(根: Path, 内容: str = "汉字测试") -> Path:
    app = _装(根)
    st = app.ports.storage  # pyright: ignore[reportAttributeAccessIssue]
    ref = ArtifactRef(
        id=ArtifactId("a-1"),
        kind=ArtifactKind.MAIN_DOCUMENT,
        path="r-0001/正文.txt",
    )
    got = st.put_artifact(ref, 内容)
    assert not got.is_error, f"写产物失败：{got.error}"
    return 根 / "data" / "r-0001" / "正文.txt"


class Test产物编码随配置:
    def test_默认是utf8(self, tmp_path: Path) -> None:
        """**核心判据·默认档**：不配就是 UTF-8（用户约束的默认值）。"""
        根 = _工作区(tmp_path, _配置头 + "\n[encoding]\ngb18030 = false\n")
        原始 = _落一个产物(根).read_bytes()
        assert 原始.decode("utf-8") == "汉字测试"
        # UTF-8 下 4 个汉字 = 12 字节；GB18030 下是 8 字节
        assert len(原始) == 12, f"默认档应为 UTF-8 的 12 字节，实际 {len(原始)}"

    def test_配了gb18030产物就是gb18030(self, tmp_path: Path) -> None:
        """**核心判据**：`gb18030 = true` 必须真的改变落盘字节。

        判据看**原始字节**，不看「解码能成功」——
        后者区分不了 UTF-8 与 GB18030（两者都能解，只是解出来不同）。
        """
        根 = _工作区(tmp_path, _配置头 + "\n[encoding]\ngb18030 = true\n")
        原始 = _落一个产物(根).read_bytes()
        assert 原始.decode("gb18030") == "汉字测试"
        assert len(原始) == 8, f"应为 4 汉字 ×2 字节 = 8，实际 {len(原始)}"

    def test_两种配置落盘字节不同(self, tmp_path: Path) -> None:
        """最强的外部观察：同一内容，两种配置 → 字节不同。"""
        默认 = _落一个产物(
            _工作区(tmp_path / "a", _配置头 + "\n[encoding]\ngb18030 = false\n")
        ).read_bytes()
        gb = _落一个产物(
            _工作区(tmp_path / "b", _配置头 + "\n[encoding]\ngb18030 = true\n")
        ).read_bytes()
        assert 默认 != gb, "两种配置落盘字节相同——配置没起作用"

    def test_gb18030下内容仍能解回(self, tmp_path: Path) -> None:
        根 = _工作区(tmp_path, _配置头 + "\n[encoding]\ngb18030 = true\n")
        p = _落一个产物(根)
        assert p.read_text(encoding="gb18030") == "汉字测试"


class Test运行数据恒为utf8:
    def test_状态文件不受产物编码影响(self, tmp_path: Path) -> None:
        """**这是配套约束**：用户要求「日志和界面始终 UTF-8」。

        若把产物编码整个塞进 `FsStorage.encoding`，
        `.state.json` 也会变 GB18030——断点续跑与工具链会乱码。
        """
        根 = _工作区(tmp_path, _配置头 + "\n[encoding]\ngb18030 = true\n")
        app = _装(根)
        st = app.ports.storage  # pyright: ignore[reportAttributeAccessIssue]
        got = st.save_run_state(_最小状态())
        assert not got.is_error, f"存状态失败：{got.error}"
        状态文件 = 根 / "data" / "r-0001" / "state.json"
        assert 状态文件.is_file(), "状态文件没落盘"
        数据 = json.loads(状态文件.read_text(encoding="utf-8"))
        assert 数据["run_id"] == "r-0001"


def _最小状态():
    from zhengtong.domain.phase import PhaseId
    from zhengtong.domain.run_state import RunState
    from zhengtong.domain.task import TaskSpec, TierId

    return RunState(
        schema_version=1,
        run_id=RunId("r-0001"),
        task=TaskSpec(tier=TierId.PRO, requirement="写一份通知"),
        phase=PhaseId.INTAKE,
    )


class Test配置字段本身:
    def test_EncodingConfig算出正确编码名(self) -> None:
        """**防退化**：`name()` 若算错，上面几条会一起失效。"""
        from zhengtong.adapters.config import EncodingConfig

        assert Config().encoding.name() == "utf-8"
        assert Config(encoding=EncodingConfig(gb18030=True)).encoding.name() == "gb18030"
