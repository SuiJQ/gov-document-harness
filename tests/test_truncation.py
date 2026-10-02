"""截断必须被**标明**，不能静默（D-053 全条）。

## 为什么这条要单独成文件

第 7 步只做了「截断」，没做架构要求的「**事件标明**」——
`read_text` 返回 `Result[str]`，**签名里根本没有承载「是否截断」的地方**。

后果不是「少个标记」那么轻：模型拿到 2 万字的残缺材料，
**却以为它是完整的**，会基于残缺事实做研判。
「严禁编造」是本项目最高数据纪律（D-007），
而这条路径正是它的反例。

## 顺带补上第二个缺口

`read_docx` **根本没有 2 万字上限**——架构 §6.3 说的是「单个文件」，
而实现里只有 `txt_md` 截断了。docx 经 zip bomb 防护后仍可解压到
20MB，其 XML 解析出的纯文本轻易超过 2 万字。
**读 txt 会截断、读 docx 不会**，同一个上限两种行为，是不设防。

## 值对象而非布尔返回

不返回 `(文本, 是否截断)` 元组，也不加第二个方法——
那两种都会让调用方**可能忘记看那个标记**。
返回一个自带 `已截断` 属性的值对象，配合第 10 步工具层统一处理。
"""

from __future__ import annotations

from pathlib import Path

from fixtures_factories import 造多段docx
from zhengtong.adapters.paths import ensure_dirs
from zhengtong.adapters.userfiles.readers.docx import MAX_CHARS as DOCX上限
from zhengtong.adapters.userfiles.readers.txt_md import MAX_CHARS as TXT上限
from zhengtong.adapters.userfiles.scanner import LocalUserFiles
from zhengtong.domain.errors import ErrorCode
from zhengtong.domain.task import 读到的文件

# ═══ 一、值对象自身 ═══


def test_未截断时标记为False() -> None:
    r = 读到的文件(text="短内容", 已截断=False, 完整字数=4, 原始字数=4)
    assert not r.已截断


def test_原始字数不得小于完整字数() -> None:
    """**截断只会让内容变少**。若原始字数小于完整字数，
    说明两者的含义搞反了——那会让模型以为「多出来的是被截掉的」。
    """
    r = 读到的文件(text="长" * 100, 已截断=True, 完整字数=100, 原始字数=500)
    assert r.完整字数 <= r.原始字数


def test_值对象不可变() -> None:
    """领域值对象一律 frozen + slots（L0 判据：24 个 dataclass 全 frozen）。"""
    import dataclasses

    assert dataclasses.is_dataclass(读到的文件)
    r = 读到的文件(text="x", 已截断=False, 完整字数=1, 原始字数=1)
    # **不用 `r.text = "y"`**：那条写法会被两个检查器各拦一个
    # （ruff 报 B018 并自动改写；basedpyright 报只读），
    # 而**判据不该为了让工具安静而变形**——
    # 先前 `check.sh --fix` 就把 `setattr` 悄悄改回了赋值。
    #
    # 改测 `slots`：frozen dataclass 带 slots 时，
    # **底层没有 `__dict__`，往里塞字段必然失败**——
    # 与「赋值被拒」是同一个「不可变」性质的直接体现，
    # 且 `object.__setattr__` 是标准库惯用法，两个检查器都不报。
    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(r, "__dict__", {"text": "篡改"})
    # 且字段确实没被改掉
    assert r.text == "x", "字段值被改了"


import pytest  # noqa: E402  —— 故意放在中间：上面的 import 顺序无意义

# ═══ 二、txt/md 截断要标明 ═══


def test_超限的txt标明已截断(tmp_path: Path) -> None:
    ws = ensure_dirs(tmp_path / "ws").unwrap()
    u = ws.dir("user")
    (u / "长.txt").write_text("字" * (TXT上限 + 800), encoding="utf-8")
    源 = LocalUserFiles(根=u)
    got = 源.read_text("长.txt")
    assert got.is_ok, got.error
    r = got.unwrap()
    assert r.已截断, "截断了却没标明——模型会以为材料是完整的"
    assert r.完整字数 == TXT上限
    assert r.原始字数 == TXT上限 + 800
    assert len(r.text) == TXT上限


def test_未超限的txt标明未截断(tmp_path: Path) -> None:
    ws = ensure_dirs(tmp_path / "ws").unwrap()
    u = ws.dir("user")
    (u / "短.txt").write_text("字" * 100, encoding="utf-8")
    源 = LocalUserFiles(根=u)
    r = 源.read_text("短.txt").unwrap()
    assert not r.已截断, "没截断却标了截断"
    assert r.原始字数 == 100


def test_恰好等于上限标明未截断(tmp_path: Path) -> None:
    """**边界值**：恰好两万字是**完整**的，不该标截断。
    写成 `>=` 判截断会把这份合法文件误标成残缺。"""
    ws = ensure_dirs(tmp_path / "ws").unwrap()
    u = ws.dir("user")
    (u / "边界.txt").write_text("字" * TXT上限, encoding="utf-8")
    源 = LocalUserFiles(根=u)
    r = 源.read_text("边界.txt").unwrap()
    assert not r.已截断
    assert r.原始字数 == TXT上限


# ═══ 三、docx 也必须截断（第二个缺口）═══


def test_超限的docx也截断(tmp_path: Path) -> None:
    """**读 txt 会截断、读 docx 不会**——同一个上限两种行为，是不设防。"""
    造多段docx(tmp_path / "长.docx", 段数=4000, 每段字=20)
    ws = ensure_dirs(tmp_path / "ws").unwrap()
    u = ws.dir("user")
    造多段docx(u / "长.docx", 段数=4000, 每段字=20)
    源 = LocalUserFiles(根=u)
    r = 源.read_text("长.docx").unwrap()
    assert r.已截断, "docx 完全没做 2 万字截断"
    assert len(r.text) <= DOCX上限, f"截断后仍有 {len(r.text)} 字"
    assert r.原始字数 > DOCX上限


def test_普通docx标明未截断(tmp_path: Path) -> None:
    ws = ensure_dirs(tmp_path / "ws").unwrap()
    u = ws.dir("user")
    造多段docx(u / "短.docx", 段数=5, 每段字=10)
    源 = LocalUserFiles(根=u)
    r = 源.read_text("短.docx").unwrap()
    assert not r.已截断


def test_两个上限必须一致(tmp_path: Path) -> None:
    """**同一个 D-053，两种格式两个上限就是漏洞**——
    模型看到的是同一条「单文件 2 万字」，不是两条。"""
    assert DOCX上限 == TXT上限, f"docx={DOCX上限} txt={TXT上限}，同一决策不能两个数"


# ═══ 四、标明要能落到文字上，不能只靠程序判断 ═══


def test_有面向模型的说明文本(tmp_path: Path) -> None:
    """**标记只有变成文字才有用**——第 10 步工具层会把它拼进给模型的输出，
    但拼接的前提是这里提供了稳定的文案。

    **文案里必须含「已被截断」与「原文字数」**：
    模型需要知道①材料不完整 ②本可以看更多，才能主动追问或改用别的文件。
    只说「已截断」而不说原文字数，模型不知道缺了多少。
    """
    ws = ensure_dirs(tmp_path / "ws").unwrap()
    u = ws.dir("user")
    (u / "长.txt").write_text("字" * (TXT上限 + 100), encoding="utf-8")
    源 = LocalUserFiles(根=u)
    r = 源.read_text("长.txt").unwrap()
    文本 = r.给模型的提示()
    assert r.已截断
    assert "截断" in 文本
    assert str(TXT上限 + 100) in 文本, "没给出原文字数，模型不知道缺了多少"


def test_未截断时提示为空(tmp_path: Path) -> None:
    """**未截断时不能加任何噪声**——每份材料都挂一句
    「本文完整」既浪费 token，又会让模型对这句提示脱敏。"""
    ws = ensure_dirs(tmp_path / "ws").unwrap()
    u = ws.dir("user")
    (u / "短.txt").write_text("内容", encoding="utf-8")
    源 = LocalUserFiles(根=u)
    assert 源.read_text("短.txt").unwrap().给模型的提示() == ""


# ═══ 五、失败时不得返回半个值对象 ═══


def test_读失败不返回值对象(tmp_path: Path) -> None:
    ws = ensure_dirs(tmp_path / "ws").unwrap()
    源 = LocalUserFiles(根=ws.dir("user"))
    got = 源.read_text("不存在.txt")
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.FILE_UNREADABLE
