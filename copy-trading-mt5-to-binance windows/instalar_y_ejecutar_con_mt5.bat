@echo off
echo ============================================
echo Instalador y Arranque BOT MT5 + MetaTrader 5
echo ============================================
echo.

:: Verificar si Python está instalado
python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo Python no encontrado. Por favor instala Python 3.9 a 3.11 antes de continuar.
    pause
    exit /b
)

:: Crear entorno virtual (si no existe)
IF NOT EXIST venv (
    echo Creando entorno virtual...
    python -m venv venv
)

:: Activar entorno virtual
call venv\Scripts\activate

:: Actualizar pip
python -m pip install --upgrade pip

:: Instalar dependencias necesarias
echo Instalando dependencias necesarias...
pip install python-binance MetaTrader5 colorama requests urllib3

echo.
echo ============================================
echo Dependencias instaladas correctamente
echo Abriendo MetaTrader 5...
echo ============================================

:: 👉 IMPORTANTE: Ajusta la ruta de terminal64.exe según tu instalación de MT5
start "" "C:\Program Files\MetaTrader 5\terminal64.exe"

timeout /t 5 >nul

echo ============================================
echo Iniciando BOT MT5 -> Binance...
echo ============================================

:: Abrir el bot en una ventana aparte
start cmd /k "call venv\Scripts\activate && python COPYMT5TOBINANCE.py"
