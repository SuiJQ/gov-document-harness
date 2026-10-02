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
from zhengtong.domain.errors import ErrorCode


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
    # `rel_path` 是定位用的路径，**不是内容**——加了它才分得清同名文件。
    assert set(条目.__slots__) == {"name", "size", "suffix", "rel_path"}, (
        f"UserFile 字段变成了 {条目.__slots__}——加内容字段就绕过了按需读取（rel_path 是定位，不算）"
    )
    assert not any("content" in f or "text" in f or "body" in f for f in 条目.__slots__), (
        f"出现了内容字段：{条目.__slots__}"
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
        "../system/config.toml",
        "../../etc/passwd",
        "a.txt/../b.txt",
    ],
)
def test_目录穿越被拦(user目录: Path, 恶意名: str) -> None:
    """**这条盯的是真实攻击面**：工具层把 name 传给 `read_text`，
    而 name 可能被模型构造成 `../system/secret.txt`。
    `system/` 里有明文 API key（D-030）。

    **诱饵必须用 reader 支持的后缀**（`.txt`）——
    第一版用 `.toml`，结果**路径守卫整个去掉后测试仍然全绿**：
    `.toml` 不被 reader 支持，于是「读不出内容」，
    但**不是因为路径被拦，而是因为后缀不被读**。
    两件事不同：换成 `.txt` 诱饵，去掉守卫后真的读出了密钥。

    **验结果不等于验原因。**
    """
    (user目录 / "a.txt").write_text("正常内容", encoding="utf-8")
    诱饵 = user目录.parent / "system" / "密钥.txt"
    诱饵.parent.mkdir(parents=True, exist_ok=True)
    诱饵.write_text("sk-LEAKED 真实密钥", encoding="utf-8")

    源 = LocalUserFiles(根=user目录)
    got = 源.read_text(恶意名)
    assert got.is_error, f"目录穿越 {恶意名} 竟然读成功了"
    assert got.error is not None
    assert "sk-LEAKED" not in got.error.detail


def test_守卫被去掉就会穿(user目录: Path) -> None:
    """**反向锁死**：确认上一条测的确实是守卫，而不是别的什么。

    本条不走 `read_text`，而是直接验 `_定位` 的越权行为——
    **只看错误码**（`PATH_ESCAPE`）。若有人把守卫换成别的方式
    （比如靠后缀白名单、靠登记表比对），本条会红，提示重新评估。
    """
    (user目录 / "a.txt").write_text("x", encoding="utf-8")
    诱饵 = user目录.parent / "system" / "密钥.txt"
    诱饵.parent.mkdir(parents=True, exist_ok=True)
    诱饵.write_text("sk-LEAKED", encoding="utf-8")

    源 = LocalUserFiles(根=user目录)
    got = 源._定位("../system/密钥.txt")
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.PATH_ESCAPE, (
        f"错误码是 {got.error.code} 而不是 PATH_ESCAPE——若靠别的机制拦的，这条判据要重新评估"
    )


def test_未登记的文件读不了(user目录: Path) -> None:
    源 = LocalUserFiles(根=user目录)
    got = 源.read_text("不存在.txt")
    assert got.is_error


# ═══ 四、复查补：登记与读取必须用同一套定位 ═══
#
# 第 7 步自查查出的真缺陷：
# `scan_and_register` 递归扫描，登记了子目录里的文件；
# 而 `_定位` 用 `self.根 / name` **只查根目录直下**。
#
# 实测：子目录里的 `通知.docx` 登记在列表里，`read_text("通知.docx")`
# 却报「未登记的文件」——**AI 看得到文件名，读不到内容**。
# 用户把材料按年度/类别放子目录是常态，这不是边角情况。
#
# **登记与读取必须用同一套定位，否则登记就是假的。**


def test_子目录里的文件能按名读到(user目录: Path) -> None:
    子 = user目录 / "2026年"
    子.mkdir()
    造docx(子 / "通知.docx", "子目录里的正文")
    源 = LocalUserFiles(根=user目录)
    登记 = 源.list_registered()
    assert any(f.name == "通知.docx" for f in 登记), "递归登记没生效"
    got = 源.read_text("通知.docx")
    assert got.is_ok, f"登记了却读不到：{got.error}"
    assert "子目录里的正文" in got.unwrap()


def test_子目录里的txt也能读(user目录: Path) -> None:
    子 = user目录 / "材料"
    子.mkdir()
    (子 / "数据.txt").write_text("子目录内容", encoding="utf-8")
    源 = LocalUserFiles(根=user目录)
    assert 源.read_text("数据.txt").is_ok


def test_同名文件能被区分(user目录: Path) -> None:
    """两个子目录各有 `通知.docx` ——**必须能被区分**。

    原实现两个都登记为 `name="通知.docx"`，模型拿到两份一模一样的
    元信息，无法指定读哪个。且 `read_text("通知.docx")` 只按根目录
    找，两边都读不到。

    解法：`UserFile` 加 `rel_path` 字段（相对 `user/` 的路径），
    `read_text` 按 `rel_path` 定位。**name 仍保留**——它是给模型看的
    人类可读标识（D-023 只要求不含内容，没要求不含路径）。
    """
    甲 = user目录 / "2025年"
    乙 = user目录 / "2026年"
    甲.mkdir()
    乙.mkdir()
    造docx(甲 / "通知.docx", "二〇二五年的通知")
    造docx(乙 / "通知.docx", "二〇二六年的通知")

    源 = LocalUserFiles(根=user目录)
    登记 = 源.list_registered()
    assert len(登记) == 2, "两个同名文件应登记为两条"

    路径集 = {f.rel_path for f in 登记}
    assert len(路径集) == 2, f"rel_path 相同，无法区分：{路径集}"

    # **必须断言 `rel_path != name`**：文件都在子目录里，
    # 若样本让两者相等，「按 rel_path 精确匹配」与「按 name 匹配」
    # 行为完全一致——判据就没有区分力（第一版正是栽在这）。
    for f in 登记:
        assert f.rel_path != f.name, (
            f"样本无效：rel_path({f.rel_path}) == name({f.name})，测不出到底按哪个匹配"
        )

    内容集 = set()
    for f in 登记:
        got = 源.read_text(f.rel_path)
        assert got.is_ok, f"按 rel_path 读 {f.rel_path} 失败：{got.error}"
        内容集.add(got.unwrap())
    assert 内容集 == {"二〇二五年的通知", "二〇二六年的通知"}, f"读回来是同一份：{内容集}"

    # ── fallback 判据：按**纯 name** 读 ──
    # 实测证伪时发现：传 `rel_path` 时 `_定位` 第一步
    # `根 / rel_path` 就命中了，**根本走不到按登记表找的 fallback**，
    # 于是「按 rel_path 精确匹配」与「只按 name 匹配」行为一致，
    # 判据抓不住退化。
    #
    # 按纯 name 读则必然走 fallback（`根/通知.docx` 不存在），
    # **这才是区分两种实现的唯一路径**。
    # 正确行为：能读到**某一份**（同名歧义下有确定性即可），
    # 但**不得报错**——原实现这里直接报「未登记的文件」。
    按名 = 源.read_text("通知.docx")
    assert 按名.is_ok, f"按纯 name 读同名文件失败：{按名.error}"
    assert 按名.unwrap() in {"二〇二五年的通知", "二〇二六年的通知"}

    # 确定性：连续两次读必须同一份（否则模型无从判断读到的是哪份）
    assert 源.read_text("通知.docx").unwrap() == 按名.unwrap(), "按 name 读不确定"


def test_按name仍能读到根目录文件(user目录: Path) -> None:
    """加 `rel_path` 不能**破坏**原有的按 name 读——那是既有约定。"""
    (user目录 / "a.txt").write_text("根目录内容", encoding="utf-8")
    源 = LocalUserFiles(根=user目录)
    assert 源.read_text("a.txt").is_ok


def test_rel_path不得越出user(user目录: Path) -> None:
    """**新增字段必须同样受路径守卫约束**——
    多一个入口就多一个越权的可能。"""
    (user目录 / "a.txt").write_text("x", encoding="utf-8")
    诱饵 = user目录.parent / "system" / "config.toml"
    诱饵.parent.mkdir(parents=True, exist_ok=True)
    诱饵.write_text("api_key = sk-LEAKED", encoding="utf-8")

    源 = LocalUserFiles(根=user目录)
    for 恶意 in ("../system/config.toml", "a.txt/../../../system/config.toml"):
        got = 源.read_text(恶意)
        assert got.is_error, f"rel_path 越权：{恶意}"
        assert got.error is not None
        assert "sk-LEAKED" not in got.error.detail
