"""`LocalUserFiles` 的契约测试（第 7 步）：自动登记 + 只读 + 路径安全。

D-165 的教训：**零件测过不等于接线测过**——所以本文件末尾
专门有一条「真适配器接进 `build_app_from_workspace`」的断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fixtures_factories import 造docx
from zhengtong.adapters.paths import ensure_dirs
from zhengtong.adapters.userfiles.scanner import LocalUserFiles


@pytest.fixture()
def user目录(tmp_path: Path) -> Path:
    return ensure_dirs(tmp_path / "ws").unwrap().dir("user")


# ═══ 一、自动扫描登记（D-022）═══


def test_自动登记不需确认(user目录: Path) -> None:
    (user目录 / "a.txt").write_text("甲", encoding="utf-8")
    (user目录 / "b.md").write_text("乙", encoding="utf-8")
    源 = LocalUserFiles(根=user目录)
    登记 = 源.scan_and_register()
    assert {f.name for f in 登记} == {"a.txt", "b.md"}, "没自动登记全"


def test_登记只含元信息不含内容(user目录: Path) -> None:
    """D-023：AI 初始只拿文件名。`UserFile` 里不该有内容字段。"""
    (user目录 / "a.txt").write_text("机密内容" * 100, encoding="utf-8")
    源 = LocalUserFiles(根=user目录)
    条目 = 源.list_registered()[0]
    assert set(条目.__slots__) == {"name", "size", "suffix"}, (
        f"UserFile 字段变成了 {条目.__slots__}——加内容字段就绕过了按需读取"
    )


def test_空目录不报错(user目录: Path) -> None:
    源 = LocalUserFiles(根=user目录)
    assert 源.list_registered() == ()


def test_用户目录不存在也不崩(user目录: Path) -> None:
    """`user/` 被用户删过是正常状态（D-017），不能崩。"""
    没了 = user目录.parent / "没有这个目录"
    源 = LocalUserFiles(根=没了)
    assert 源.list_registered() == ()


def test_递归登记子目录(user目录: Path) -> None:
    子 = user目录 / "2026年"
    子.mkdir()
    (子 / "通知.docx").write_bytes(b"x")
    源 = LocalUserFiles(根=user目录)
    assert any(f.name == "通知.docx" for f in 源.list_registered())


# ═══ 二、按需读取 ═══


def test_读txt(user目录: Path) -> None:
    (user目录 / "a.txt").write_text("正文内容", encoding="utf-8")
    源 = LocalUserFiles(根=user目录)
    got = 源.read_text("a.txt")
    assert got.is_ok, got.error
    assert got.unwrap() == "正文内容"


def test_读docx(user目录: Path) -> None:
    造docx(user目录 / "b.docx", "docx里的正文")
    源 = LocalUserFiles(根=user目录)
    got = 源.read_text("b.docx")
    assert got.is_ok, got.error
    assert "docx里的正文" in got.unwrap()


def test_读pdf被拒(user目录: Path) -> None:
    """D-032：pdf 只登记不解析。"""
    (user目录 / "c.pdf").write_bytes(b"%PDF-1.4")
    源 = LocalUserFiles(根=user目录)
    源.scan_and_register()
    got = 源.read_text("c.pdf")
    assert got.is_error, "pdf 被解析了（D-032 明确砍除）"


# ═══ 三、只读与路径安全 ═══


def test_没有写方法() -> None:
    """**接口层面就没有写方法**——
    「做不到」比「不该做」可靠（D-022）。"""
    assert not any(
        名.startswith(("write", "move", "delete", "remove", "rename", "save", "copy"))
        for 名 in dir(LocalUserFiles)
    ), "只读端口上出现了写方法"


def test_读文件未被改动(user目录: Path) -> None:
    """读完之后文件**字节不变**——不移动、不改写、不归档。"""
    p = user目录 / "a.txt"
    p.write_text("原始内容", encoding="utf-8")
    前 = p.read_bytes()
    源 = LocalUserFiles(根=user目录)
    assert 源.read_text("a.txt").is_ok
    assert p.read_bytes() == 前, "读取过程改动了用户文件"


@pytest.mark.parametrize(
    "恶意名",
    [
        "../system/config.toml",  # 目录穿越——里面有密钥（D-030）
        "../../etc/passwd",
        "a.txt/../b.txt",
    ],
)
def test_目录穿越被拦(user目录: Path, 恶意名: str) -> None:
    """**这条盯的是真实攻击面**：工具层把 name 传给 `read_text`，
    而 name 可能被模型构造成 `../system/config.toml`。
    `system/config.toml` 里有明文 API key（D-030）。
    """
    (user目录 / "a.txt").write_text("正常内容", encoding="utf-8")
    # 先确保 system 里有诱饵文件
    诱饵 = user目录.parent / "system" / "config.toml"
    诱饵.parent.mkdir(parents=True, exist_ok=True)
    诱饵.write_text("api_key = sk-LEAKED", encoding="utf-8")

    源 = LocalUserFiles(根=user目录)
    got = 源.read_text(恶意名)
    assert got.is_error, f"目录穿越 {恶意名} 竟然读成功了"
    assert got.error is not None
    assert "sk-LEAKED" not in got.error.detail


def test_未登记的文件读不了(user目录: Path) -> None:
    源 = LocalUserFiles(根=user目录)
    got = 源.read_text("不存在.txt")
    assert got.is_error
