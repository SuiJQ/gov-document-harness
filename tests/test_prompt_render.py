"""D-008「提示词可 diff」的判据。

## 一条判据，三件要验的事

1. **逐字节稳定**——同样输入必得同样输出（这是可 diff 的前提）
2. **不含时间戳 / 轮次号**——含了就毁掉 diff
3. **头部能定位差异来源**——角色、条目、占用、被截块
"""

from __future__ import annotations

import re

from zhengtong.adapters.fakes import FakeContentPack
from zhengtong.domain.dcard import DCard, DCardField, DCardFieldId
from zhengtong.domain.role import RoleId
from zhengtong.domain.tier import TierId
from zhengtong.services.prompting import (
    装配提示词,
    装配结果,
    装配请求,
    预算闸门,
)
from zhengtong.services.prompting.render import 渲染


def _装(*, 卡: DCard | None = None, 主体数: int = 0) -> 装配结果:
    """**参数逐个写死，不用 `**kw: object`**。

    那样写确实能过运行，但 BasedPyright 会把 `object` 往 `DCard` 上赋，
    报五个 error——而我第一版正是加了 `# type: ignore` 压过去。
    那正是 D-159 否掉的做法：**用 `object` 绕过类型检查，
    等于把该发现的问题挡在门外**。
    """
    return 装配提示词(
        请求=装配请求(角色=RoleId.TEAM_LEAD, 卡=卡, 主体数=主体数),
        内容包=FakeContentPack(),
        闸门=预算闸门(上限=100_000),
    )


class Test可diff:
    def test_逐字节稳定(self) -> None:
        卡 = DCard(by_id={DCardFieldId.D7: DCardField(id=DCardFieldId.D7, label="D7", value="x")})
        a = 渲染(_装(卡=卡), 档位=TierId.PRO).文本
        b = 渲染(_装(卡=卡), 档位=TierId.PRO).文本
        assert a == b

    def test_不含时间戳(self) -> None:
        文本 = 渲染(_装(), 档位=TierId.FLASH).文本
        # 常见时间戳形态：日期、时钟
        assert not re.search(r"\d{4}-\d{2}-\d{2}", 文本), "含日期——每次 diff 都全红"
        assert not re.search(r"\d{2}:\d{2}:\d{2}", 文本), "含时刻——每次 diff 都全红"
        assert "轮次" not in 文本, "含轮次号"

    def test_头部能定位差异(self) -> None:
        文本 = 渲染(_装(主体数=3), 档位=TierId.FLASH).文本
        assert "角色：team-lead" in 文本
        assert "档位：flash" in 文本
        assert "admin-ordering-rules" in 文本, "没记装了哪些条目"
        assert "占用：" in 文本
        assert "分块占用：" in 文本, "没分块——看不出是谁变了"

    def test_被截块要写进头部(self) -> None:
        装配 = 装配提示词(
            请求=装配请求(角色=RoleId.TEAM_LEAD, 主体数=3),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=300),
        )
        assert 装配.被截块
        assert "被截块：" in 渲染(装配, 档位=TierId.PRO).文本

    def test_正文原样落在末尾(self) -> None:
        装配 = _装()
        assert 装配.提示词正文 in 渲染(装配, 档位=TierId.PRO).文本
