"""门控记忆门（D-254）。

**不是压缩，是导航。零阈值，始终在。**

它不丢任何东西，不替代任何压缩层级，只是给模型一个
「读下面的历史时优先看这些」的标记。压缩器只动
`recent_window`，够不着这一栏——所以它永远完整。

## 为什么单独成模块而不是塞进 compactor

- `compactor` 的职责是「丢」，`memory_gate` 的职责是「标」。
  两者方向相反，放一起必然互相污染。
- `memory_gate` 的写入侧可能来自程序（阶段切换、成员汇报），
  需要独立的状态持有者，不能跟着 `ContextView` 的快照走。
- `ContextView.记忆门` 只存**压缩时的快照**——可变状态在本类里。

## 模型侧写入暂不接工具

14 个工具是硬约束（`test_工具恰为十四个` 钉死），
模型推送属 speculativ 且无消费侧，接了就是负收益。
"""

from collections import deque


class 记忆门:
    """门控记忆门。**线程不安全**——编排层是单线程，不加锁。

    硬锚点（程序写入）永远在最前面且永不被挤出；
    模型推送（暂未接工具）挤最早的。
    """

    def __init__(self, 上限: int = 20) -> None:
        self._缓冲: deque[tuple[str, str]] = deque(maxlen=上限)
        self._上限 = 上限

    def 程序写入(self, 内容: str, 标签: str = "硬锚点") -> None:
        """程序侧写入。**永不被挤出**。

        `deque` 的 `maxlen` 只挤两侧，`appendleft` 让硬锚点
        永远在最前面——后面挤掉的不会影响它。
        """
        if not 内容.strip():
            return
        # 去重：同标签同内容不重复写
        for 现标签, 现内容 in self._缓冲:
            if 现标签 == 标签 and 现内容 == 内容:
                return
        self._缓冲.appendleft((标签, 内容))

    def 注入(self) -> tuple[tuple[str, str], ...]:
        """返回当前快照。**顺序有意义**：硬锚点在前，模型推送在后。"""
        return tuple(self._缓冲)

    def 清空(self) -> None:
        self._缓冲.clear()

    def __len__(self) -> int:
        return len(self._缓冲)
