"""Главное окно программы (PySide6)."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageOps
from PySide6.QtCore import QObject, QSize, Qt, QThread, Signal
from PySide6.QtGui import QAction, QFont, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSplitter,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME
from .processing import ProcessStats, find_existing, process
from .scanner import MODE_NUMBERED, MODE_OTHER, ScanResult, scan_folder
from .utils import format_dpi, format_mb, format_size, is_same_or_nested, long_path

QUALITY_OPTIONS = [
    (95, "95 — максимальное сохранение качества"),
    (92, "92 — стандартный режим"),
    (90, "90 — более сильное сжатие"),
]
DEFAULT_QUALITY_INDEX = 1
PREVIEW_MAX = 900
ENTRY_ROLE = Qt.ItemDataRole.UserRole
FOLDER_ROLE = Qt.ItemDataRole.UserRole + 1


# ======================================================================= потоки


class ScanThread(QThread):
    progress = Signal(int)
    finished_scan = Signal(object)
    failed = Signal(str)

    def __init__(self, root: str, parent=None):
        super().__init__(parent)
        self.root = root

    def run(self):
        try:
            result = scan_folder(self.root, progress=self.progress.emit, cancelled=self.isInterruptionRequested)
            self.finished_scan.emit(result)
        except Exception as e:
            self.failed.emit(str(e) or e.__class__.__name__)


class ProcessThread(QThread):
    progress = Signal(int, int, str)
    log = Signal(str)
    finished_processing = Signal(object)

    def __init__(self, scan: ScanResult, mode: str, out_root: str, quality: int, overrides: dict, parent=None):
        super().__init__(parent)
        self.scan, self.mode, self.out_root, self.quality = scan, mode, out_root, quality
        self.overrides = dict(overrides)
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            stats = process(
                self.scan, self.mode, self.out_root, self.quality,
                overrides=self.overrides,
                cancel_event=self.cancel_event,
                on_progress=self.progress.emit,
                on_log=self.log.emit,
            )
        except Exception as e:  # непредвиденная ошибка — интерфейс не должен «зависнуть»
            self.log.emit(f"ОШИБКА: обработка остановлена: {e}")
            stats = ProcessStats(cancelled=True, errors=1)
        self.finished_processing.emit(stats)


def _load_preview(path: str) -> QImage:
    """Уменьшенная копия изображения для просмотра (не держит оригинал в памяти)."""
    with Image.open(long_path(path)) as img:
        img.draft("RGB", (PREVIEW_MAX, PREVIEW_MAX))  # быстрое уменьшение для JPEG
        img.seek(0)
        if img.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
            img = img.convert("I").point(lambda v: v * (1 / 256)).convert("L")
        img.thumbnail((PREVIEW_MAX, PREVIEW_MAX))
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGBA")
        data = img.tobytes("raw", "RGBA")
        qimg = QImage(data, img.width, img.height, img.width * 4, QImage.Format.Format_RGBA8888)
        return qimg.copy()  # отвязать от буфера data


class PreviewLoader(QObject):
    """Загружает предпросмотр в фоновом потоке; устаревшие запросы игнорируются."""

    loaded = Signal(int, QImage, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._request = 0

    def request(self, path: str) -> int:
        self._request += 1
        req = self._request
        self._pool.submit(self._work, req, path)
        return req

    def _work(self, req: int, path: str):
        if req != self._request:
            return
        try:
            self.loaded.emit(req, _load_preview(path), "")
        except Exception as e:
            self.loaded.emit(req, QImage(), str(e) or e.__class__.__name__)

    def shutdown(self):
        self._request += 1
        self._pool.shutdown(wait=False, cancel_futures=True)


# ======================================================================= виджеты


class PreviewLabel(QLabel):
    """Картинка, масштабируемая под размер панели с сохранением пропорций."""

    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._image = QImage()

    def set_image(self, image: QImage):
        self._image = image
        self.setText("")
        self._update_pixmap()

    def set_message(self, text: str):
        self._image = QImage()
        self.setPixmap(QPixmap())
        self.setText(text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_pixmap()

    def _update_pixmap(self):
        if self._image.isNull():
            return
        size = self.contentsRect().size()
        pix = QPixmap.fromImage(self._image)
        if pix.width() > size.width() or pix.height() > size.height():
            pix = pix.scaled(size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.setPixmap(pix)


# ======================================================================= окно


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon))
        self.resize(1200, 800)

        self.scan: ScanResult | None = None
        # ручные изменения списка: {относительный путь: True — добавлено, False — убрано}
        self.overrides: dict[str, bool] = {}
        self._mode = MODE_NUMBERED
        self.scan_thread: ScanThread | None = None
        self.process_thread: ProcessThread | None = None
        self.preview_request = 0
        self.preview_loader = PreviewLoader(self)
        self.preview_loader.loaded.connect(self._on_preview_loaded)

        self._build_ui()
        self._update_state()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        self.setCentralWidget(central)

        # --- папки
        folders = QGroupBox("Папки")
        grid = QGridLayout(folders)
        self.src_edit = QLineEdit(readOnly=True, placeholderText="Исходная папка не выбрана")
        self.out_edit = QLineEdit(readOnly=True, placeholderText="Выходная папка не выбрана")
        self.src_btn = QPushButton("Выбрать исходную папку…")
        self.out_btn = QPushButton("Выбрать выходную папку…")
        self.src_btn.clicked.connect(self.choose_source)
        self.out_btn.clicked.connect(self.choose_output)
        grid.addWidget(QLabel("Исходная папка:"), 0, 0)
        grid.addWidget(self.src_edit, 0, 1)
        grid.addWidget(self.src_btn, 0, 2)
        grid.addWidget(QLabel("Выходная папка:"), 1, 0)
        grid.addWidget(self.out_edit, 1, 1)
        grid.addWidget(self.out_btn, 1, 2)
        grid.setColumnStretch(1, 1)
        root.addWidget(folders)

        # --- режим отбора
        modes = QGroupBox("Отбор изображений по имени")
        mode_layout = QHBoxLayout(modes)
        self.mode_a = QRadioButton("Вариант А — имена с номером и точкой  (например, «93. Г 144 скрещенные.jpg»)")
        self.mode_b = QRadioButton("Вариант Б — все остальные имена  (например, «4_Г 144_отраж_5х.jpg»)")
        self.mode_a.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.mode_a)
        self.mode_group.addButton(self.mode_b)
        self.mode_group.buttonToggled.connect(self._on_mode_changed)
        mode_layout.addWidget(self.mode_a)
        mode_layout.addWidget(self.mode_b)
        mode_layout.addStretch(1)
        root.addWidget(modes)

        # --- дерево и предпросмотр
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Имя файла", "Размер", "Разрешение", "DPI"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QSize(16, 16))
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)
        self.tree.currentItemChanged.connect(self._on_current_item)
        self.tree.itemSelectionChanged.connect(self._update_list_buttons)

        self.remove_action = QAction("Убрать из списка", self)
        self.remove_action.setShortcut(QKeySequence(QKeySequence.StandardKey.Delete))
        self.remove_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self.remove_action.triggered.connect(self.remove_selected)
        self.add_action = QAction("Добавить из этой папки…", self)
        self.add_action.setShortcut(QKeySequence("Insert"))
        self.add_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self.add_action.triggered.connect(self.add_from_folder)
        self.tree.addAction(self.remove_action)
        self.tree.addAction(self.add_action)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)

        self.remove_btn = QPushButton("Убрать из списка")
        self.remove_btn.setToolTip("Выделенные изображения не будут сжиматься — "
                                   "в выходную папку попадут их исходные файлы (клавиша Delete)")
        self.remove_btn.clicked.connect(self.remove_selected)
        self.add_btn = QPushButton("Добавить из этой папки…")
        self.add_btn.setToolTip("Добавить в список изображения из папки выделенного элемента (клавиша Insert)")
        self.add_btn.clicked.connect(self.add_from_folder)
        list_buttons = QHBoxLayout()
        list_buttons.setContentsMargins(0, 0, 0, 0)
        list_buttons.addWidget(self.remove_btn)
        list_buttons.addWidget(self.add_btn)
        list_buttons.addStretch(1)
        tree_box = QWidget()
        tl = QVBoxLayout(tree_box)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.tree, 1)
        tl.addLayout(list_buttons)

        preview_box = QWidget()
        pv = QVBoxLayout(preview_box)
        pv.setContentsMargins(0, 0, 0, 0)
        self.preview = PreviewLabel()
        self.preview.set_message("Выберите изображение\nдля предварительного просмотра")
        self.preview_info = QLabel()
        self.preview_info.setWordWrap(True)
        self.preview_info.setTextFormat(Qt.TextFormat.RichText)
        self.preview_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pv.addWidget(self.preview, 1)
        pv.addWidget(self.preview_info)

        center = QSplitter(Qt.Orientation.Horizontal)
        center.addWidget(tree_box)
        center.addWidget(preview_box)
        center.setStretchFactor(0, 3)
        center.setStretchFactor(1, 2)
        center.setSizes([720, 420])

        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setPlaceholderText("Журнал обработки")
        self.log_view.setMaximumBlockCount(20000)

        vsplit = QSplitter(Qt.Orientation.Vertical)
        vsplit.addWidget(center)
        vsplit.addWidget(self.log_view)
        vsplit.setStretchFactor(0, 4)
        vsplit.setStretchFactor(1, 1)
        vsplit.setSizes([560, 120])
        root.addWidget(vsplit, 1)

        # --- нижняя панель
        self.summary_label = QLabel()
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(self.summary_label)

        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("Качество JPEG / WEBP:"))
        self.quality_combo = QComboBox()
        for value, text in QUALITY_OPTIONS:
            self.quality_combo.addItem(text, value)
        self.quality_combo.setCurrentIndex(DEFAULT_QUALITY_INDEX)
        bottom.addWidget(self.quality_combo)
        bottom.addSpacing(16)
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setValue(0)
        bottom.addWidget(self.progress, 1)
        self.start_btn = QPushButton("Начать обработку")
        self.start_btn.setDefault(True)
        self.start_btn.setMinimumWidth(150)
        self.start_btn.clicked.connect(self.start_processing)
        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.clicked.connect(self.cancel_processing)
        bottom.addWidget(self.start_btn)
        bottom.addWidget(self.cancel_btn)
        root.addLayout(bottom)

        self.status_label = QLabel("Выберите исходную папку.")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.status_label)

    # ------------------------------------------------------------------ состояние

    @property
    def mode(self) -> str:
        return MODE_NUMBERED if self.mode_a.isChecked() else MODE_OTHER

    def _busy(self) -> bool:
        return bool(
            (self.scan_thread and self.scan_thread.isRunning())
            or (self.process_thread and self.process_thread.isRunning())
        )

    def _update_state(self):
        busy = self._busy()
        processing = bool(self.process_thread and self.process_thread.isRunning())
        for w in (self.src_btn, self.out_btn, self.mode_a, self.mode_b, self.quality_combo):
            w.setEnabled(not busy)
        self.start_btn.setEnabled(not busy and self.scan is not None and bool(self.out_edit.text()))
        self.cancel_btn.setEnabled(processing and not self.process_thread.cancel_event.is_set())
        self._update_list_buttons()

    def _update_list_buttons(self):
        can_edit = self.scan is not None and not self._busy()
        has_selection = bool(self.tree.selectedItems())
        self.remove_action.setEnabled(can_edit and has_selection)
        self.remove_btn.setEnabled(can_edit and has_selection)
        self.add_action.setEnabled(can_edit)
        self.add_btn.setEnabled(can_edit)

    def _log(self, text: str):
        self.log_view.appendPlainText(text)

    # ------------------------------------------------------------------ выбор папок

    def choose_source(self):
        path = QFileDialog.getExistingDirectory(self, "Выберите исходную папку", self.src_edit.text())
        if not path:
            return
        path = os.path.normpath(path)
        out = self.out_edit.text()
        if out and is_same_or_nested(path, out):
            QMessageBox.warning(self, APP_NAME, "Исходная и выходная папки не должны совпадать "
                                "или находиться одна внутри другой.\nВыберите другую исходную папку.")
            return
        self.src_edit.setText(path)
        self.start_scan(path)

    def choose_output(self):
        start = self.out_edit.text() or os.path.dirname(self.src_edit.text())
        path = QFileDialog.getExistingDirectory(self, "Выберите выходную папку", start)
        if not path:
            return
        path = os.path.normpath(path)
        src = self.src_edit.text()
        if src and is_same_or_nested(src, path):
            QMessageBox.warning(self, APP_NAME, "Исходная и выходная папки не должны совпадать "
                                "или находиться одна внутри другой.\nВыберите другую выходную папку.")
            return
        self.out_edit.setText(path)
        self._update_state()

    # ------------------------------------------------------------------ сканирование

    def start_scan(self, path: str):
        self.scan = None
        self.tree.clear()
        self._clear_preview()
        self.summary_label.setText("")
        self.progress.setRange(0, 0)  # «бегущий» индикатор
        self.status_label.setText("Сканирование папки…")
        self.scan_thread = ScanThread(path, self)
        self.scan_thread.progress.connect(lambda n: self.status_label.setText(f"Сканирование папки… найдено файлов: {n}"))
        self.scan_thread.finished_scan.connect(self._on_scan_finished)
        self.scan_thread.failed.connect(self._on_scan_failed)
        self.scan_thread.finished.connect(self._update_state)
        self.scan_thread.start()
        self._update_state()

    def _on_scan_finished(self, result: ScanResult):
        self.scan = result
        self.overrides.clear()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.log_view.clear()
        self._log(f"Просканирована папка: {result.root}")
        self._log(f"Папок: {len(result.dirs)}, файлов: {len(result.files)}, изображений: {len(result.images)}")
        for msg in result.errors:
            self._log("ОШИБКА: " + msg)
        unreadable = [e for e in result.images if e.info_error]
        for e in unreadable:
            self._log(f"Не удалось прочитать заголовок изображения: {e.rel_path} ({e.info_error})")
        self.status_label.setText("Сканирование завершено.")
        self.rebuild_tree()
        self._update_state()

    def _on_scan_failed(self, message: str):
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status_label.setText("Ошибка сканирования.")
        QMessageBox.critical(self, APP_NAME, f"Не удалось просканировать папку:\n{message}")

    # ------------------------------------------------------------------ дерево

    def _on_mode_changed(self, _button, checked: bool):
        if not checked:
            return
        new_mode = self.mode
        if new_mode == self._mode:
            return
        if self.overrides:
            answer = QMessageBox.question(
                self, APP_NAME,
                "При смене режима отбора ручные изменения списка (добавленные и убранные "
                "изображения) будут сброшены.\nПродолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                self.mode_group.blockSignals(True)
                (self.mode_a if self._mode == MODE_NUMBERED else self.mode_b).setChecked(True)
                self.mode_group.blockSignals(False)
                return
            self.overrides.clear()
        self._mode = new_mode
        if self.scan is not None:
            self.rebuild_tree()

    def _set_in_list(self, entry, in_list: bool):
        """Добавить изображение в список или убрать из него (с учётом фильтра по имени)."""
        if entry.matches_filter(self.mode) == in_list:
            self.overrides.pop(entry.rel_path, None)
        else:
            self.overrides[entry.rel_path] = in_list

    def _entries_under(self, item: QTreeWidgetItem) -> list:
        entry = item.data(0, ENTRY_ROLE)
        if entry is not None:
            return [entry]
        result = []
        for i in range(item.childCount()):
            result += self._entries_under(item.child(i))
        return result

    def remove_selected(self):
        if self.scan is None or self._busy():
            return
        entries = {}
        for item in self.tree.selectedItems():
            for entry in self._entries_under(item):
                entries[entry.rel_path] = entry
        if not entries:
            return
        if any(item.data(0, ENTRY_ROLE) is None for item in self.tree.selectedItems()) and len(entries) > 1:
            answer = QMessageBox.question(
                self, APP_NAME,
                f"Убрать из списка все изображения выделенных папок ({len(entries)} шт.)?\n"
                "Они не будут сжиматься — в выходную папку попадут исходные файлы.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
            if answer != QMessageBox.StandardButton.Yes:
                return
        for entry in entries.values():
            self._set_in_list(entry, False)
        self._log(f"Убрано из списка: {len(entries)} — будут скопированы без сжатия.")
        self.rebuild_tree(keep_scroll=True)

    def _current_folder(self) -> str:
        item = self.tree.currentItem()
        if item is None:
            selected = self.tree.selectedItems()
            item = selected[0] if selected else None
        if item is None:
            return ""
        entry = item.data(0, ENTRY_ROLE)
        if entry is not None:
            return entry.rel_dir
        return item.data(0, FOLDER_ROLE) or ""

    def add_from_folder(self):
        if self.scan is None or self._busy():
            return
        rel_dir = self._current_folder()
        candidates = [e for e in self.scan.images
                      if e.rel_dir == rel_dir and not e.is_selected(self.mode, self.overrides)]
        folder_name = rel_dir or os.path.basename(self.scan.root) or self.scan.root
        if not candidates:
            QMessageBox.information(self, APP_NAME,
                                    f"В папке «{folder_name}» все изображения уже в списке.")
            return
        dialog = AddImagesDialog(folder_name, candidates, self.mode, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dialog.chosen()
        for entry in chosen:
            self._set_in_list(entry, True)
        if chosen:
            self._log(f"Добавлено в список из папки «{folder_name}»: {len(chosen)}.")
            self.rebuild_tree(keep_scroll=True, select=[e.rel_path for e in chosen])

    def rebuild_tree(self, keep_scroll: bool = False, select: list | None = None):
        scroll = self.tree.verticalScrollBar().value()
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        self._clear_preview()
        scan = self.scan
        if scan is None:
            self.tree.setUpdatesEnabled(True)
            return
        mode, overrides = self.mode, self.overrides
        dir_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        file_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        italic = QFont(self.tree.font())
        italic.setItalic(True)
        folders: dict[str, QTreeWidgetItem] = {}
        folder_stats: dict[str, list] = {}  # [в списке, размер, не в списке]

        def folder_item(rel_dir: str) -> QTreeWidgetItem:
            if rel_dir in folders:
                return folders[rel_dir]
            parent_rel = os.path.dirname(rel_dir)
            parent = folder_item(parent_rel) if parent_rel else self.tree.invisibleRootItem()
            item = QTreeWidgetItem(parent, [os.path.basename(rel_dir)])
            item.setIcon(0, dir_icon)
            item.setData(0, FOLDER_ROLE, rel_dir)
            folders[rel_dir] = item
            folder_stats[rel_dir] = [0, 0, 0]
            return item

        selected = excluded = 0
        selected_size = 0
        to_select = set(select or ())
        select_items = []
        for entry in scan.images:
            in_list = entry.is_selected(mode, overrides)
            # папки с изображениями показываются всегда, чтобы в них можно было добавлять файлы
            parent = folder_item(entry.rel_dir) if entry.rel_dir else self.tree.invisibleRootItem()
            if not in_list:
                excluded += 1
                d = entry.rel_dir
                while d:
                    folder_stats[d][2] += 1
                    d = os.path.dirname(d)
                continue
            selected += 1
            selected_size += entry.size
            if entry.info_error:
                res, dpi = "не удалось прочитать", ""
            else:
                res, dpi = f"{entry.width} × {entry.height} px", format_dpi(entry.dpi)
            item = QTreeWidgetItem(parent, [entry.name, format_mb(entry.size), res, dpi])
            item.setIcon(0, file_icon)
            item.setData(0, ENTRY_ROLE, entry)
            tip = entry.rel_path
            if entry.rel_path in overrides:
                item.setFont(0, italic)
                tip += "\nДобавлено в список вручную"
            if entry.info_error:
                tip += f"\nОшибка: {entry.info_error}"
            item.setToolTip(0, tip)
            for col in (1, 2, 3):
                item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if entry.rel_path in to_select:
                select_items.append(item)
            # итоги по папкам (включая вложенные)
            d = entry.rel_dir
            while d:
                folder_stats[d][0] += 1
                folder_stats[d][1] += entry.size
                d = os.path.dirname(d)

        for rel_dir, item in folders.items():
            count, size, outside = folder_stats[rel_dir]
            item.setText(1, f"{count} шт. · {format_size(size)}")
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if outside:
                item.setText(2, f"не в списке: {outside}")
                item.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.tree.expandAll()
        for item in select_items:
            item.setSelected(True)
        if select_items:
            self.tree.setCurrentItem(select_items[0])
        self.tree.setUpdatesEnabled(True)
        if keep_scroll and not select_items:
            self.tree.verticalScrollBar().setValue(scroll)
        elif select_items:
            self.tree.scrollToItem(select_items[0])

        added = sum(1 for v in overrides.values() if v)
        removed = len(overrides) - added
        manual = ""
        if overrides:
            manual = (f" &nbsp;&nbsp;|&nbsp;&nbsp; Вручную: добавлено <b>{added}</b>, "
                      f"убрано <b>{removed}</b>")
        sep = " &nbsp;&nbsp;|&nbsp;&nbsp; "
        self.summary_label.setText(
            f"Всего найдено изображений: <b>{len(scan.images)}</b>{sep}"
            f"Выбрано для сжатия: <b>{selected}</b>{sep}"
            f"Общий размер выбранных: <b>{format_size(selected_size)}</b>{sep}"
            f"Исключено (копируются без сжатия): <b>{excluded}</b>{manual}{sep}"
            f"Прочих файлов: <b>{len(scan.others)}</b>"
        )
        self._update_list_buttons()

    # ------------------------------------------------------------------ предпросмотр

    def _clear_preview(self):
        self.preview_request = -1
        self.preview.set_message("Выберите изображение\nдля предварительного просмотра")
        self.preview_info.setText("")

    def _on_current_item(self, item: QTreeWidgetItem | None, _prev):
        entry = item.data(0, ENTRY_ROLE) if item is not None else None
        if entry is None or self.scan is None:
            self._clear_preview()
            return
        parts = [format_mb(entry.size)]
        if entry.width:
            parts.append(f"{entry.width} × {entry.height} px")
        if entry.dpi:
            parts.append(format_dpi(entry.dpi))
        self.preview_info.setText(f"<b>{_html(entry.name)}</b><br>{' | '.join(parts)}")
        self.preview.set_message("Загрузка…")
        self.preview_request = self.preview_loader.request(os.path.join(self.scan.root, entry.rel_path))

    def _on_preview_loaded(self, req: int, image: QImage, error: str):
        if req != self.preview_request:
            return
        if image.isNull():
            self.preview.set_message(f"Не удалось показать изображение\n{error}")
        else:
            self.preview.set_image(image)

    # ------------------------------------------------------------------ обработка

    def start_processing(self):
        if self._busy() or self.scan is None:
            return
        out = self.out_edit.text()
        if not out:
            QMessageBox.warning(self, APP_NAME, "Выберите выходную папку.")
            return
        if is_same_or_nested(self.scan.root, out):
            QMessageBox.warning(self, APP_NAME, "Исходная и выходная папки не должны совпадать "
                                "или находиться одна внутри другой.")
            return
        mode = self.mode
        selected = [e for e in self.scan.images if e.is_selected(mode, self.overrides)]
        if not selected:
            answer = QMessageBox.question(
                self, APP_NAME,
                "Список изображений для сжатия пуст.\n"
                "Продолжить? Все файлы будут скопированы без изменений.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        existing = find_existing(self.scan, out)
        if existing:
            examples = "\n".join("• " + p for p in existing[:5])
            more = f"\n… и ещё {len(existing) - 5}" if len(existing) > 5 else ""
            box = QMessageBox(QMessageBox.Icon.Warning, APP_NAME,
                              f"В выходной папке уже существуют файлы с такими же именами: {len(existing)}.\n\n"
                              f"{examples}{more}\n\nПерезаписать их?", parent=self)
            overwrite = box.addButton("Перезаписать", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is not overwrite:
                return

        quality = self.quality_combo.currentData()
        self._log("")
        self._log(f"Начало обработки. Режим: {'А' if mode == MODE_NUMBERED else 'Б'}, качество JPEG/WEBP: {quality}.")
        self._log(f"Выходная папка: {out}")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        if self.overrides:
            added = sum(1 for v in self.overrides.values() if v)
            self._log(f"Ручные изменения списка: добавлено {added}, убрано {len(self.overrides) - added}.")
        self.process_thread = ProcessThread(self.scan, mode, out, quality, self.overrides, self)
        self.process_thread.progress.connect(self._on_progress)
        self.process_thread.log.connect(self._log)
        self.process_thread.finished_processing.connect(self._on_processing_finished)
        self.process_thread.finished.connect(self._update_state)
        self.process_thread.start()
        self._update_state()

    def cancel_processing(self):
        if self.process_thread and self.process_thread.isRunning():
            self.process_thread.cancel()
            self.status_label.setText("Отмена… дожидаемся завершения текущего файла.")
            self._update_state()

    def _on_progress(self, done: int, total: int, current: str):
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        self.progress.setFormat(f"{done} из {total}")
        if current and not (self.process_thread and self.process_thread.cancel_event.is_set()):
            self.status_label.setText(f"Обработано файлов: {done} из {total}.  Текущий файл: {current}")

    def _on_processing_finished(self, stats: ProcessStats):
        lines = []
        if stats.cancelled:
            lines.append(f"Обработка прервана. Обработано файлов: {stats.done_files} из {stats.total_files}.")
            lines.append("Уже сохранённые файлы не удалены, результат в выходной папке неполный.")
            lines.append("")
            orig_label = "Исходный размер обработанных изображений"
        else:
            orig_label = "Исходный размер выбранных изображений"
        lines += [
            f"{orig_label}: {format_size(stats.orig_images_size)}",
            f"Размер изображений после обработки: {format_size(stats.new_images_size)}",
            f"Экономия: {format_size(stats.saved)} ({stats.saved_percent:.1f}%)".replace(".", ","),
            f"Обработано изображений: {stats.images_processed} "
            f"(уменьшено: {stats.images_compressed}, сохранено без изменений: {stats.images_kept})",
            f"Скопировано изображений без сжатия (не в списке): {stats.images_copied}",
            f"Скопировано прочих файлов: {stats.others_copied}",
            f"Ошибок: {stats.errors}",
        ]
        text = "\n".join(lines)
        title = "Обработка прервана" if stats.cancelled else "Результат обработки"
        self._log("")
        self._log(f"=== {title} ===")
        self._log(text)
        self.status_label.setText(
            f"{title}: обработано файлов {stats.done_files} из {stats.total_files}, ошибок: {stats.errors}.")
        icon = QMessageBox.Icon.Warning if (stats.cancelled or stats.errors) else QMessageBox.Icon.Information
        if stats.errors:
            text += "\n\nПодробности ошибок — в журнале внизу окна."
        QMessageBox(icon, title, text, parent=self).exec()

    # ------------------------------------------------------------------ закрытие

    def closeEvent(self, event):
        if self.process_thread and self.process_thread.isRunning():
            answer = QMessageBox.question(
                self, APP_NAME,
                "Идёт обработка. Прервать её и закрыть программу?\n"
                "Уже обработанные файлы останутся в выходной папке.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.process_thread.cancel()
            self.process_thread.wait()
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.requestInterruption()
            self.scan_thread.wait()
        self.preview_loader.shutdown()
        event.accept()


class AddImagesDialog(QDialog):
    """Выбор изображений папки, которые нужно добавить в список на сжатие."""

    def __init__(self, folder_name: str, entries: list, mode: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Добавить изображения в список")
        self.resize(560, 420)
        self._entries = entries
        layout = QVBoxLayout(self)
        label = QLabel(f"Изображения папки «{folder_name}», которых нет в списке.\n"
                       "Отметьте те, которые нужно сжимать:")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.list = QListWidget()
        for entry in entries:
            reason = "убрано вручную" if entry.matches_filter(mode) else "исключено фильтром"
            item = QListWidgetItem(f"{entry.name}    ({format_mb(entry.size)}, {reason})")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.list.addItem(item)
        self.list.itemDoubleClicked.connect(self._toggle)
        layout.addWidget(self.list, 1)

        row = QHBoxLayout()
        all_btn = QPushButton("Отметить все")
        none_btn = QPushButton("Снять все")
        all_btn.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        none_btn.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        row.addWidget(all_btn)
        row.addWidget(none_btn)
        row.addStretch(1)
        layout.addLayout(row)

        buttons = QDialogButtonBox()
        buttons.addButton("Добавить", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Отмена", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _toggle(self, item: QListWidgetItem):
        checked = item.checkState() == Qt.CheckState.Checked
        item.setCheckState(Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked)

    def _set_all(self, state):
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)

    def chosen(self) -> list:
        return [e for i, e in enumerate(self._entries)
                if self.list.item(i).checkState() == Qt.CheckState.Checked]


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
