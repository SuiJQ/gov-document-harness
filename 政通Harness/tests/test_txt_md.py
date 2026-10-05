"""txt/md 读取器的契约测试（第 7 步）：编码探测与 2 万字截断。

D-032 定 txt/md 走标准库 + 编码自动探测（UTF-8 → GB18030 → 回退）；
D-053 定单文件 2 万字上限，截断时事件标明"已截断"。
"""

from __future__ import annotations

from pathlib import Path

from zhengtong.adapters.userfiles.readers.txt_md import (
    MAX_CHARS,
    read_txt_md,
)
from zhengtong.domain.errors import ErrorCode

# ═══ 一、编码探测 ═══


def test_读utf8(tmp_path: Path) -> None:
    p = tmp_path / "u.txt"
    p.write_text("这是UTF-8内容，标点：，。", encoding="utf-8")
    got = read_txt_md(p)
    assert got.is_ok, got.error
    assert got.unwrap().text == "这是UTF-8内容，标点：，。", "中文乱码或标点丢失"


def test_读gb18030(tmp_path: Path) -> None:
    """GB18030 是国产环境文档的实际编码（用户 Windows 机器上的公文多是它）。"""
    p = tmp_path / "g.txt"
    p.write_bytes("这是GB18030内容。".encode("gb18030"))
    got = read_txt_md(p)
    assert got.is_ok, got.error
    assert "GB18030内容" in got.unwrap().text, f"GB18030 乱码：{got.unwrap()!r}"


def test_带bom的utf8不乱码(tmp_path: Path) -> None:
    """BOM 若不处理会变成正文第一个字符（`\\ufeff`），
    混进公文里就是脏字符。"""
    p = tmp_path / "bom.txt"
    p.write_bytes("﻿标题内容".encode())
    got = read_txt_md(p)
    assert got.is_ok, got.error
    assert not got.unwrap().text.startswith("﻿"), "BOM 混进了正文"


def test_空文件返回空字符串而非报错(tmp_path: Path) -> None:
    """空文件是合法输入（用户放了个空占位），返回空串而非 fail。"""
    p = tmp_path / "empty.txt"
    p.write_text("", encoding="utf-8")
    got = read_txt_md(p)
    assert got.is_ok, got.error
    assert got.unwrap().text == ""


# ═══ 二、2 万字截断（D-053）═══


def test_超限被截断(tmp_path: Path) -> None:
    p = tmp_path / "big.txt"
    p.write_text("字" * (MAX_CHARS + 500), encoding="utf-8")
    got = read_txt_md(p)
    assert got.is_ok, got.error
    r = got.unwrap()
    assert len(r.text) == MAX_CHARS, f"截断后长度应是 {MAX_CHARS}，实为 {len(r.text)}"


def test_恰好等于上限不截断(tmp_path: Path) -> None:
    """**边界值**——`>` 还是 `>=` 的 off-by-one 就在这。

    写成 `>=` 会把恰好两万字（虽罕见但合法）的文件也砍掉 1 字。
    """
    p = tmp_path / "edge.txt"
    p.write_text("字" * MAX_CHARS, encoding="utf-8")
    got = read_txt_md(p)
    assert got.is_ok
    assert len(got.unwrap().text) == MAX_CHARS, "边界值被误截断了"


def test_截断保留前缀(tmp_path: Path) -> None:
    """截断必须保留**开头**——公文的价值在开头（标题、发号、称谓）。"""
    p = tmp_path / "head.txt"
    p.write_text("标题：关于印发通知\n" + "字" * (MAX_CHARS + 100), encoding="utf-8")
    got = read_txt_md(p)
    assert got.is_ok
    assert got.unwrap().text.startswith("标题：关于印发通知"), "截断丢了开头"


# ═══ 三、异常输入（D-108：返回 fail 不抛异常）═══


def test_不存在的文件返回fail(tmp_path: Path) -> None:
    got = read_txt_md(tmp_path / "nope.txt")
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.FILE_UNREADABLE


def test_是目录返回fail(tmp_path: Path) -> None:
    d = tmp_path / "adir"
    d.mkdir()
    got = read_txt_md(d)
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.FILE_UNREADABLE


def test_二进制垃圾不崩(tmp_path: Path) -> None:
    """随机字节既不是 UTF-8 也不是 GB18030——
    必须回退处理，不能抛 UnicodeDecodeError 穿透（D-108）。"""
    p = tmp_path / "bin.txt"
    p.write_bytes(bytes(range(256)) * 10)
    got = read_txt_md(p)
    assert got.is_error or got.unwrap().已截断 is False, "返回结构不对"


def test_不存在的后缀不被自动识别(tmp_path: Path) -> None:
    """`.pdf` / `.doc` / 图片**不解析**（D-032 用户砍除）——
    reader 只认 txt/md，别的后缀交给上层只登记不读。"""
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    got = read_txt_md(p)
    assert got.is_error, "pdf 被当成文本读了"
    assert got.error is not None
    assert got.error.code is ErrorCode.FILE_UNREADABLE
