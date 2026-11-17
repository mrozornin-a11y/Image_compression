"""Обработка всей папки: структура каталогов, сжатие отобранных изображений, копирование всех остальных файлов."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from .compressor import COMPRESSED, FALLBACK, compress_image, copy_file
from .scanner import ScanResult
from .utils import long_path


@dataclass
class ProcessStats:
    total_files: int = 0  # сколько файлов нужно обработать
    done_files: int = 0
    images_processed: int = 0  # сохранено изображений в выходной папке
    images_compressed: int = 0  # из них уменьшено
    images_kept: int = 0  # из них сохранён оригинал
    images_copied: int = 0  # изображения вне списка — скопированы без изменений
    others_copied: int = 0
    errors: int = 0
    orig_images_size: int = 0  # исходный размер обработанных изображений
    new_images_size: int = 0  # их размер после обработки
    selected_images_size: int = 0  # исходный размер всех выбранных изображений
    cancelled: bool = False
    error_messages: list = field(default_factory=list)

    @property
    def saved(self) -> int:
        return self.orig_images_size - self.new_images_size

    @property
    def saved_percent(self) -> float:
        return 100.0 * self.saved / self.orig_images_size if self.orig_images_size else 0.0


def plan_tasks(scan: ScanResult, mode: str, overrides: Optional[dict] = None) -> list:
    """Список (FileEntry, сжимать ли) для всех файлов исходной папки.

    Сжимаются только изображения из списка; остальные изображения и прочие файлы
    копируются без изменений.
    """
    return [(entry, entry.is_selected(mode, overrides)) for entry in scan.files]


def find_existing(scan: ScanResult, out_root: str) -> list:
    """Относительные пути файлов, которые уже существуют в выходной папке."""
    existing = []
    for entry in scan.files:
        if os.path.lexists(long_path(os.path.join(out_root, entry.rel_path))):
            existing.append(entry.rel_path)
    return existing


def process(
    scan: ScanResult,
    mode: str,
    out_root: str,
    quality: int,
    overrides: Optional[dict] = None,
    cancel_event: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> ProcessStats:
    src_root = scan.root
    out_root = os.path.normpath(os.path.abspath(out_root))
    tasks = plan_tasks(scan, mode, overrides)
    stats = ProcessStats(total_files=len(tasks))
    stats.selected_images_size = sum(e.size for e, compress in tasks if compress)

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    def error(msg: str) -> None:
        stats.errors += 1
        stats.error_messages.append(msg)
        log("ОШИБКА: " + msg)

    # 1. Исходная структура каталогов (включая папки, которые окажутся пустыми)
    try:
        os.makedirs(long_path(out_root), exist_ok=True)
    except OSError as e:
        error(f"не удалось создать выходную папку {out_root}: {e.strerror or e}")
        return stats
    for rel_dir in scan.dirs:
        try:
            os.makedirs(long_path(os.path.join(out_root, rel_dir)), exist_ok=True)
        except OSError as e:
            error(f"не удалось создать папку {rel_dir}: {e.strerror or e}")

    # 2–5. Файлы
    for entry, compress in tasks:
        if cancel_event is not None and cancel_event.is_set():
            stats.cancelled = True
            break
        if on_progress:
            on_progress(stats.done_files, stats.total_files, entry.rel_path)
        src = os.path.join(src_root, entry.rel_path)
        dst = os.path.join(out_root, entry.rel_path)
        try:
            os.makedirs(long_path(os.path.dirname(dst)), exist_ok=True)
            if compress:
                result = compress_image(src, dst, quality)
                orig = os.path.getsize(long_path(src))
                stats.images_processed += 1
                stats.orig_images_size += orig
                stats.new_images_size += result.out_size
                if result.status == COMPRESSED:
                    stats.images_compressed += 1
                else:
                    stats.images_kept += 1
                if result.status == FALLBACK:
                    error(f"{entry.rel_path}: {result.message}")
                elif result.message:
                    log(f"{entry.rel_path}: {result.message}")
            else:
                copy_file(src, dst)
                if entry.is_image:
                    stats.images_copied += 1
                else:
                    stats.others_copied += 1
        except OSError as e:
            error(f"{entry.rel_path}: ошибка чтения/записи — {e.strerror or e}")
        except Exception as e:  # на всякий случай: один файл не должен останавливать обработку
            error(f"{entry.rel_path}: {e}")
        stats.done_files += 1

    if on_progress:
        on_progress(stats.done_files, stats.total_files, "")
    return stats
