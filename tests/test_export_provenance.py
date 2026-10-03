"""导出扫描要能指出「哪个文件的哪一行」（D-263）。

## 缺陷比原判更重：不只是没人读，**构造时就是空的**

`扫描结论.原文出处` 声明了 `field(default=())`，
而 `扫描产出` 从来只填 `可导出` 与 `待补`——
**这个字段从落地起就没有过值**（D-265 的复查结论）。

更上游的原因在签名：`扫描产出(文本们: Sequence[str])` **只收裸文本**。
`导出交付物` 手里明明有 `导出文件.文件名`，却只传了 `内容`，
**文件名在调用处就被丢掉了**——扫描器根本无从知道扫的是哪份文件。

## 为什么改签名收「对」

改成 `Sequence[tuple[str, str]]`（文件名, 内容），
而不是加一个平行的名称列表：**两个平行列表一旦长度对不上，
报错的位置就会指到别的文件去——那比不报更坏。**

## 两个字段分开装，不合成一份

| | 回答 | 去重键 |
|---|---|---|
| `待补` | 要补哪几个字段 | 字段号（同一字段补一次） |
| `原文出处` | 去哪个文件的哪一行 | 文件 + 行号（同一处只列一次） |

合成一份清单会翻倍，用户明确要求界面「不要占太大空间」（D-038）。
"""

from __future__ import annotations

from zhengtong.services.export import 导出交付物, 扫描产出, 扫描结论


def _扫(*段: tuple[str, str]) -> 扫描结论:
    return 扫描产出(段)


class Test出处真的被填上:
    def test_字段不再是空的(self) -> None:
        """**本条最直接的判据**：此前它构造出来就是 `()`。"""
        c = _扫(("正文初稿.txt", "正文【待填 · D3】"))
        assert c.原文出处, "原文出处仍是空的——这正是 D-263 的原状"

    def test_带文件名(self) -> None:
        c = _扫(("正文初稿.txt", "【待填 · D3】"))
        assert any("正文初稿.txt" in 出 for 出 in c.原文出处)

    def test_带行号且从1起(self) -> None:
        """行号**从 1 起**——显示「第 0 行」只会让人以为自己算错了。"""
        c = _扫(("正文初稿.txt", "第一行\n第二行\n第三行【待填 · D3】"))
        assert c.原文出处 == ("正文初稿.txt 第3行",), c.原文出处

    def test_第一行就是第1行(self) -> None:
        c = _扫(("正文初稿.txt", "【待填 · D3】"))
        assert c.原文出处 == ("正文初稿.txt 第1行",), c.原文出处

    def test_同一行两个占位符只列一处(self) -> None:
        """同一行去重——用户去那一行一次就能补完。"""
        c = _扫(("正文初稿.txt", "【待填 · D3】与【待核实 · D7】"))
        assert c.原文出处 == ("正文初稿.txt 第1行",), c.原文出处
        assert len(c.待补) == 2, "待补清单应仍列两个字段"

    def test_跨文件各记各的(self) -> None:
        c = _扫(
            ("正文初稿.txt", "【待填 · D3】"),
            ("附件1.txt", "前一行\n【待填 · D3】"),
        )
        assert c.原文出处 == ("正文初稿.txt 第1行", "附件1.txt 第2行"), c.原文出处
        assert c.待补 == ("D3 待填",), "待补仍按字段去重，不因出处而翻倍"

    def test_干净文本不产出处(self) -> None:
        c = _扫(("正文初稿.txt", "没有占位符"))
        assert c.可导出
        assert c.原文出处 == ()

    def test_空输入不崩(self) -> None:
        c = 扫描产出(())
        assert c.可导出 and c.原文出处 == ()

    def test_多个文件保序(self) -> None:
        """顺序必须稳定——清单是用来照着做的。"""
        c = _扫(
            ("甲.txt", "【待填 · D1】"),
            ("乙.txt", "【待核实 · D7】"),
            ("丙.txt", "【待填 · D3】"),
        )
        assert [出.split()[0] for 出 in c.原文出处] == ["甲.txt", "乙.txt", "丙.txt"]


class Test拦截事件带出处:
    def test_载荷含原文出处(self) -> None:
        """字段得**走到事件里**，否则投影层照样看不到。"""
        c = _扫(("正文初稿.txt", "正文\n【待填 · D3】"))
        载荷 = 导出交付物.取拦截事件(c)
        assert 载荷 is not None
        assert 载荷.get("原文出处"), f"事件载荷里没有出处：{载荷}"

    def test_干净时不发拦截(self) -> None:
        assert 导出交付物.取拦截事件(_扫(("正文初稿.txt", "干净"))) is None

    def test_待补与出处都在载荷里(self) -> None:
        c = _扫(("正文初稿.txt", "【待填 · D3】"))
        载荷 = 导出交付物.取拦截事件(c)
        assert 载荷 is not None
        assert 载荷.get("placeholders") == ("D3 待填",)
        assert 载荷.get("原文出处") == ("正文初稿.txt 第1行",)


class Test标准模式显示出处:
    def test_界面出现文件名与行号(self) -> None:
        """判据断言的是**投影实际产出的文字**，不是载荷字典——
        载荷带了而界面不显示，等于没修（D-233 同型）。"""
        from zhengtong.kernel.projection import StandardProjection

        p = StandardProjection()
        p.accept(_拦截事件(_扫(("正文初稿.txt", "第一行\n【待填 · D3】"))))
        行 = "\n".join(p.lines())
        assert "正文初稿.txt" in 行, f"界面看不到出处：{行}"
        assert "2" in 行, f"界面看不到行号：{行}"

    def test_出处不挤占待补那行(self) -> None:
        """出处**单独一行**——用户要求界面不要占太大空间（D-038）。"""
        from zhengtong.kernel.projection import StandardProjection

        p = StandardProjection()
        p.accept(_拦截事件(_扫(("正文初稿.txt", "【待填 · D3】"))))
        行们 = p.lines()
        含出处 = [行 for 行 in 行们 if "在这些位置" in 行]
        含待补 = [行 for 行 in 行们 if "待补信息" in 行]
        assert 含出处, f"没有出处那一行：{行们}"
        assert 含待补, f"没有待补那一行：{行们}"
        assert 含出处[0] != 含待补[0], "出处与待补挤在同一行了"


def _拦截事件(c: 扫描结论):
    """造一条 `export.blocked`。

    `取拦截事件` 返回 `dict | None`（干净时不发拦截），
    这里显式收窄并断言非空——**不用 `# type: ignore`**（D-018）。
    """
    from zhengtong.domain.event import Event, EventKind, Level
    from zhengtong.domain.ids import RunId

    载荷 = 导出交付物.取拦截事件(c)
    assert 载荷 is not None, "有占位符却没发出拦截事件"
    return Event(
        seq=1,
        kind=EventKind.EXPORT_BLOCKED,
        run_id=RunId("r-1"),
        phase="DONE",
        level=Level.INFO,
        data=载荷,
    )
