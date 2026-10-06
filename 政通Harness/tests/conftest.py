"""pytest 共享装置。

`tests/` **是包**（有 `__init__.py`），测试之间互相引用靠包导入。
共享的样本工厂（如 `fixtures_factories`）因此被正确发现。

**已确认**：给 `tests/` 加 `__init__.py` 不会改变 pytest 的收集语义，
2175 条测试全绿，且解决了 `from tests.test_seams_round1 import ...` 的
导入问题。基于pyright 也不报「隐式相对导入」告警。
"""
