"""导出层：占位符拦截、编码、多份产出（D-034/D-044/D-038）。

## 架构 §9 的原文要求

| 检查 | 失败处置 |
|---|---|
| 残留占位符 `〔待填：…〕` `〔待核实：…〕` | **禁止导出**，列出待补信息 |
| 必填字段 `provenance != USER` 且无值（D4/D5/D7） | 禁止导出 |
| 未标注来源的数据型断言 | 禁止导出 |
| D-Card 降级字段 `degraded=True` | 允许导出，交付说明首行醒目提示 |

编码（§9.2）：`gb18030` 只影响产出文件，日志与界面恒 UTF-8。
产出布局（§9.3）：`data/<run-id>/output/`，`01-主件-….txt`、`02-附件1-….txt`。

## 两处文档与实现的落差（判据按实现写，另记决策）

1. **占位符格式**：架构写 `〔待填：…〕`，实现是 `【待填 · D3】`
   （`_渲染事实块` 产出）。判据扫**实现真格式**——
   扫一个代码从不产出的格式，等于扫了个寂寞。
2. **`degraded` 字段不存在**：`DCardField` 没有这个属性，
   架构 §9.1 最后一行无载体。本步不硬造该字段，
   降级提示留到有载体时再补（D-116 的标记还在编排注释里）。

## 判据只从外部观察

查扫描结论、落盘文件、事件载荷，不断言源码里有没有某行。
"""

from __future__ import annotations

from pathlib import Path

from zhengtong.domain.artifact import ArtifactId, ArtifactKind, ArtifactRef
from zhengtong.services.export import 导出文件


class Test残留占位符拦截:
    def test_有待填项则禁止导出(self) -> None:
        """**核心判据·D-034**：残留占位符**禁止导出**。

        这是最高数据纪律：「严禁编造」的机械落地——
        光靠提示词约束模型不许编造是不可靠的（架构 §2 原文）。
        """
        from zhengtong.services.export import 扫描产出

        结论 = 扫描产出((("正文.txt", "正文：关于防汛工作的通知【待填 · D3】"),))
        assert not 结论.可导出, "有待填项却放行了——严禁编造形同虚设"
        assert 结论.待补, "拦截了却没列出待补信息——用户不知道要补什么"

    def test_待核实项同样拦截(self) -> None:
        from zhengtong.services.export import 扫描产出

        结论 = 扫描产出((("正文.txt", "数据为【待核实 · D7】"),))
        assert not 结论.可导出, "待核实项被放行了"

    def test_无占位符则放行(self) -> None:
        """**反向**：干净稿件不能被误拦——误拦等于门禁失效。"""
        from zhengtong.services.export import 扫描产出

        结论 = 扫描产出((("正文.txt", "关于防汛工作的通知\n\n请各地于六月三十日前上报。"),))
        assert 结论.可导出, f"干净稿件被拦了：{结论.待补}"
        assert not 结论.待补

    def test_列出待补的是字段号不是整段正文(self) -> None:
        """D-038 要求「**列出待补信息**」——
        报字段号，用户才知道去补哪一条。

        把整段正文糊回给用户既没用又占地方
        （用户明确要求「不要占太大空间」）。
        """
        from zhengtong.services.export import 扫描产出

        结论 = 扫描产出((("正文.txt", "正文【待填 · D3】还有【待核实 · D7】"),))
        列出 = " ".join(结论.待补)
        assert "D3" in 列出 and "D7" in 列出, f"没列出字段号：{列出}"
        assert len(结论.待补) == 2, f"重复计入：{结论.待补}"


class Test导出:
    def test_拦截时不落任何文件(self) -> None:
        """**核心判据**：拦截必须是**真的不写**，
        不是「写了再删」或「标记一下」。"""
        from zhengtong.services.export import 导出交付物

        got = 导出交付物(根=Path("/tmp/不存在的目录"))(
            文件=(导出文件("01-主件-通知.txt", "正文【待填 · D3】", ArtifactKind.MAIN_DOCUMENT),)
        )
        assert got.is_error, "有待填项却导出成功了"
        assert got.error is not None
        assert got.error.code.value == "E050", f"错误码不对：{got.error.code}"

    def test_拦截时发export_blocked并带待补清单(self) -> None:
        """事件载荷契约由投影层定死：`placeholders`（D-038）。"""
        from zhengtong.services.export import 导出交付物, 扫描产出

        结论 = 扫描产出((("正文.txt", "正文【待填 · D3】"),))
        事件 = 导出交付物.取拦截事件(结论)
        assert 事件 is not None, "拦截了却没留下用户可见的事件"
        待补 = 事件["placeholders"]
        assert 待补, f"事件没带待补清单：{事件}"
        assert "D3" in str(待补), f"待补清单里没有字段号：{待补}"

    def test_整条导出路径都过扫描(self, tmp_path: Path) -> None:
        """**本条治「判据只测扫描器、没测导出路径」**。

        证伪时发现的：`扫描产出` 的判据全绿，而把导出里的
        `if not 结论.可导出` 改成 `if False` 也只红一条——
        因为前几条直接调扫描器，绕过了导出路径。

        这与 D-233「两层各自绿、没人对账」是同一类毛病。
        故本条**只走导出路径**，不碰扫描器。
        """
        from zhengtong.services.export import 导出交付物

        got = 导出交付物(根=tmp_path, 运行号="r-0001")(
            文件=(
                导出文件("01-主件-通知.txt", "正文【待填 · D3】", ArtifactKind.MAIN_DOCUMENT),
                导出文件("02-附件1-清单.txt", "干净的附件", ArtifactKind.ATTACHMENT),
            )
        )
        assert got.is_error, "正文留着占位符却导出成功了"
        out = tmp_path / "r-0001" / "output"
        落了 = sorted(p.name for p in out.iterdir()) if out.is_dir() else []
        assert not 落了, f"拦截了却落了文件（半套交付物比什么都不写更难收拾）：{落了}"

    def test_放行时发export_done并带文件清单(self) -> None:
        """载荷契约：`files`（D-038「列出全部文件」）。"""
        from zhengtong.services.export import 导出交付物, 扫描产出

        结论 = 扫描产出((("正文.txt", "干净的正文"),))
        事件 = 导出交付物.取完成事件(结论, ("01-主件-通知.txt",))
        assert 事件 is not None, "放行了却没发完成事件"
        文件 = 事件["files"]
        assert "01-主件-通知.txt" in str(文件), f"完成事件没点名交付了哪些文件：{文件}"


class Test多份产出:
    def test_主件只有一个(self, tmp_path: Path) -> None:
        """D-038：`**唯一**主件`。两个主件用户不知道看哪个。"""
        from zhengtong.services.export import 导出交付物

        got = 导出交付物(根=tmp_path, 运行号="r-0001")(
            文件=(
                导出文件("01-主件-通知.txt", "甲", ArtifactKind.MAIN_DOCUMENT),
                导出文件("02-附件1-清单.txt", "乙", ArtifactKind.MAIN_DOCUMENT),
            )
        )
        assert got.is_error, "两个主件竟放行了"

    def test_主件加配套件一起交付(self, tmp_path: Path) -> None:
        """D-038：用户拿到的是「一个项目的全部东西」，不是单个文件。"""
        from zhengtong.services.export import 导出交付物

        got = 导出交付物(根=tmp_path, 运行号="r-0001")(
            文件=(
                导出文件("01-主件-通知.txt", "甲", ArtifactKind.MAIN_DOCUMENT),
                导出文件("02-附件1-清单.txt", "乙", ArtifactKind.ATTACHMENT),
                导出文件("03-附件2-附表.txt", "丙", ArtifactKind.ATTACHMENT),
            )
        )
        assert not got.is_error, f"正常多件被拦：{got.error}"
        out = tmp_path / "r-0001" / "output"
        落了 = sorted(p.name for p in out.iterdir())
        assert 落了 == [
            "01-主件-通知.txt",
            "02-附件1-清单.txt",
            "03-附件2-附表.txt",
        ], f"落盘不对：{落了}"

    def test_文件名按序号前缀排序落盘(self, tmp_path: Path) -> None:
        """用户明确「主送机关等必要内容要素」，
        文件名带序号的约定（架构 §9.3）必须真的生效。"""
        from zhengtong.services.export import 导出交付物

        导出交付物(根=tmp_path, 运行号="r-0001")(
            文件=(
                导出文件("03-附件2-丙.txt", "丙", ArtifactKind.ATTACHMENT),
                导出文件("01-主件-甲.txt", "甲", ArtifactKind.MAIN_DOCUMENT),
                导出文件("02-附件1-乙.txt", "乙", ArtifactKind.ATTACHMENT),
            )
        )
        序 = [p.name for p in sorted((tmp_path / "r-0001" / "output").iterdir())]
        assert 序 == ["01-主件-甲.txt", "02-附件1-乙.txt", "03-附件2-丙.txt"], f"{序}"


class Test产物与存储的衔接:
    def test_交付物kind进ArtifactRef(self) -> None:
        """导出层不认 `is_deliverable` 就等于这条性质没人用。

        端口注释写着「只有交付物才走导出扫描与门禁」——
        导出层却不过滤，等于三个 kind 混着被扫描。
        """
        assert ArtifactKind.MAIN_DOCUMENT.is_deliverable
        assert ArtifactKind.ATTACHMENT.is_deliverable
        assert not ArtifactKind.DRAFT.is_deliverable, "初稿不算交付物"
        ref = ArtifactRef(id=ArtifactId("x"), kind=ArtifactKind.MAIN_DOCUMENT, path="p.txt")
        assert ref.kind.is_deliverable

    def test_运行号进输出目录(self) -> None:
        """约束：`data/<run-id>/output/`——**两次运行不能互相覆盖**。

        判据看真实路径，不看属性拼对没拼对。
        """
        from zhengtong.services.export import 导出交付物

        导出 = 导出交付物(根=Path("data"), 运行号="r-0007")
        导出(文件=(导出文件("01-主件-甲.txt", "甲", ArtifactKind.MAIN_DOCUMENT),))
        assert 导出.输出目录 == Path("data") / "r-0007" / "output", (
            f"运行目录没带上——两次运行会互相覆盖：{导出.输出目录}"
        )
        assert 导出.输出目录.is_dir(), "输出目录没建出来"
        assert 导出.输出目录.name == "output"
