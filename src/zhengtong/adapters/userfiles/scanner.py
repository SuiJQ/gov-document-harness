"""`user/` 目录自动扫描与登记（D-022）。

两条纪律直接体现在这个类上：

**一、只读**（D-022 / D-032）：没有移动、改写、归档的方法。
**「做不到」比「不该做」可靠**——不给写端口，模型就调不出来。

**二、只给元信息**（D-023）：`UserFile` 只含 name/size/suffix，
内容必须经 `read_text` 按需取。否则 AI 开局就把所有材料塞进上下文，
既烧 token 又违背按需读取的设计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ...domain.errors import ErrorCode, Result
from ...domain.task import UserFile
from ..guard import ensure_within
from .readers.docx import read_docx
from .readers.txt_md import read_txt_md


@dataclass(slots=True)
class LocalUserFiles:
    """`user/` 的真实只读访问。

    **登记发生在构造时**（`scan_and_register`），
    `list_registered` 直接返回登记结果——D-022 要求自动扫描不逐项确认。
    """

    根: Path
    _已登记: tuple[UserFile, ...] = field(default=(), init=False)

    def __post_init__(self) -> None:
        self.scan_and_register()

    def scan_and_register(self) -> tuple[UserFile, ...]:
        """扫描 `user/` 下所有文件并登记元信息。**递归**。

        不逐项向用户确认（D-022）。符号链接指向目录外的**跳过**——
        用户放进来一个指向隐私目录的软链不该让我们跟出去。
        """
        条目: list[UserFile] = []
        if not self.根.is_dir():
            self._已登记 = ()
            return ()
        for 路径 in sorted(self.根.rglob("*")):
            if not 路径.is_file():
                continue
            # 软链解析后必须仍在 user/ 内
            if ensure_within(self.根, 路径).is_error:
                continue
            条目.append(
                UserFile(
                    name=路径.name,
                    size=路径.stat().st_size,
                    suffix=路径.suffix.lower(),
                )
            )
        self._已登记 = tuple(条目)
        return self._已登记

    def list_registered(self) -> tuple[UserFile, ...]:
        """已登记文件的元信息。**不含内容**（D-023）。"""
        return self._已登记

    def read_text(self, name: str) -> Result[str]:
        """按需读取内容。**截断到上限由 reader 负责**（D-053）。

        `name` 来自登记列表，**必须重新过路径守卫**——
        工具层传来的 name 可能被构造成 `../../system/config.toml`（带密钥）。
        """
        定位 = self._定位(name)
        if 定位.is_error:
            return Result.fail(ErrorCode.FILE_UNREADABLE, f"未登记的文件：{name}")
        路径 = 定位.unwrap()
        后缀 = 路径.suffix.lower()
        if 后缀 == ".docx":
            return read_docx(路径)
        return read_txt_md(路径)

    def _定位(self, name: str) -> Result[Path]:
        """按文件名找已登记文件，且**必须落在 user/ 内**。"""
        安全 = ensure_within(self.根, self.根 / name)
        if 安全.is_error:
            return Result.fail(ErrorCode.PATH_ESCAPE, "路径越出 user/ 范围")
        路径 = 安全.unwrap()
        if not 路径.is_file():
            return Result.fail(ErrorCode.FILE_UNREADABLE, f"文件不存在：{name}")
        return Result.ok(路径)
