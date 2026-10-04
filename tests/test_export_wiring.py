"""导出层接线（D-296）。

## 这批判据在补什么

`services/export.py` 此前在生产路径**零调用**——
`导出交付物` 全仓只有测试实例化它。于是两件事同时成立：

1. 最终 TXT **从不落盘**：编排器只产出 `编排结果.交付物`（一个字符串）
2. 用户硬约束「导出前必须扫描残留占位符，存在未处理占位符时禁止导出」
   **没有代码在执行它**

这是 D-165 一族的第四次：`probe()` 零调用、`跑完整流程` 零调用、
`合法写` 零调用，现在是导出层。件备好了、测过了，就是没人用。

## 为什么「拦住」不发 run.failed

用户约束原文是「**禁止导出**，并列出待补信息」——
禁的是写文件，不是判整轮失败。文稿本身已成形，
是「交付这一步没过去」。发 `run.failed` 会让界面显示运行失败，
而用户的稿件其实好好的。

故发 `export.blocked`。这个事件类型与 `导出交付物.取拦截事件`
**早就定义了却零调用**——正是 D-118「白名单里有、却没有分支」的老病，
本判据顺手把它接上。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zhengtong.adapters.paths import Workspace
from zhengtong.app import build_app_from_workspace
from zhengtong.domain.artifact import ArtifactKind
from zhengtong.domain.errors import ErrorCode
from zhengtong.ports.export import 未接线导出
from zhengtong.services.export import 导出交付物, 导出文件


def _写配置(根: Path, *, gb18030: bool) -> None:
    """造一份真配置。**直接写程序真会读的那份**。"""
    (根 / "system").mkdir(parents=True, exist_ok=True)
    (根 / "system" / "config.toml").write_text(
        f"[encoding]\ngb18030 = {'true' if gb18030 else 'false'}\n\n"
        '[llm]\nendpoint = "http://127.0.0.1:9/v1/chat/completions"\n'
        'model = "m"\napi_key = "sk-x"\ncontext_k = 128\ntemperature = 0.3\n',
        encoding="utf-8",
    )


class Test占位导出器不许静默成功:
    def test_占位导出器返回失败(self) -> None:
        """静默成功过一次（D-192 原委）：没接线却返回成功，
        上层以为导出完了，用户在目录里找不到文件却什么都没报错。"""
        got = 未接线导出()(文件=())
        assert got.is_error
        assert got.error is not None
        assert "未接线" in got.error.detail

    def test_占位用的是现成错误码不新造(self) -> None:
        got = 未接线导出()(文件=())
        assert got.error is not None
        assert got.error.code is ErrorCode.UNSOURCED_ASSERTION


class Test扫描拦得住:
    def test_残留占位符拒绝导出且不落盘(self, tmp_path: Path) -> None:
        导出 = 导出交付物(根=tmp_path, 运行号="r-1")
        got = 导出(文件=(导出文件("01-主件.txt", "正文【待填 · D3】", ArtifactKind.MAIN_DOCUMENT),))
        assert got.is_error, "有残留占位符却放行了"
        错 = got.error
        assert 错 is not None
        assert 错.code is ErrorCode.PLACEHOLDER_REMAIN
        assert not (tmp_path / "r-1" / "output").exists(), "拦住了却还是建了目录"

    def test_干净的正文才落盘(self, tmp_path: Path) -> None:
        导出 = 导出交付物(根=tmp_path, 运行号="r-1")
        got = 导出(文件=(导出文件("01-主件.txt", "干净的正文。", ArtifactKind.MAIN_DOCUMENT),))
        assert got.is_ok, got.error.detail if got.error else ""
        落 = tmp_path / "r-1" / "output" / "01-主件.txt"
        assert 落.exists(), "干净正文也没落盘"
        assert 落.read_text(encoding="utf-8") == "干净的正文。"


class Test编码真落到最终TXT:
    def test_默认是utf8(self, tmp_path: Path) -> None:
        导出交付物(根=tmp_path, 运行号="r-1")(
            文件=(导出文件("a.txt", "正文", ArtifactKind.MAIN_DOCUMENT),)
        )
        p = tmp_path / "r-1" / "output" / "a.txt"
        assert p.read_bytes().decode("utf-8") == "正文"

    def test_配了gb18030就真写gb18030(self, tmp_path: Path) -> None:
        """D-235 的原委：存储层按配置写对了，**导出层又写回 UTF-8**。
        用户配的 `gb18030: true` 管得到过程产物，管不到最终交付件。"""
        导出交付物(根=tmp_path, 运行号="r-1", 编码="gb18030")(
            文件=(导出文件("a.txt", "正文内容", ArtifactKind.MAIN_DOCUMENT),)
        )
        p = tmp_path / "r-1" / "output" / "a.txt"
        原 = p.read_bytes()
        assert 原.decode("gb18030") == "正文内容"
        with pytest.raises(UnicodeDecodeError):
            原.decode("utf-8")

    def test_两次运行不互相覆盖(self, tmp_path: Path) -> None:
        """输出目录必须带 run 目录——共用 `output/` 会覆盖上一次的文件。"""
        for run in ("r-1", "r-2"):
            导出交付物(根=tmp_path, 运行号=run)(
                文件=(导出文件("a.txt", run, ArtifactKind.MAIN_DOCUMENT),)
            )
        assert (tmp_path / "r-1" / "output" / "a.txt").read_text(encoding="utf-8") == "r-1"
        assert (tmp_path / "r-2" / "output" / "a.txt").read_text(encoding="utf-8") == "r-2"


class Test装配根传真导出器:
    """这一类是**同型第四次**的核心判据：
    接线在不在，只看装配根有没有把它往下传。"""

    def test_装配出的应用带着导出器(self, tmp_path: Path) -> None:
        _写配置(tmp_path, gb18030=False)
        got = build_app_from_workspace(Workspace(root=tmp_path))
        assert got.is_ok, got.error.detail if got.error else ""
        应用 = got.unwrap()
        try:
            assert 应用.导出 is not None
            # **不能是占位**——占位返回失败，真实运行就导不出文件
            assert not isinstance(应用.导出, 未接线导出), "装配根发的仍是占位导出器"
        finally:
            应用.close()

    def test_装配根按配置把编码喂给导出器(self, tmp_path: Path) -> None:

        _写配置(tmp_path, gb18030=True)
        got = build_app_from_workspace(Workspace(root=tmp_path))
        assert got.is_ok, got.error.detail if got.error else ""
        应用 = got.unwrap()
        try:
            # **验行为，不验字段**：`导出` 的类型是 `ExportPort` 协议，
            # 协议上刻意没有 `编码`——那会把它变成实现细节（D-070）。
            # 直接落一份文件，看字节到底是什么编码。
            got = 应用.导出(文件=(导出文件("a.txt", "正文内容", ArtifactKind.MAIN_DOCUMENT),))
            assert got.is_ok, got.error.detail if got.error else ""
            落 = tmp_path / "data" / "r-0001" / "output" / "a.txt"
            assert 落.exists(), f"导出器没落盘：{got.unwrap()}"
            assert 落.read_bytes().decode("gb18030") == "正文内容"
        finally:
            应用.close()
