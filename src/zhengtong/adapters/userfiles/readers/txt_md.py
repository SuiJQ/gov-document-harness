"""txt / md 读取器。

两条纪律：

**一、零第三方依赖**（D-032）：只用标准库。
**二、2 万字上限**（D-053）：超出的部分不读，截断并标明。
"""

from __future__ import annotations

from pathlib import Path

from ....domain.errors import ErrorCode, Result
from ....domain.task import 单文件上限, 读到的文件

#: 单文件读取上限（D-053）。**从 domain 取，不在此另写一个数**——
#: docx 与 txt/md 若各写各的，同一条决策就有了两个上限，
#: 读 txt 会截断而读 docx 不会，是不设防。
MAX_CHARS: int = 单文件上限

#: 识别为文本的后缀。**只有这两个**——D-032 明确 `.pdf`/`.doc`/图片
#: 不实现解析，只登记文件名。给 `.txt` 之外的都放开就成了「偷偷支持」。
支持后缀: frozenset[str] = frozenset({".txt", ".md"})

#: 探测顺序（D-032）：UTF-8 → GB18030 → 回退。
#: GB18030 是超集，能兜住 GBK/GB2312，用户 Windows 上的公文多是这些。
_编码顺序: tuple[str, ...] = ("utf-8", "gb18030")


def read_txt_md(路径: Path) -> Result[读到的文件]:
    """读一个 txt/md 文件，返回内容（可能已截断到 2 万字）。

    **编码自动探测**：先试 UTF-8，再试 GB18030，都不行则
    以 GB18030 的替换字符模式读（`errors="replace"`）——
    **宁可带 � 也不能抛异常崩掉整个任务**（D-108）。

    **截断保留开头**：公文的价值在开头（标题、发号、称谓），D-053 的
    「2 万字」是给长文档兜底，不是要把标题砍掉。
    """
    if not 路径.is_file():
        return Result.fail(ErrorCode.FILE_UNREADABLE, f"文件不存在或不是文件：{路径.name}")
    if 路径.suffix.lower() not in 支持后缀:
        return Result.fail(
            ErrorCode.FILE_UNREADABLE,
            f"不支持解析 {路径.suffix}（D-032：pdf/doc/图片只登记不解析）",
        )
    try:
        原始 = 路径.read_bytes()
    except OSError as exc:
        return Result.fail(ErrorCode.FILE_UNREADABLE, f"读取失败：{type(exc).__name__}")

    文本 = _解码(原始)
    return Result.ok(_截断(文本))


def _截断(文本: str) -> 读到的文件:
    """截断到上限并**标明**（D-053 的两条，缺一不可）。"""
    if len(文本) <= MAX_CHARS:
        return 读到的文件(text=文本, 已截断=False, 完整字数=len(文本), 原始字数=len(文本))
    return 读到的文件(text=文本[:MAX_CHARS], 已截断=True, 完整字数=MAX_CHARS, 原始字数=len(文本))


#: UTF-8 BOM。Windows 记事本默认存 UTF-8 带 BOM，
#: 不剥掉的话它会变成正文第一个字符混进公文里——**一个肉眼难辨的脏字符**。
_UTF8_BOM = "\ufeff"


def _解码(原始: bytes) -> str:
    """按 `utf-8 → gb18030 → 替换` 的顺序解码。"""
    for 编码 in _编码顺序:
        try:
            return 剥BOM(原始.decode(编码))
        except UnicodeDecodeError:
            continue
    # 都失败：逐字节替换，保留能读的部分
    return 剥BOM(原始.decode("gb18030", errors="replace"))


def 剥BOM(文本: str) -> str:
    """剥掉开头的 BOM。**只剥开头**——
    正文中间的 U+FEFF 是合法字符（零宽不换行空格），剥掉就改内容了。"""
    return 文本.removeprefix(_UTF8_BOM)
