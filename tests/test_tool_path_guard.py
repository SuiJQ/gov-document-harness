"""**路径白名单**：双重白名单的第二重（D-195）。

## 为什么单独一个文件

第 10 步交付后核实发现：判据只测了**角色白名单**，
路径那一重全靠注释写「由 StoragePort 执行」——**一条测试都没有**。
而路径正是工具层最可能被绕过的攻击面：
模型完全可以要求 `kb.read_user_file` 去读 `../../system/config.toml`。

**实现碰巧是对的，和判据守住了，是两回事。**
实测当时确实四种越界写法全被拒——但那安全性写在第 7 步适配器的
`ensure_within` 里，它的测试测的是**直接调适配器**。
从**工具层**进去越界读，一条测试都没有：
哪天有人把 `kb_read_user_file` 改成 `Path(name).read_text()`，
不会有任何一条工具层测试变红。

## 本文件测的是「经过工具层」这条路径

刻意走 `工具运行器.调用(...)`（模型实际会走的路），
而不是直接调 `LocalUserFiles.read_text`。

## 真实威胁模型

`system/` 存配置与日志，**含 API key 的密文与片段**（D-030）。
AI 只能看到 `user/` 与 `data/`，不能看到或修改 `system/`。
故「读到 `system/config.toml`」不是理论问题。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zhengtong.adapters import paths
from zhengtong.adapters.fakes import (
    FakeContentPack,
    FakeRetriever,
    FakeValidators,
)
from zhengtong.adapters.fs_storage import FsStorage
from zhengtong.adapters.userfiles.scanner import LocalUserFiles
from zhengtong.domain.ids import RunId
from zhengtong.domain.role import RoleId
from zhengtong.services.tools import 建工具运行器
from zhengtong.services.tools.handlers import 工具依赖
from zhengtong.services.tools.runner import 工具运行器

_RUN = RunId("r-path")

#: 越界写法。**每一条都是模型可能真的生成出来的字符串**，
#: 不是凑数的边角——`..\` 那条尤其重要：Windows 上反斜杠同样是路径分隔符，
#: 只挡 `/` 的实现在 Windows 上会直接漏（目标是 Windows 11）。
越界写法: tuple[str, ...] = (
    "../../system/config.toml",
    "../../../system/secrets.json",
    "..\\system\\config.toml",
    "子目录\\..\\..\\system\\config.toml",
    "/etc/passwd",
    "通知.txt/../../system/config.toml",
)


def _环境(tmp_path: Path) -> tuple[工具运行器, Path]:
    """造一个**真实可越界**的环境：`system/` 里放真的敏感文件。"""
    建 = paths.ensure_dirs(tmp_path)
    assert 建.is_ok
    ws = 建.unwrap()
    系统 = ws.dir("system")
    系统.mkdir(parents=True, exist_ok=True)
    # 敏感内容必须**独特**，万一泄露进结果，断言能一眼看出
    (系统 / "config.toml").write_text("API_KEY=sk-绝密-勿外泄-12345", encoding="utf-8")
    用户 = ws.dir("user")
    用户.mkdir(parents=True, exist_ok=True)
    (用户 / "通知.txt").write_text("关于做好稳增长工作的通知", encoding="utf-8")
    return (
        建工具运行器(
            storage=FsStorage(ws=ws),
            user_files=LocalUserFiles(根=用户),
            retriever=FakeRetriever(),
            content_pack=FakeContentPack(),
            validators=FakeValidators(),
            run_id=_RUN,
            审计时钟=lambda: 0.0,
        ),
        系统,
    )


# ═══ 越界必须被拒 ═══


@pytest.mark.parametrize("文件名", 越界写法)
def test_越界读被拒(文件名: str, tmp_path: Path) -> None:
    """**本文件的核心判据**：走工具层进去也读不到 `system/`。"""
    跑, _ = _环境(tmp_path)
    r = 跑.调用("kb.read_user_file", 角色=RoleId.TEAM_LEAD, 参数={"filename": 文件名})
    assert r.is_error, f"{文件名!r} 竟被读到了——AI 能看到 system/ 即防线失守"


def test_越界内容绝不出现(tmp_path: Path) -> None:
    """比「报错」更强一重：断言**敏感串没进结果**。

    只断言 `is_error` 有一个漏洞：万一实现改成
    「读到了但截断后报错」，敏感内容可能已经进了返回值或日志。
    """
    跑, _ = _环境(tmp_path)
    for 文件名 in 越界写法:
        r = 跑.调用("kb.read_user_file", 角色=RoleId.TEAM_LEAD, 参数={"filename": 文件名})
        内容 = r.unwrap() if r.is_ok else ""
        assert "绝密" not in 内容, f"{文件名!r} 的返回里带了敏感内容"
        assert "sk-" not in 内容, f"{文件名!r} 的返回里带了密钥片段"


def test_越界必须留审计(tmp_path: Path) -> None:
    """越权尝试恰恰最该被看见（D-191）。
    路径越界与角色越权同属「不该发生却发生了」，不能静默。"""
    跑, _ = _环境(tmp_path)
    _ = 跑.调用(
        "kb.read_user_file",
        角色=RoleId.TEAM_LEAD,
        参数={
            "filename": "../../system/config.toml",
        },
    )
    条目 = 跑.审计.条目
    assert len(条目) == 1
    assert not 条目[0].允许
    assert "config.toml" not in 条目[0].参数摘要, "审计不该抄录用户给的路径内容"


def test_符号链接指向目录外被跳过(tmp_path: Path) -> None:
    """用户往 `user/` 放一个指向隐私目录的软链——
    登记时就该跳过（D-022 的边界：不跟出去）。"""
    建 = paths.ensure_dirs(tmp_path)
    assert 建.is_ok
    ws = 建.unwrap()
    密 = ws.dir("system")
    密.mkdir(parents=True, exist_ok=True)
    (密 / "config.toml").write_text("API_KEY=sk-绝密", encoding="utf-8")
    用户 = ws.dir("user")
    用户.mkdir(parents=True, exist_ok=True)
    链 = 用户 / "诱人的材料.txt"
    try:
        链.symlink_to(密 / "config.toml")
    except (OSError, NotImplementedError):  # pragma: no cover - 无权建链的环境
        pytest.skip("本环境不支持符号链接")
    uf = LocalUserFiles(根=用户)
    assert not any(f.name == "诱人的材料.txt" for f in uf.list_registered()), (
        "指向目录外的符号链接被登记了——登记就该拦住"
    )
    r = uf.read_text("诱人的材料.txt")
    assert r.is_error, "软链竟然能读出内容"


# ═══ 正常读仍要能通（防止「一律拒绝」也算绿）═══


def test_正常材料仍能读(tmp_path: Path) -> None:
    """**反向判据**：不能靠「什么都拒绝」把上面那些测绿。"""
    跑, _ = _环境(tmp_path)
    r = 跑.调用("kb.read_user_file", 角色=RoleId.TEAM_LEAD, 参数={"filename": "通知.txt"})
    assert r.is_ok, r.error
    assert "稳增长" in r.unwrap()


def test_子目录材料能读(tmp_path: Path) -> None:
    """用户按年度/类别放子目录是常态（第 7 步复查修过这个），
    路径白名单不能把合法子目录也拦掉。"""
    用户 = tmp_path / "user"
    用户.mkdir(parents=True, exist_ok=True)
    子 = 用户 / "2026年"
    子.mkdir(parents=True, exist_ok=True)
    (子 / "通知.txt").write_text("归档材料内容", encoding="utf-8")
    建 = paths.ensure_dirs(tmp_path)
    assert 建.is_ok
    跑 = 建工具运行器(
        storage=FsStorage(ws=建.unwrap()),
        user_files=LocalUserFiles(根=用户),
        retriever=FakeRetriever(),
        content_pack=FakeContentPack(),
        validators=FakeValidators(),
        run_id=_RUN,
        审计时钟=lambda: 0.0,
    )
    r = 跑.调用(
        "kb.read_user_file",
        角色=RoleId.TEAM_LEAD,
        参数={
            "filename": "2026年/通知.txt",
        },
    )
    assert r.is_ok, r.error
    assert "归档材料" in r.unwrap()


# ═══ 装配层同一道闸门（不经工具层也拦得住）═══


def _装好的App(tmp_path: Path):
    """落一份最小配置并装配出 App。

    `build_app_from_workspace` 要求 endpoint/model/api_key 齐全
    （缺一报 `CONFIG_INVALID`）——这是有意的：
    真实用户必须填，不该有「默认值兜底」。
    故测试里也得像真用户一样把三样填齐。
    """
    import dataclasses

    from zhengtong.adapters.config import Config, save
    from zhengtong.app import build_app_from_workspace

    根 = tmp_path / "ws"
    建 = paths.ensure_dirs(根)
    assert 建.is_ok
    ws = 建.unwrap()
    密 = ws.dir("system")
    密.mkdir(parents=True, exist_ok=True)
    (密 / "config.toml").write_text("API_KEY=sk-绝密-装配层", encoding="utf-8")
    用户 = ws.dir("user")
    用户.mkdir(parents=True, exist_ok=True)
    (用户 / "通知.txt").write_text("正常材料", encoding="utf-8")
    落配置 = save(
        ws,
        dataclasses.replace(
            Config(),
            endpoint="https://example.invalid/v1/chat/completions",
            model="some-model",
            api_key="sk-test",
            context_k=128,
        ),
    )
    assert 落配置.is_ok, 落配置.error
    结果 = build_app_from_workspace(ws)
    assert 结果.is_ok, 结果.error
    return 结果.unwrap()


def test_app装配出的工具层同样拦得住(tmp_path: Path) -> None:
    """**接线判据的一部分**：从 `build_app_from_workspace` 出来的工具层，
    也必须带真适配器、真守卫。不能只在单元测试里对。

    这条正是 D-165 同型的定点守卫：真配置下拿到的必须是真工具层。
    """
    app = _装好的App(tmp_path)
    try:
        assert isinstance(app.tools, 工具运行器)
        依赖: 工具依赖 = app.tools._d  # pyright: ignore[reportPrivateUsage]
        assert isinstance(依赖.user_files, LocalUserFiles), (
            "装配层给工具层塞了假 user_files——D-165 同型"
        )
        assert isinstance(依赖.storage, FsStorage), "装配层给工具层塞了假 storage——D-165 同型"
        越界 = app.tools.调用(
            "kb.read_user_file",
            角色=RoleId.TEAM_LEAD,
            参数={
                "filename": "../../system/config.toml",
            },
        )
        assert 越界.is_error, "装配出来的工具层竟能越界读 system/"
        正常 = app.tools.调用(
            "kb.read_user_file",
            角色=RoleId.TEAM_LEAD,
            参数={
                "filename": "通知.txt",
            },
        )
        assert 正常.is_ok, "装配出来的工具层连正常材料都读不到"
        assert "绝密" not in (正常.unwrap() if 正常.is_ok else "")
    finally:
        app.close()


def test_工具层与装配层用同一批端口(tmp_path: Path) -> None:
    """工具层不得自己 new 一份端口。

    否则会出现「装配层测的是真检索、工具层用的还是假检索」——
    同型盲区，且两边测试都全绿。"""
    app = _装好的App(tmp_path)
    try:
        依赖: 工具依赖 = app.tools._d  # pyright: ignore[reportPrivateUsage]
        assert 依赖.storage is app.ports.storage
        assert 依赖.user_files is app.ports.user_files
        assert 依赖.retriever is app.ports.retriever
        assert 依赖.content_pack is app.ports.content_pack
        assert 依赖.validators is app.ports.validators
        assert 依赖.run_id == app.run_id, (
            "工具层的 run_id 应与 App 一致，否则产物会落到别的运行目录"
        )
    finally:
        app.close()
