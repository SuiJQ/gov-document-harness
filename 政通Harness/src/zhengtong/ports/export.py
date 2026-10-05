"""导出端口（L3）。

## 为什么要单独立一个端口

`services/export.py` 里的 `导出交付物` **原先在生产路径零调用**——
全仓只有测试实例化它。那意味着「导出前必须扫描残留占位符」
这条用户硬约束**没有代码在执行**（D-296）。

要把它接上，就得回答一个问题：编排层怎么拿到它。
直接 import 具体类会让 L5 依赖 L4 的**实现**，
所以这里只声明形状。

## 形状刻意窄

编排层要的只有一件事：**把交付物落盘，或明确拒绝**。
所以端口只有一个方法，`Result` 里既有成败也有文件名——
调用方需要知道「到底写了哪几个」才能报给用户，
不能只拿到一个 `None`（那就是 D-283「成败要用值表达」的病根）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ..domain.artifact import ArtifactKind
from ..domain.errors import ErrorCode, Result


class 待落盘(Protocol):
    """一份待落盘的交付物。

    **故意不复用 `services.export.导出文件`**：
    端口不能依赖适配器与服务层的具体类（D-070）。
    结构相同即可——`Protocol` 只按形状匹配。
    """

    @property
    def 文件名(self) -> str: ...

    @property
    def 内容(self) -> str: ...

    @property
    def 种类(self) -> ArtifactKind: ...


@dataclass(frozen=True, slots=True)
class 落盘摘要:
    """一份已落盘交付物的摘要：`artifact.published` 事件的载荷（D-072）。

    ## 为什么不能只返回文件名

    架构 §5.4 规定 `artifact.published` 携 `name` / `kind` / **`hash`**，
    且明说调试模式拿它与 `phase.artifact_ready` 对照，
    「检查两者内容一致（防『界面显示的和落盘的不是一份』这类最难查的错）」。

    只返回文件名，这个对照**做不了**——而那正是本类型存在的理由。

    ## hash 必须由写入侧算

    编排层拿着的是 `内容: str`，而落盘走的是 `编码`（用户可配 `gb18030`）。
    同一串字符按 UTF-8 与 GB18030 写出的**字节完全不同**，
    hash 也完全不同。所以 hash 只能由**真正写文件的那一层**算，
    编排在旁边重算等于假设「编码没变」——而那个假设正是要防的。

    `hash` 是**写入字节**的 sha256（十六进制），不是内容字符串的 sha256。
    """

    文件名: str
    种类: ArtifactKind
    #: 落盘字节的 sha256。可用 `hashlib.sha256(路径.read_bytes()).hexdigest()` 复算。
    hash: str


class ExportPort(Protocol):
    """导出端口。**只有「落盘或拒绝」一件事。**"""

    def __call__(self, *, 文件: Sequence[待落盘]) -> Result[tuple[落盘摘要, ...]]:
        """扫 → 判 → 落盘。**任一步不过就不写任何文件。**

        返回值是**落盘摘要**而非文件名：调用方要发
        `artifact.published`，需要 `kind` 与 `hash`。
        """
        ...


class 未接线导出:
    """没装导出器时用这个占位。

    ## 必须是 fail，不能是 ok

    静默成功过一次——那正是 D-192「未接线的编排回调必须失败，
    不得静默成功」记的原委：回调没接线却返回成功，
    上层以为导出完成了，用户在目录里找不到文件却什么都没报错。
    """

    def __call__(self, *, 文件: Sequence[待落盘]) -> Result[tuple[落盘摘要, ...]]:
        #  用不到，但**签名必须与 ExportPort 一致**——
        # 落不到地的占位也得是个合法的实现，否则接不上（D-192）。
        del 文件
        return Result.fail(
            ErrorCode.UNSOURCED_ASSERTION,
            "导出器未接线：最终交付物没有落盘",
        )
