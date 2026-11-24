# -*- coding: utf-8 -*-
"""图片落盘、体积统计与自动清理。

稳定性要点：
- 只接受通过魔数校验的真实图片字节，拒绝把错误页/HTML 当图片发出去；
- 文件名带时间戳与随机串，避免并发覆盖；
- 清理只作用于插件自己的数据目录，按“超期 + 超量”两条规则兜底磁盘。
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime
from pathlib import Path

try:  # 插件内相对导入；独立运行时退回绝对导入
    from .providers.base import sniff_image
except ImportError:  # pragma: no cover
    from core.providers.base import sniff_image  # type: ignore


class ImageStore:
    def __init__(self, root: Path, keep_days: int = 7, max_files: int = 2000):
        self.root = Path(root)
        self.keep_days = max(0, int(keep_days))
        self.max_files = max(10, int(max_files))

    # ---------- 写入 ----------

    def save(self, data: bytes, ext: str = "", prefix: str = "img") -> Path:
        """保存一张图片，返回绝对路径。数据不合法时抛 ValueError。"""
        if not data:
            raise ValueError("图片数据为空")
        detected = sniff_image(data)
        if not detected:
            raise ValueError(
                "返回内容不是可识别的图片（可能被服务商拒绝或返回了错误页）"
            )
        real_ext, _mime = detected
        ext = real_ext if not ext else ext.lower().lstrip(".")
        if ext not in ("png", "jpg", "webp", "gif"):
            ext = real_ext

        day_dir = self.root / datetime.now().strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(5):
            name = f"{prefix}_{datetime.now():%H%M%S}_{uuid.uuid4().hex[:8]}.{ext}"
            path = day_dir / name
            if not path.exists():
                break
        tmp = path.with_suffix(path.suffix + ".part")
        try:
            with open(tmp, "wb") as handle:
                handle.write(data)
        except FileNotFoundError:
            # 目录可能被并发清理删掉：重建后再写一次
            day_dir.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as handle:
                handle.write(data)
        os.replace(tmp, path)
        return path.resolve()

    # ---------- 清理 ----------

    def _iter_files(self):
        if not self.root.exists():
            return
        for path in self.root.rglob("*"):
            if not path.is_file() or path.suffix == ".part":
                continue
            yield path

    def cleanup(self) -> dict:
        """删除超期文件并按数量裁剪，返回统计信息。"""
        deleted = 0
        errors = 0
        records: list[tuple[float, Path, int]] = []

        cutoff = time.time() - self.keep_days * 86400 if self.keep_days > 0 else None
        for path in self._iter_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            if cutoff is not None and stat.st_mtime < cutoff:
                try:
                    path.unlink()
                    deleted += 1
                    continue
                except OSError:
                    errors += 1
                    continue
            records.append((stat.st_mtime, path, stat.st_size))

        if len(records) > self.max_files:
            records.sort(key=lambda item: item[0])
            overflow = len(records) - self.max_files
            for _mtime, path, _size in records[:overflow]:
                try:
                    path.unlink()
                    deleted += 1
                except OSError:
                    errors += 1
            records = records[overflow:]

        # 顺手清掉空目录（只动一天前的，避免与正在写入的目录竞争）
        try:
            dir_cutoff = time.time() - 86400
            for sub in self.root.glob("*"):
                if not sub.is_dir():
                    continue
                try:
                    if sub.stat().st_mtime > dir_cutoff:
                        continue
                    if not any(sub.iterdir()):
                        sub.rmdir()
                except OSError:
                    continue
        except OSError:
            pass

        remaining = len(list(self._iter_files()))
        return {
            "deleted": deleted,
            "errors": errors,
            "remaining": remaining,
            "bytes": sum(item[2] for item in records),
        }

    def stats(self) -> dict:
        count = 0
        total = 0
        oldest: float | None = None
        for path in self._iter_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            count += 1
            total += stat.st_size
            if oldest is None or stat.st_mtime < oldest:
                oldest = stat.st_mtime
        return {
            "count": count,
            "bytes": total,
            "root": str(self.root),
            "oldest": datetime.fromtimestamp(oldest).strftime("%Y-%m-%d %H:%M")
            if oldest
            else "-",
        }
