@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "VENV=%~dp0.venv"
set "PY=%VENV%\Scripts\python.exe"

if exist "%PY%" goto check_deps

echo Первый запуск: создание виртуального окружения...
py -3 -m venv "%VENV%" >nul 2>&1
if not exist "%PY%" python -m venv "%VENV%"
if not exist "%PY%" goto no_python

:check_deps
"%PY%" -c "import PySide6, PIL" >nul 2>&1
if not errorlevel 1 goto run
echo Установка зависимостей (PySide6, Pillow), это может занять несколько минут...
"%PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto pip_failed

:run
start "" "%VENV%\Scripts\pythonw.exe" "%~dp0main.py"
exit /b 0

:no_python
echo.
echo Не найден Python 3.11-3.13. Установите его с https://www.python.org/downloads/
echo (при установке отметьте "Add python.exe to PATH") и запустите run.bat снова.
pause
exit /b 1

:pip_failed
echo.
echo Не удалось установить зависимости. Проверьте подключение к интернету и запустите run.bat снова.
pause
exit /b 1
