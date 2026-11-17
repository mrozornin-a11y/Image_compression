"""Сжатие одного изображения с сохранением разрешения, имени, расширения и метаданных.

Правила:
* JPEG — пересжатие Pillow с выбранным качеством, optimize=True, исходной
  субдискретизацией цвета, EXIF, ICC, DPI, XMP и комментарием;
* PNG — только без потерь (optimize=True), без изменения режима и глубины цвета;
* TIFF — только без потерь (Deflate), все страницы, исходная глубина и каналы;
* WEBP — lossless-файлы без потерь, остальные с высоким качеством;
* BMP и прочее — копирование без изменений.

Для форматов без потерь результат повторно открывается и попиксельно сравнивается
с оригиналом. Если сохранность не подтверждена, файл копируется без изменений.
Если результат не меньше оригинала — также сохраняется оригинал.
"""

from __future__ import annotations

import os
import shutil
import struct
from dataclasses import dataclass

from PIL import Image, ImageFile, JpegImagePlugin, PngImagePlugin, TiffImagePlugin

from .utils import long_path

Image.MAX_IMAGE_PIXELS = None
ImageFile.LOAD_TRUNCATED_IMAGES = False  # повреждённые файлы не «чинить» молча

# Итог обработки одного файла
COMPRESSED = "compressed"  # сохранена сжатая версия
KEPT = "kept"  # сохранён оригинал (сжатие не уменьшило файл или формат не сжимается)
FALLBACK = "fallback"  # не удалось обработать — скопирован оригинал (ошибка в журнал)


@dataclass
class CompressResult:
    status: str
    out_size: int
    message: str = ""


class UnsafeImage(Exception):
    """Нельзя гарантировать сохранность данных — файл копируется без изменений."""


# ---------------------------------------------------------------- общие функции


def copy_file(src: str, dst: str) -> None:
    """Копирование содержимого и времени изменения (без атрибута «только чтение»)."""
    lsrc, ldst = long_path(src), long_path(dst)
    tmp = ldst + ".~tmp"
    try:
        shutil.copyfile(lsrc, tmp)
        st = os.stat(lsrc)
        os.utime(tmp, (st.st_atime, st.st_mtime))
        os.replace(tmp, ldst)
    finally:
        if os.path.exists(tmp):
            _silent_remove(tmp)


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _frames(img: Image.Image) -> int:
    return getattr(img, "n_frames", 1)


def _pixels_equal(a: Image.Image, b: Image.Image) -> bool:
    """Попиксельное сравнение по полосам, без лишних копий всего изображения."""
    if a.size != b.size:
        return False
    if a.mode != b.mode:
        return False
    if a.mode == "P":
        a, b = a.convert("RGBA"), b.convert("RGBA")
    a.load()
    b.load()
    w, h = a.size
    step = max(1, 4_000_000 // max(1, w))
    for y in range(0, h, step):
        box = (0, y, w, min(h, y + step))
        if a.crop(box).tobytes() != b.crop(box).tobytes():
            return False
    return True


def _verify_lossless(src_img: Image.Image, tmp: str) -> None:
    """Сравнить все страницы исходного изображения и результата."""
    with Image.open(tmp) as out:
        n = _frames(src_img)
        if _frames(out) != n:
            raise UnsafeImage("после сохранения изменилось число страниц")
        for i in range(n):
            src_img.seek(i)
            out.seek(i)
            if not _pixels_equal(src_img, out):
                raise UnsafeImage("после сохранения изменились пиксели")
            if src_img.info.get("transparency") != out.info.get("transparency"):
                raise UnsafeImage("после сохранения изменилась прозрачность")


def _verify_basic(src_img: Image.Image, tmp: str) -> None:
    """Для сжатия с потерями: файл открывается, размер и режим совпадают."""
    with Image.open(tmp) as out:
        if out.size != src_img.size:
            raise UnsafeImage("после сохранения изменилось разрешение")
        if out.mode != src_img.mode:
            raise UnsafeImage("после сохранения изменился цветовой режим")
        out.load()


# ---------------------------------------------------------------- JPEG


def _save_jpeg(img: Image.Image, tmp: str, quality: int) -> None:
    if img.mode not in ("L", "RGB", "CMYK"):
        raise UnsafeImage(f"режим {img.mode} не поддерживается для JPEG")
    img.seek(0)
    params = {"quality": quality, "optimize": True}
    if img.mode != "L":
        sampling = JpegImagePlugin.get_sampling(img)
        if sampling >= 0:
            params["subsampling"] = sampling  # сохраняем исходную субдискретизацию цвета
    if img.info.get("progressive") or img.info.get("progression"):
        params["progressive"] = True
    for key in ("exif", "icc_profile", "dpi", "comment", "xmp"):
        if img.info.get(key):
            params[key] = img.info[key]
    img.save(tmp, format="JPEG", **params)
    _verify_basic(img, tmp)


# ---------------------------------------------------------------- PNG


def _png_header(path: str) -> tuple:
    """(битовая глубина, тип цвета) из заголовка IHDR."""
    with open(path, "rb") as f:
        head = f.read(33)
    if len(head) < 33 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        raise UnsafeImage("некорректный заголовок PNG")
    return head[24], head[25]


def _save_png(img: Image.Image, src: str, tmp: str) -> None:
    if _frames(img) > 1:
        raise UnsafeImage("анимированный PNG")
    bit_depth, color_type = _png_header(src)
    # Pillow понижает 16-битные цветные PNG до 8 бит, а 2/4-битные серые расширяет до 8.
    if bit_depth == 16 and color_type != 0:
        raise UnsafeImage("16-битный цветной PNG")
    if color_type == 0 and bit_depth in (2, 4):
        raise UnsafeImage(f"{bit_depth}-битный серый PNG")

    pnginfo = PngImagePlugin.PngInfo()
    for key, value in getattr(img, "text", {}).items():
        if isinstance(value, PngImagePlugin.iTXt):
            pnginfo.add_itxt(key, value, value.lang or "", value.tkey or "", zip=True)
        else:
            pnginfo.add_text(key, value, zip=len(value) > 1024)
    info = img.info
    if "gamma" in info:
        pnginfo.add(b"gAMA", struct.pack(">I", round(info["gamma"] * 100000)))
    if "chromaticity" in info:
        pnginfo.add(b"cHRM", struct.pack(">8I", *(round(v * 100000) for v in info["chromaticity"])))
    if "srgb" in info and "icc_profile" not in info:
        pnginfo.add(b"sRGB", bytes([info["srgb"]]))

    params = {"optimize": True, "pnginfo": pnginfo}
    for key in ("icc_profile", "dpi", "exif", "transparency"):
        if info.get(key) is not None and info.get(key) != b"":
            params[key] = info[key]
    img.save(tmp, format="PNG", **params)
    _verify_lossless(img, tmp)


# ---------------------------------------------------------------- TIFF

# Теги структуры файла — их Pillow/libtiff формирует сами при записи.
_TIFF_STRUCT_TAGS = {
    254, 255, 256, 257, 258, 259, 262, 266, 273, 277, 278, 279, 284, 317, 320, 322, 323,
    324, 325, 330, 338, 339, 347, 512, 513, 514, 515, 517, 518, 519, 520, 521, 530, 531,
    532, 34665, 34853, 40965,
}
_TIFF_SAFE_MODES = {"1", "L", "LA", "P", "RGB", "RGBA", "CMYK", "I;16", "I;16B", "I;16L", "I", "F"}


def _check_tiff_frame(img: Image.Image) -> None:
    if img.mode not in _TIFF_SAFE_MODES:
        raise UnsafeImage(f"режим {img.mode}")
    tags = img.tag_v2
    bits = tags.get(258, (1,))
    if not isinstance(bits, tuple):
        bits = (bits,)
    samples = tags.get(277, 1)
    if samples != len(img.getbands()):
        raise UnsafeImage(f"{samples} канала(ов) в файле, Pillow читает {len(img.getbands())}")
    depth = set(bits)
    ok = (
        depth == {8}
        or (depth == {1} and img.mode == "1")
        or (depth == {16} and img.mode.startswith("I;16"))
        or (depth == {32} and img.mode in ("I", "F"))
    )
    if not ok:
        raise UnsafeImage(f"глубина {bits} бит при режиме {img.mode}")


def _tiff_info(img: Image.Image, full: bool) -> TiffImagePlugin.ImageFileDirectory_v2:
    src = img.tag_v2
    ifd = TiffImagePlugin.ImageFileDirectory_v2()
    for tag, value in src.items():
        if tag in _TIFF_STRUCT_TAGS:
            continue
        if not full and tag not in (282, 283, 296, 34675):  # разрешение и ICC-профиль
            continue
        ifd[tag] = value
        if tag in src.tagtype:
            ifd.tagtype[tag] = src.tagtype[tag]
    return ifd


def _write_tiff(img: Image.Image, tmp: str, full_meta: bool) -> None:
    n = _frames(img)
    # Страницы пишутся по одной, чтобы у каждой остались свои теги (DPI, описание и т. п.).
    tf = TiffImagePlugin.AppendingTiffWriter(tmp, new=True)
    try:
        for i in range(n):
            img.seek(i)
            img.load()
            info = {"compression": "tiff_adobe_deflate", "tiffinfo": _tiff_info(img, full_meta)}
            if full_meta and i == 0:
                exif = img.getexif()
                if exif.get_ifd(0x8769) or exif.get_ifd(0x8825):
                    info["exif"] = exif
            img.encoderinfo = info
            img.encoderconfig = ()
            TiffImagePlugin._save(img, tf, tmp)
            tf.newFrame()
    finally:
        tf.close()
        # Pillow повторно вызывает close() из деструктора уже закрытого объекта — отключаем.
        tf.close = lambda: None


def _save_tiff(img: Image.Image, tmp: str) -> str:
    for i in range(_frames(img)):
        img.seek(i)
        _check_tiff_frame(img)
    note = ""
    try:
        _write_tiff(img, tmp, full_meta=True)
        _verify_lossless(img, tmp)
    except Exception:
        # Не все теги могут быть записаны библиотекой — повторяем с основными метаданными.
        _silent_remove(tmp)
        _write_tiff(img, tmp, full_meta=False)
        _verify_lossless(img, tmp)
        note = "часть служебных тегов TIFF не перенесена (сохранены разрешение и ICC-профиль)"
    return note


# ---------------------------------------------------------------- WEBP


def _webp_kind(path: str) -> tuple:
    """(lossless, animated) по чанкам RIFF-контейнера."""
    lossless = animated = False
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
            raise UnsafeImage("некорректный заголовок WEBP")
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
            if cid == b"VP8L":
                lossless = True
            elif cid in (b"ANIM", b"ANMF"):
                animated = True
            elif cid == b"VP8 ":
                lossless = False
            if cid in (b"VP8 ", b"VP8L"):
                break
            f.seek(size + (size & 1), 1)
    return lossless, animated


def _save_webp(img: Image.Image, src: str, tmp: str, quality: int) -> None:
    lossless, animated = _webp_kind(src)
    if animated or _frames(img) > 1:
        raise UnsafeImage("анимированный WEBP")
    params = {"method": 6}
    if lossless:
        params.update(lossless=True, quality=100, exact=True)
    else:
        params.update(quality=quality)
    for key in ("icc_profile", "exif", "xmp"):
        if img.info.get(key):
            params[key] = img.info[key]
    img.save(tmp, format="WEBP", **params)
    if lossless:
        _verify_lossless(img, tmp)
    else:
        _verify_basic(img, tmp)


# ---------------------------------------------------------------- точка входа


def compress_image(src: str, dst: str, quality: int = 92) -> CompressResult:
    """Сжать изображение src в dst. Исходный файл только читается.

    Возвращает итог; исключение OSError означает, что не удалось даже скопировать оригинал.
    """
    lsrc, ldst = long_path(src), long_path(dst)
    tmp = ldst + ".~tmp"
    orig_size = os.path.getsize(lsrc)
    note = ""
    try:
        with Image.open(lsrc) as img:
            fmt = img.format
            if fmt in ("JPEG", "MPO"):
                _save_jpeg(img, tmp, quality)
            elif fmt == "PNG":
                _save_png(img, lsrc, tmp)
            elif fmt == "TIFF":
                note = _save_tiff(img, tmp)
            elif fmt == "WEBP":
                _save_webp(img, lsrc, tmp, quality)
            else:
                copy_file(src, dst)
                return CompressResult(KEPT, orig_size, f"формат {fmt} копируется без изменений")
    except UnsafeImage as e:
        _silent_remove(tmp)
        copy_file(src, dst)
        return CompressResult(KEPT, orig_size, f"скопирован без изменений: {e}")
    except Exception as e:
        _silent_remove(tmp)
        copy_file(src, dst)
        reason = str(e) or e.__class__.__name__
        return CompressResult(FALLBACK, orig_size, f"не удалось обработать ({reason}), скопирован оригинал")

    try:
        new_size = os.path.getsize(tmp)
        if new_size >= orig_size:
            _silent_remove(tmp)
            copy_file(src, dst)
            return CompressResult(KEPT, orig_size, "сжатие не уменьшило файл, сохранён оригинал")
        st = os.stat(lsrc)
        os.utime(tmp, (st.st_atime, st.st_mtime))
        os.replace(tmp, ldst)
        return CompressResult(COMPRESSED, new_size, note)
    finally:
        if os.path.exists(tmp):
            _silent_remove(tmp)
