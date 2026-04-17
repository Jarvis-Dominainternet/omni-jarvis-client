@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

:: Instalar dependencias si faltan (silencioso)
python -X utf8 -c "import openwakeword, sounddevice, websockets, pystray" >nul 2>&1
if errorlevel 1 (
    echo Instalando dependencias...
    python -m pip install -r requirements.txt --quiet
)

:: Lanzar sin ventana de terminal (pythonw)
start "" pythonw -X utf8 "%~dp0client.py"
