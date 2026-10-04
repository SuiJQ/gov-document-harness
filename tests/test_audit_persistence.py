"""D-258：审计留痕必须真的落盘，且进程退出后仍在。

## 判据怎么设计的

不测「审计器记了几条」——那是内存形状，进程一结束就没了，
正是 D-258 病根所在。此处一律走**真实装配**（`build_app`），
读的是磁盘上真实存在的 `audit.jsonl`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zhengtong.app import build_app
from zhengtong.domain.role import RoleId


def _读(tmp: Path) -> list[dict[str, Any]]:
    行 = (tmp / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(x) for x in 行 if x.strip()]


def test_审计落盘且进程退出后仍在(tmp_path: Path) -> None:
    """核心判据：留痕不随进程消失。

    上下文管理器退出即代表「进程结束」，
    此前三条全靠内存，退出一句都读不到。
    """
    log = tmp_path / "journal.jsonl"
    with build_app(log_path=log) as app:
        _ = app.tools.调用("kb.search", 角色=RoleId.STRUCTURE, 参数={"查询": "公文"})

    审计 = tmp_path / "audit.jsonl"
    assert 审计.exists(), "审计文件必须落盘——否则进程一结束留痕全丢（D-258）"
    条目 = _读(tmp_path)
    assert len(条目) >= 1
    assert 条目[0]["工具名"] == "kb.search"


def test_拒绝也留痕且带裁决(tmp_path: Path) -> None:
    """越权/未知工具**最该被看见**，不能只在成功时留痕。"""
    log = tmp_path / "journal.jsonl"
    with build_app(log_path=log) as app:
        _ = app.tools.调用("不存在.工具", 角色=RoleId.STRUCTURE, 参数={})

    条目 = _读(tmp_path)
    assert any(e["允许"] is False for e in 条目), "拒绝必须留痕"
    拒 = next(e for e in 条目 if e["允许"] is False)
    assert 拒["裁决"], "拒绝必须带人可读的中文裁决，不能只有空串"
    assert 拒["角色"] == "structure"


def test_八个字段全部落盘不落空(tmp_path: Path) -> None:
    """D-258 点名「6 个字段全零消费」——先确认它们至少**被写下来**了。

    「被写入」和「被消费」是两回事：前者是留痕，后者是展示。
    此处只锁前者；后者由调试模式摘要那条判据负责。
    """
    log = tmp_path / "journal.jsonl"
    with build_app(log_path=log) as app:
        _ = app.tools.调用("kb.search", 角色=RoleId.STRUCTURE, 参数={"查询": "公文"})

    e = _读(tmp_path)[0]
    for 键 in ("工具名", "角色", "允许", "裁决", "时间戳", "参数摘要", "结果摘要", "错误码"):
        assert 键 in e, f"审计条目必须含字段 {键}"
    assert isinstance(e["时间戳"], (int, float)), "时间戳必须是数值，写进 JSON 后才能排序"
    assert isinstance(e["允许"], bool), "允许必须是布尔——字符串 'False' 在 JSON 里为真"


def test_默认不落盘时不报错(tmp_path: Path) -> None:
    """`log_path` 不给时不该凭空造文件，也不该抛。

    单元测试与库调用大量走无落盘路径，
    若无落盘也硬写文件，会在临时目录里堆出垃圾。
    """
    with build_app() as app:
        _ = app.tools.调用("kb.search", 角色=RoleId.STRUCTURE, 参数={"查询": "公文"})
    assert not (tmp_path / "audit.jsonl").exists()


def test_审计与事件是两份留痕不是一份(tmp_path: Path) -> None:
    """审计独立文件，**不混进 journal**。

    `JsonlJournal` 注明「单线程调用」，两个写入者交错追加会串行损坏。
    审计走工具层自己的同步路径，事件走泵线程，必须分开。
    """
    log = tmp_path / "journal.jsonl"
    with build_app(log_path=log) as app:
        _ = app.tools.调用("kb.search", 角色=RoleId.STRUCTURE, 参数={"查询": "公文"})

    if log.exists():
        for 行 in log.read_text(encoding="utf-8").splitlines():
            事件 = json.loads(行)
            assert "工具名" not in 事件, "审计字段不得混进 journal 事件——两套写入者会交错"
    assert (tmp_path / "audit.jsonl").exists()


def test_多个审计器实例不互相污染(tmp_path: Path) -> None:
    """落盘是追加语义：两次运行写同一文件，历史不该被第二次覆盖。"""
    log = tmp_path / "journal.jsonl"
    for _ in range(2):
        with build_app(log_path=log) as app:
            _ = app.tools.调用("kb.search", 角色=RoleId.STRUCTURE, 参数={"查询": "公文"})
    assert len(_读(tmp_path)) == 2, "第二次运行必须追加，不能截断第一次的留痕"


def test_三个查询接口有真实消费且走事件出口(tmp_path: Path) -> None:
    """D-258 的另一半：接口不能只是「定义了没人调」。

    判据读的是**收尾后的事件序列**——`条目`/`该角色的条目`/`拒绝数`
    三个接口都必须真的被 `App.close()` 用到，汇总成一条事件。
    少用一个，这条就红。
    """
    log = tmp_path / "journal.jsonl"
    with build_app(log_path=log) as app:
        _ = app.tools.调用("kb.search", 角色=RoleId.STRUCTURE, 参数={"查询": "公文"})
        _ = app.tools.调用("不存在.工具", 角色=RoleId.TEAM_LEAD, 参数={})

    摘 = [e for e in app.collected if e.kind.value == "run.audit_summary"]
    assert len(摘) == 1, f"收尾必须发一条审计摘要，实得 {len(摘)}"
    d = 摘[0].data
    # **从落盘文件实读真值来断言**，不硬编码数字。
    # 硬编码会在「某次调用成功了」时假红——判据要锁的是
    # 「事件里的数字来自真实审计记录」，不是「数字等于 2」。
    真 = _读(tmp_path)
    assert d["调用总数"] == len(真)
    assert d["拒绝数"] == sum(1 for e in 真 if not e["允许"]), "拒绝数必须与落盘记录一致"
    # `该角色的条目` 被消费：按角色分布必须与落盘记录逐角色吻合。
    # 显式转 `dict` 而不是 `cast`：这里真的要断言它是映射，
    # 转不过去就该红——`cast` 会把类型错误藏起来（D-001 类型纪律）。
    按角色 = d["按角色"]
    assert isinstance(按角色, dict), f"按角色必须是映射，实得 {type(按角色)}"
    for r in (RoleId.STRUCTURE, RoleId.TEAM_LEAD):
        assert 按角色[r.value] == sum(1 for e in 真 if e["角色"] == r.value)


def test_没有工具调用时不发空摘要(tmp_path: Path) -> None:
    """零调用不该发一条全是 0 的摘要——那是噪声，不是信息。"""
    log = tmp_path / "journal.jsonl"
    with build_app(log_path=log) as app:
        pass
    assert not [e for e in app.collected if e.kind.value == "run.audit_summary"]


def test_审计摘要只进调试不进标准(tmp_path: Path) -> None:
    """按 D-046：拒绝数属审核信息，标准模式不得显示。"""
    log = tmp_path / "journal.jsonl"
    with build_app(log_path=log) as app:
        _ = app.tools.调用("不存在.工具", 角色=RoleId.STRUCTURE, 参数={})

    调 = "\n".join(app.debug.lines())
    标 = "\n".join(app.standard.lines())
    assert "run.audit_summary" in 调 or "audit_summary" in 调, "调试模式必须能看到审计摘要（D-027）"
    assert "audit_summary" not in 标, "标准模式不得显示审核信息（D-046）"
