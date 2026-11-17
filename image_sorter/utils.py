"""Вспомогательные функции: длинные пути Windows, форматирование, проверка папок."""

from __future__ import annotations

import os

_WIN = os.name == "nt"


def long_path(path: str) -> str:
    """Вернуть путь, пригодный для файловых операций в Windows длиннее 260 символов.

    В Windows добавляется префикс ``\\\\?\\`` (или ``\\\\?\\UNC\\`` для сетевых путей).
    В остальных ОС путь возвращается без изменений.
    """
    if not _WIN:
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def is_same_or_nested(a: str, b: str) -> bool:
    """True, если папки совпадают или одна находится внутри другой."""
    na, nb = _norm(a), _norm(b)
    try:
        common = os.path.commonpath([na, nb])
    except ValueError:  # разные диски
        return False
    return common == na or common == nb


def format_mb(size: int) -> str:
    """Размер в МБ с запятой: «4,97 МБ»."""
    return f"{size / (1024 * 1024):.2f} МБ".replace(".", ",")


def format_size(size: int) -> str:
    """Человекочитаемый размер (КБ/МБ/ГБ) с запятой в качестве разделителя."""
    sign = "-" if size < 0 else ""
    size = abs(size)
    if size >= 1024 ** 3:
        text = f"{size / 1024 ** 3:.2f} ГБ"
    elif size >= 1024 ** 2:
        text = f"{size / 1024 ** 2:.2f} МБ"
    elif size >= 1024:
        text = f"{size / 1024:.1f} КБ"
    else:
        text = f"{size} Б"
    return sign + text.replace(".", ",")


def format_dpi(dpi) -> str:
    """Текст DPI или пустая строка, если DPI не указан."""
    if not dpi:
        return ""
    x, y = dpi
    if x == y:
        return f"{x:g} DPI"
    return f"{x:g} × {y:g} DPI"
