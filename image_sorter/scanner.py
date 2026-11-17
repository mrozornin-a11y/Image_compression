"""Рекурсивное сканирование исходной папки и фильтрация изображений по имени."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from PIL import Image

from .utils import long_path

Image.MAX_IMAGE_PIXELS = None  # большие сканы и микрофотографии — не «бомбы»

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}

# Вариант А: «число. пробел название»
NUMBERED_RE = re.compile(r"^\d+\.\s+")

MODE_NUMBERED = "A"  # имена с номером и точкой
MODE_OTHER = "B"  # все остальные имена


@dataclass
class FileEntry:
    rel_path: str  # путь относительно исходной папки
    size: int
    is_image: bool
    width: int = 0
    height: int = 0
    dpi: Optional[tuple] = None
    info_error: str = ""

    @property
    def name(self) -> str:
        return os.path.basename(self.rel_path)

    @property
    def rel_dir(self) -> str:
        return os.path.dirname(self.rel_path)

    def is_numbered(self) -> bool:
        return bool(NUMBERED_RE.match(self.name))

    def is_selected(self, mode: str) -> bool:
        """Изображение соответствует выбранному режиму отбора."""
        if not self.is_image:
            return False
        return self.is_numbered() if mode == MODE_NUMBERED else not self.is_numbered()


@dataclass
class ScanResult:
    root: str
    dirs: list = field(default_factory=list)  # относительные пути всех вложенных папок
    files: list = field(default_factory=list)  # FileEntry
    errors: list = field(default_factory=list)  # тексты ошибок чтения

    @property
    def images(self) -> list:
        return [f for f in self.files if f.is_image]

    @property
    def others(self) -> list:
        return [f for f in self.files if not f.is_image]


def natural_key(name: str) -> list:
    """Ключ «естественной» сортировки: «2. …» раньше «10. …»."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name.lower())]


def is_image_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS


def _round_dpi(value) -> float:
    value = float(value)
    return round(value) if abs(value - round(value)) < 0.05 else round(value, 1)


def read_dpi(img: Image.Image) -> Optional[tuple]:
    """DPI из метаданных (JFIF/pHYs/TIFF, затем EXIF). None, если не указан."""
    dpi = img.info.get("dpi")
    if dpi:
        try:
            x, y = float(dpi[0]), float(dpi[1])
            if x > 1 and y > 1:  # 1 DPI — значение-заглушка «не указано»
                return _round_dpi(x), _round_dpi(y)
        except (TypeError, ValueError, IndexError):
            pass
    try:
        exif = img.getexif()
        x, y, unit = exif.get(282), exif.get(283), exif.get(296, 2)
        if x and y and float(x) > 1 and float(y) > 1:
            factor = 2.54 if unit == 3 else 1.0
            if unit in (2, 3):
                return _round_dpi(float(x) * factor), _round_dpi(float(y) * factor)
    except Exception:
        pass
    return None


def read_image_info(path: str) -> tuple:
    """(ширина, высота, dpi) — читается только заголовок файла, не всё изображение."""
    with Image.open(long_path(path)) as img:
        return img.width, img.height, read_dpi(img)


def scan_folder(
    root: str,
    progress: Optional[Callable[[int], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> ScanResult:
    root = os.path.normpath(os.path.abspath(root))
    result = ScanResult(root=root)
    lroot = long_path(root)

    def onerror(err: OSError):
        result.errors.append(f"Нет доступа к папке: {err.filename} ({err.strerror})")

    count = 0
    for cur, dirnames, filenames in os.walk(lroot, onerror=onerror):
        if cancelled and cancelled():
            break
        dirnames.sort(key=natural_key)
        rel_dir = cur[len(lroot):].lstrip("\\/")
        if rel_dir:
            result.dirs.append(rel_dir)
        for name in sorted(filenames, key=natural_key):
            rel = os.path.join(rel_dir, name) if rel_dir else name
            full = os.path.join(cur, name)
            try:
                size = os.stat(full).st_size
            except OSError as e:
                result.errors.append(f"Не удалось прочитать файл: {rel} ({e.strerror})")
                continue
            entry = FileEntry(rel_path=rel, size=size, is_image=is_image_name(name))
            if entry.is_image:
                try:
                    entry.width, entry.height, entry.dpi = read_image_info(full)
                except Exception as e:  # повреждённый или неподдерживаемый файл
                    entry.info_error = str(e) or e.__class__.__name__
            result.files.append(entry)
            count += 1
            if progress and count % 25 == 0:
                progress(count)
    if progress:
        progress(count)
    return result
