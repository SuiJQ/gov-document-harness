"""真实密钥源（`adapters/local_secrets.py`）。

## 位置：`system/config.toml` 里的 `llm.api_key`

`SecretsPort` 的**真适配器**。此前只有 `FakeSecrets`，
本文件补上生产路径——第 6 步接真端点时它才是唯一密钥出口。

## 三条纪律

### 一、构造时读一次，之后走快照

每次 `get_api_key()` 都读盘，会让「同一次运行内密钥不变」失效：
用户中途改了配置，正在跑的这一轮行为会突变，
而日志里记的还是旧密钥的请求结果。故构造时读，定为 frozen。

### 二、脱敏片段 ≠ 完整密钥（D-030）

`redaction_patterns()` 给的是「**怎么脱**」而不是「脱什么」——
取前缀 8 字符与后 4 字符，足以让脱敏器两段匹配上，
又**不足以还原完整密钥**。

若这里返回全文，`Redactor` 就成了第二个密钥持有者：
它自己被异常栈或调试投影记录时，密钥照样出去，
「唯一持有点」当场作废。这条有断言守着（`test_脱敏片段不含完整密钥`）。

### 三、没填就返回空串，不抛异常，也不假装有

首次启动本就没有配置。抛异常会让用户看到栈回溯；
返回非空占位会让请求带着假密钥发出去，换来一个语焉不详的 401。
**判空的责任在调用方**——端口给的是事实，不是判断。
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import load
from .paths import Workspace

#: 脱敏片段的长度。前缀够长以免误伤太少，后缀够长以免漏脱敏。
#: 8 + 4 是权衡：短到无法还原，长到能覆盖绝大多数 `sk-` 形态密钥。
_前缀长度 = 8
_后缀长度 = 4

#: 短于此长度的密钥不给片段——碎片太短会大面积误伤正常文本，
#: 那比不脱更糟（把日志搞得没法看，真泄密反而被淹没）。
_最短可脱长度 = 12


@dataclass(frozen=True, slots=True, repr=False)
class LocalSecrets:
    """从 `system/config.toml` 读密钥。

    `frozen` + 构造时读取 = 快照语义（见模块文档纪律一）。

    ## `repr=False` 不是洁癖

    测试实测：默认 dataclass `repr` 会把字段值原样打出来，
    于是 `print(sec)` 或调试器里展开对象，**密钥直接出现在屏幕上**。
    端口的 repr 默认是安全的（只给地址），dataclass 恰好破坏了这一点。
    故显式关掉——代价是调试时看不到字段，
    这个代价比泄密钥小得多。
    """

    _key: str = ""

    @classmethod
    def from_workspace(cls, ws: Workspace) -> LocalSecrets:
        """从工作区读。**配置损坏不抛异常**——
        与 `config.load` 的口径一致：读不出来当「没填」，
        真正的问题由 `Config.validate` 报给用户。"""
        got = load(ws)
        key = got.unwrap().api_key if got.is_ok else ""
        return cls(_key=key.strip())

    def get_api_key(self) -> str:
        """取明文。**仅供构造请求时使用**（D-030）。"""
        return self._key

    def redaction_patterns(self) -> tuple[str, ...]:
        """需要脱敏的字面量——**片段，不是全文**（见模块文档纪律二）。

        纯空白视为未填：返回空元组而不是 `("",)`，
        否则脱敏器会去匹配空串，把日志里所有空位都替换掉。
        """
        k = self._key
        if len(k) < _最短可脱长度:
            return ()
        return (k[:_前缀长度], k[-_后缀长度:])
