"""pytest 共享装置。

`tests/` **不是包**（无 `__init__.py`），故共享的样本工厂
只能靠 `tests/` 在 `sys.path` 上被发现——这就是
`from fixtures_factories import ...` 的来源。

**不要为了消掉 basedpyright 的「隐式相对导入」告警给 `tests/` 加
`__init__.py`**：试过，会改变 pytest 的收集语义，当场打挂 9 条既有测试。
**该改的是告警，不是包结构。**
"""
