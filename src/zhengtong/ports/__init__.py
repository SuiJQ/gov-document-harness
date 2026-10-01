"""L3 端口层：纯抽象，零实现、零第三方类型。

**这条层的全部意义是隔离**：`services` 与 `orchestration` 只认这里的
抽象签名，不认 httpx / textual / pathlib 的具体类型。
换掉整个 LLM 客户端，不该让上层任何一行代码改动。

因此铁律：**端口签名里不得出现任何第三方类型。**
一旦某个签名写成 `-> httpx.Response`，隔离就已经漏了，
而且这种泄漏通常发生在"就差这一个类型"的时候。
检查由 `tests/test_ports_purity.py` 机械把关。
"""

from __future__ import annotations
