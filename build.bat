@echo off
chcp 65001 >nul
REM QuantDesk 一键打包（Windows）。产物在 dist\QuantDesk\
REM 首次使用前需安装 PyInstaller：pip install "pyinstaller>=6.10"

set PY=..\quant-desk\.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

echo [1/2] 清理旧产物...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [2/2] 构建中（约 1 分钟）...
"%PY%" -m PyInstaller --noconfirm --clean QuantDesk.spec || goto :fail

echo.
echo 构建完成：dist\QuantDesk\QuantDesk.exe
echo 自检：dist\QuantDesk\QuantDesk.exe --selftest
goto :eof

:fail
echo.
echo 构建失败，请查看上面的报错。
exit /b 1
