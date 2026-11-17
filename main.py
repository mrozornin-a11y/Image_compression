"""Точка входа: Сортировка и сжатие изображений."""

import os
import sys
import traceback


def _show_fatal(exc_type, exc, tb):
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    sys.__stderr__ and sys.__stderr__.write(text)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is not None:
            QMessageBox.critical(None, "Непредвиденная ошибка", text[-3000:])
    except Exception:
        pass


def main() -> int:
    sys.excepthook = _show_fatal
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    from PySide6.QtWidgets import QApplication

    from image_sorter import APP_NAME
    from image_sorter.gui import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
