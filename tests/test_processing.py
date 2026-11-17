"""Проверка работоспособности на тестовом наборе (без графического интерфейса).

Запуск из папки проекта:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageCms, PngImagePlugin  # noqa: E402

from image_sorter.processing import find_existing, process  # noqa: E402
from image_sorter.scanner import MODE_NUMBERED, MODE_OTHER, scan_folder  # noqa: E402
from image_sorter.utils import is_same_or_nested  # noqa: E402


def photo(size=(640, 480), mode="RGB") -> Image.Image:
    """Изображение, похожее на фотографию: градиенты + шум."""
    w, h = size
    noise = Image.effect_noise(size, 40)
    grad = Image.linear_gradient("L").resize(size)
    r = Image.blend(grad, noise, 0.3)
    g = Image.blend(grad.transpose(Image.Transpose.ROTATE_90).resize(size), noise, 0.3)
    b = Image.blend(noise, grad.transpose(Image.Transpose.FLIP_TOP_BOTTOM), 0.5)
    img = Image.merge("RGB", (r, g, b))
    return img.convert(mode) if mode != "RGB" else img


def png16_rgb(path: str, w=64, h=48) -> None:
    """16-битный RGB PNG, записанный вручную (Pillow такой не пишет)."""
    raw = b"".join(
        b"\x00" + b"".join(struct.pack(">HHH", x * 1000 % 65536, y * 1300 % 65536, (x * y) % 65536) for x in range(w))
        for y in range(h)
    )

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(raw, 0)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(data)


def file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def tree_snapshot(root: str) -> dict:
    snap = {}
    for cur, dirs, files in os.walk(root):
        rel = os.path.relpath(cur, root)
        snap[rel + os.sep] = None
        for name in files:
            p = os.path.join(cur, name)
            snap[os.path.join(rel, name)] = (file_hash(p), os.stat(p).st_mtime)
    return snap


class ProcessingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="img_sorter_test_")
        cls.src = os.path.join(cls.tmp, "Исходные фотографии")
        s = cls.src
        os.makedirs(os.path.join(s, "Г 144"))
        os.makedirs(os.path.join(s, "Г 149", "Глубоко", "вложенная папка"))
        os.makedirs(os.path.join(s, "Пустая папка"))
        srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        exif = Image.Exif()
        exif[0x010F] = "Test camera"
        exif[0x0112] = 6  # ориентация
        cls.icc = srgb

        # Вариант А
        photo().save(os.path.join(s, "Г 144", "93. Г 144 скрещенные.jpg"), quality=100,
                     dpi=(72, 72), icc_profile=srgb, exif=exif.tobytes(), subsampling=0)
        photo().save(os.path.join(s, "Г 149", "15. Г 149 отраженный свет.PNG"), compress_level=1,
                     dpi=(300, 300), icc_profile=srgb)
        photo((300, 200)).save(os.path.join(s, "Г 149", "2. Образец 35.tif"), dpi=(600, 600))
        photo((320, 240)).save(os.path.join(s, "Г 149", "Глубоко", "вложенная папка", "7. Многостраничный.TIFF"),
                               save_all=True, append_images=[photo((100, 80), "L"), photo((50, 40), "CMYK")])
        Image.linear_gradient("L").resize((256, 256)).convert("I;16").save(
            os.path.join(s, "Г 149", "8. Серый 16 бит.tif"))
        png16_rgb(os.path.join(s, "Г 149", "9. Цветной 16 бит.png"))
        photo().save(os.path.join(s, "Г 144", "10. Без потерь.webp"), lossless=True, method=0)
        photo().save(os.path.join(s, "Г 144", "11. С потерями.WEBP"), quality=100)
        photo((200, 100)).save(os.path.join(s, "Г 144", "12. Растр.bmp"))
        rgba = photo((200, 150)).convert("RGBA")
        rgba.putalpha(Image.linear_gradient("L").resize((200, 150)))
        info = PngImagePlugin.PngInfo()
        info.add_text("Description", "Образец Г 144")
        rgba.save(os.path.join(s, "Г 144", "13. Прозрачный.png"), pnginfo=info, compress_level=0)
        with open(os.path.join(s, "Г 144", "14. Повреждённый.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0 not a real jpeg")

        # Вариант Б
        photo().save(os.path.join(s, "Г 144", "4_Г 144_отраж_5х.jpg"), quality=100)
        photo().save(os.path.join(s, "Г 149", "3_Г 149_скрещенные.JPEG"), quality=95)
        photo().save(os.path.join(s, "Г 149", "Г 149_скрещенные.png"))
        photo().save(os.path.join(s, "образец_27.tif"))
        photo().save(os.path.join(s, "93.без пробела.jpg"), quality=100)  # не вариант А: нет пробела

        # Прочие файлы
        with open(os.path.join(s, "Г 144", "описание.txt"), "w", encoding="utf-8") as f:
            f.write("Описание образца Г 144\n")
        with open(os.path.join(s, "Г 149", "Глубоко", "заметки.docx"), "wb") as f:
            f.write(os.urandom(1000))

        cls.snapshot_before = tree_snapshot(s)
        cls.scan = scan_folder(s)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------ фильтрация

    def names(self, mode):
        return sorted(e.name for e in self.scan.images if e.is_selected(mode))

    def test_filter_numbered(self):
        self.assertEqual(self.names(MODE_NUMBERED), sorted([
            "93. Г 144 скрещенные.jpg", "15. Г 149 отраженный свет.PNG", "2. Образец 35.tif",
            "7. Многостраничный.TIFF", "8. Серый 16 бит.tif", "9. Цветной 16 бит.png",
            "10. Без потерь.webp", "11. С потерями.WEBP", "12. Растр.bmp", "13. Прозрачный.png",
            "14. Повреждённый.jpg",
        ]))

    def test_filter_other(self):
        self.assertEqual(self.names(MODE_OTHER), sorted([
            "4_Г 144_отраж_5х.jpg", "3_Г 149_скрещенные.JPEG", "Г 149_скрещенные.png",
            "образец_27.tif", "93.без пробела.jpg",
        ]))

    def test_modes_partition(self):
        a, b = set(self.names(MODE_NUMBERED)), set(self.names(MODE_OTHER))
        self.assertFalse(a & b)
        self.assertEqual(len(a) + len(b), len(self.scan.images))

    def test_scan_info(self):
        e = next(x for x in self.scan.files if x.name == "93. Г 144 скрещенные.jpg")
        self.assertEqual((e.width, e.height, e.dpi), (640, 480, (72, 72)))
        e = next(x for x in self.scan.files if x.name == "2. Образец 35.tif")
        self.assertEqual(e.dpi, (600, 600))
        self.assertEqual(len(self.scan.others), 2)

    def test_nested_check(self):
        self.assertTrue(is_same_or_nested(self.src, os.path.join(self.src, "Г 144")))
        self.assertTrue(is_same_or_nested(os.path.join(self.src, "Г 144"), self.src))
        self.assertTrue(is_same_or_nested(self.src, self.src))
        self.assertFalse(is_same_or_nested(self.src, os.path.join(self.tmp, "Исходные фотографии 2")))

    # ------------------------------------------------------------ обработка

    def run_mode(self, mode, out_name):
        out = os.path.join(self.tmp, out_name)
        logs = []
        stats = process(self.scan, mode, out, 92, on_log=logs.append)
        return out, stats, logs

    def test_process_numbered(self):
        out, stats, logs = self.run_mode(MODE_NUMBERED, "Обработанные А")
        self.assertEqual(tree_snapshot(self.src), self.snapshot_before, "исходная папка изменилась")

        # структура папок полностью сохранена, включая пустые
        for d in self.scan.dirs:
            self.assertTrue(os.path.isdir(os.path.join(out, d)), d)

        out_files = {os.path.relpath(os.path.join(c, f), out) for c, _, fs in os.walk(out) for f in fs}
        expected = {e.rel_path for e in self.scan.files}  # копируются все файлы
        self.assertEqual(out_files, expected)

        # прочие файлы и изображения, исключённые фильтром, скопированы без изменений
        for e in self.scan.others + [x for x in self.scan.images if not x.is_selected(MODE_NUMBERED)]:
            self.assertEqual(file_hash(os.path.join(out, e.rel_path)), file_hash(os.path.join(self.src, e.rel_path)))

        # разрешение не изменилось, страницы сохранены
        for e in self.scan.images:
            if not e.is_selected(MODE_NUMBERED) or e.info_error:
                continue
            with Image.open(os.path.join(self.src, e.rel_path)) as a, Image.open(os.path.join(out, e.rel_path)) as b:
                self.assertEqual(a.size, b.size, e.rel_path)
                self.assertEqual(a.mode, b.mode, e.rel_path)
                self.assertEqual(getattr(a, "n_frames", 1), getattr(b, "n_frames", 1), e.rel_path)
                self.assertEqual(a.format, b.format, e.rel_path)

        p = lambda *x: os.path.join(out, *x)  # noqa: E731
        s = lambda *x: os.path.join(self.src, *x)  # noqa: E731
        # JPEG: меньше, метаданные на месте
        jpg = ("Г 144", "93. Г 144 скрещенные.jpg")
        self.assertLess(os.path.getsize(p(*jpg)), os.path.getsize(s(*jpg)))
        with Image.open(p(*jpg)) as im:
            self.assertEqual(im.info.get("dpi"), (72, 72))
            self.assertEqual(im.info.get("icc_profile"), self.icc)
            self.assertEqual(im.getexif().get(0x0112), 6)
            self.assertEqual(im.getexif().get(0x010F), "Test camera")
        # PNG и TIFF без потерь: пиксели идентичны
        for rel in [("Г 149", "15. Г 149 отраженный свет.PNG"), ("Г 149", "2. Образец 35.tif"),
                    ("Г 144", "13. Прозрачный.png"), ("Г 144", "10. Без потерь.webp"),
                    ("Г 149", "8. Серый 16 бит.tif"),
                    ("Г 149", "Глубоко", "вложенная папка", "7. Многостраничный.TIFF")]:
            with Image.open(s(*rel)) as a, Image.open(p(*rel)) as b:
                for i in range(getattr(a, "n_frames", 1)):
                    a.seek(i)
                    b.seek(i)
                    self.assertEqual(a.tobytes(), b.tobytes(), rel)
            self.assertLessEqual(os.path.getsize(p(*rel)), os.path.getsize(s(*rel)), rel)
        with Image.open(p("Г 149", "15. Г 149 отраженный свет.PNG")) as im:
            self.assertEqual(im.info.get("icc_profile"), self.icc)
            self.assertAlmostEqual(im.info["dpi"][0], 300, delta=0.1)
        with Image.open(p("Г 144", "13. Прозрачный.png")) as im:
            self.assertEqual(im.text.get("Description"), "Образец Г 144")
        with Image.open(p("Г 149", "2. Образец 35.tif")) as im:
            self.assertEqual(im.info.get("dpi"), (600, 600))
            self.assertEqual(im.info.get("compression"), "tiff_adobe_deflate")
        # 16-битный цветной PNG и BMP — без изменений
        for rel in [("Г 149", "9. Цветной 16 бит.png"), ("Г 144", "12. Растр.bmp"), ("Г 144", "14. Повреждённый.jpg")]:
            self.assertEqual(file_hash(p(*rel)), file_hash(s(*rel)), rel)

        self.assertEqual(stats.errors, 1, logs)  # повреждённый JPEG
        self.assertEqual(stats.images_processed, 11)
        self.assertEqual(stats.others_copied, 2)
        self.assertEqual(stats.images_copied, 5)
        self.assertFalse(stats.cancelled)
        self.assertGreater(stats.saved, 0)
        real_new = sum(os.path.getsize(os.path.join(out, e.rel_path))
                       for e in self.scan.images if e.is_selected(MODE_NUMBERED))
        self.assertEqual(stats.new_images_size, real_new)
        self.assertEqual(stats.orig_images_size, sum(e.size for e in self.scan.images if e.is_selected(MODE_NUMBERED)))

        # повторный запуск в ту же папку — все файлы обнаружены как существующие
        self.assertEqual(len(find_existing(self.scan, out)), len(expected))

    def test_process_other(self):
        out, stats, logs = self.run_mode(MODE_OTHER, "Обработанные Б")
        self.assertEqual(tree_snapshot(self.src), self.snapshot_before)
        out_files = {os.path.relpath(os.path.join(c, f), out) for c, _, fs in os.walk(out) for f in fs}
        self.assertEqual(out_files, {e.rel_path for e in self.scan.files})
        self.assertEqual(stats.errors, 0, logs)
        self.assertEqual(stats.images_processed, 5)
        self.assertEqual(stats.images_copied, 11)
        # повреждённый JPEG (исключён фильтром) просто скопирован — это не ошибка
        bad = os.path.join("Г 144", "14. Повреждённый.jpg")
        self.assertEqual(file_hash(os.path.join(out, bad)), file_hash(os.path.join(self.src, bad)))

    def test_manual_overrides(self):
        removed = os.path.join("Г 144", "93. Г 144 скрещенные.jpg")  # подходит под А, убран вручную
        added = os.path.join("Г 144", "4_Г 144_отраж_5х.jpg")  # не подходит под А, добавлен вручную
        overrides = {removed: False, added: True}
        by_path = {e.rel_path: e for e in self.scan.files}
        self.assertFalse(by_path[removed].is_selected(MODE_NUMBERED, overrides))
        self.assertTrue(by_path[added].is_selected(MODE_NUMBERED, overrides))

        out = os.path.join(self.tmp, "Ручной список")
        stats = process(self.scan, MODE_NUMBERED, out, 92, overrides=overrides)
        self.assertEqual(tree_snapshot(self.src), self.snapshot_before)
        # убранное изображение скопировано байт в байт, добавленное — сжато
        self.assertEqual(file_hash(os.path.join(out, removed)), file_hash(os.path.join(self.src, removed)))
        self.assertLess(os.path.getsize(os.path.join(out, added)), os.path.getsize(os.path.join(self.src, added)))
        self.assertEqual(stats.images_processed, 11)  # 11 по фильтру − 1 убранное + 1 добавленное
        self.assertEqual(stats.images_copied, 5)
        self.assertEqual(stats.orig_images_size,
                         sum(e.size for e in self.scan.images if e.is_selected(MODE_NUMBERED, overrides)))

    def test_cancel(self):
        import threading
        ev = threading.Event()
        calls = []

        def on_progress(done, total, cur):
            calls.append(done)
            if done == 3:
                ev.set()

        out = os.path.join(self.tmp, "Отмена")
        stats = process(self.scan, MODE_NUMBERED, out, 92, cancel_event=ev, on_progress=on_progress)
        self.assertTrue(stats.cancelled)
        self.assertEqual(stats.done_files, 4)
        self.assertLess(stats.done_files, stats.total_files)
        n_out = sum(len(fs) for _, _, fs in os.walk(out))
        self.assertEqual(n_out, 4)  # уже сохранённые файлы не удалены

    def test_quality_levels(self):
        sizes = []
        for q in (95, 92, 90):
            out = os.path.join(self.tmp, f"q{q}")
            process(self.scan, MODE_OTHER, out, q)
            sizes.append(os.path.getsize(os.path.join(out, "Г 144", "4_Г 144_отраж_5х.jpg")))
        self.assertGreater(sizes[0], sizes[1])
        self.assertGreater(sizes[1], sizes[2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
