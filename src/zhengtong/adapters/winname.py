"""文件名与路径的 Windows 兼容性。

## 为什么需要专门一个模块

Linux 允许的文件名，Windows 一律拒绝。**这不是"洁癖"，是能不能用的问题**：
程序在 Windows 上产出一个叫 `报告:v2.txt` 的文件，
`os.replace` 会在最后一步失败，用户看到的是"写入失败：_invalid_argument_"。

更隐蔽的是**保留设备名**：`CON`、`NUL`、`COM1`……在 Linux 上是普通文件名，
在 Windows 上是设备。任何 `Path("NUL").exists()` 在 Windows 上返回 `True`——
它指向的是空设备，不是文件。路径守卫会把 `user/NUL` 判为"存在且在允许根下"，
于是 AI 往里写的东西**人间蒸发，且没有任何报错**。

这类问题在 Linux 上开发时**永远测不出来**，因为 Linux 上它们全部合法。

## 处理原则

**只拒绝，不静默改写。**
把 `报告:v2.txt` 悄悄改成 `报告_v2.txt` 会让用户找不到自己预期的文件，
而用户根本不知道发生过替换。宁可**明确报错并说清原因**。

例外是**目录 fsync 之类无法回避的差异**，那里做降级而非拒绝。
"""

from __future__ import annotations

from typing import Final

#: Windows 保留设备名。**必须连扩展名一起比对**：
#: `NUL.txt` 在 Windows 上同样是设备，任何扩展名都被忽略。
#: 这些名字保留自 MS-DOS，无法禁用。
RESERVED_NAMES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

#: Windows 文件名非法字符。含正斜杠与反斜杠——它们同时是路径分隔符，
#: 出现在文件名里就意味着目录穿越，必须一并禁掉。
#: Windows 文件名非法字符。
#:
#: 审计 D-141：原注释写「含正斜杠与反斜杠——它们同时是路径分隔符，
#: 出现在文件名里就意味着目录穿越，必须一并禁掉」，**但集合里并没有它们**
#: （实测 `why_rejected("a/b")` 与 `why_rejected("a\\b")` 都返回「可用」）。
#:
#: 不放进本集合的理由是**职责边界**，不是遗漏：
#: `ILLEGAL_CHARS` 回答的是「Windows 不接受这个字符」，
#: 而 `/` 与 `\` 在 Windows 上是**完全合法的路径分隔符**
#: ——`os.path.join` 天天在用，只是不能单独作为一个文件名分量。
#: 把它们混进来会让「路径」与「文件名」两种输入无法区分。
#:
#: 故分隔符由调用方负责切分：`guard._分量()` 按两种分隔符都切，
#: 再把每个分量交给本模块——那条路径是安全的（D-133 实测）。
#: **但下一个人若照原注释理解、直接把整条路径传进来就会中招**，
#: 故在 `why_rejected` 里显式拒绝含分隔符的输入（见下）。
#: 路径分隔符。**不属于 `ILLEGAL_CHARS`**（理由见上），
#: 但出现即说明调用方传错了东西——传进来的是路径，不是文件名。
_SEPARATORS: Final[tuple[str, ...]] = ("/", "\\")
ILLEGAL_CHARS: frozenset[str] = frozenset('<>:"|?*')


def is_reserved(name: str) -> bool:
    """是否为 Windows 保留设备名。**大小写不敏感**（Windows 本身如此）。

    只看文件名主体，不看整个路径：`user/CON/正常.txt` 里的 `CON`
    是**目录名**。Windows 确实也不允许用它建目录，所以一并拦。
    """
    return _stem(name).upper() in RESERVED_NAMES


def has_illegal_chars(name: str) -> bool:
    """文件名是否含 Windows 禁止的字符。

    **控制字符（U+0000–U+001F）一并禁掉**：它们在 Windows 上不可用，
    且会破坏日志与终端输出。
    """
    return any(ch in ILLEGAL_CHARS or ord(ch) < 0x20 for ch in name)


def why_rejected(name: str) -> str | None:
    """返回不可用的原因；可用则返回 `None`。

    **给用户看的原因必须是中文且能指导行动**——
    "invalid name" 用户无从下手，"文件名含 Windows 不允许的字符：：" 他立刻知道。
    """
    if not name:
        return "文件名不能为空"
    # 审计 D-141：含路径分隔符说明传进来的是**整条路径**而非文件名。
    # 本模块的契约是「单个文件名分量」——早先的注释说要禁分隔符，
    # 但集合里并没有，于是 `why_rejected("../../etc/passwd")` 返回
    # 「可用」，会让照文档理解直接调用的下一个人中招。
    #
    # 这里不把它们塞进 ILLEGAL_CHARS（那会混淆「Windows 不接受」
    # 与「这不是文件名」两回事），而是单独拦下并说清原因。
    if any(sep in name for sep in _SEPARATORS):
        return "这不是文件名，而是含路径分隔符的路径；请先切分出单个文件名"
    if name in {".", ".."}:
        return "文件名不能是 . 或 .."
    if name.endswith((" ", ".")):
        # Windows 静默剥掉结尾的空格与点，于是 `报告.txt ` 和 `报告.txt`
        # 是同一个文件。与其让用户困惑，不如拒绝
        return "Windows 文件名不能以空格或点结尾"
    illegal = sorted({ch for ch in name if ch in ILLEGAL_CHARS})
    if illegal:
        return f"文件名含 Windows 不允许的字符：{' '.join(illegal)}"
    ctrl = sorted({ch for ch in name if ord(ch) < 0x20})
    if ctrl:
        return "文件名含不可见控制字符"
    if is_reserved(name):
        return f"{_stem(name).upper()} 是 Windows 保留设备名，不能用作文件名"
    return None


def _stem(name: str) -> str:
    """去掉扩展名，留下文件名主体。

    `Path("a.tar.gz").stem` 是 `"a.tar"`，`splitext` 才是对的。
    这里要的就是"最后一个点之前"——`NUL.txt` 必须被认出来。

    `NUL.txt` 在 Windows 上忽略扩展名仍指向空设备，
    所以判断保留名必须拿**主体**去比，不能拿整个名字。
    """
    head, dot, _ = name.rpartition(".")
    return head if dot else name
