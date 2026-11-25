@echo off
rem QuantDesk Windows 一键构建：exe(onedir) + 安装器
rem 前置：Windows 10/11 + Python 3.13（与开发环境一致）+ Inno Setup 6（ISCC.exe 在 PATH 或默认路径）
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

if not exist .venv (
    python -m venv .venv || goto :err
)
call .venv\Scripts\activate
python -m pip install -U pip
python -m pip install -r requirements.txt pyinstaller pywebview || goto :err

python -m PyInstaller --noconfirm --clean packaging\quantdesk.spec || goto :err

set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC=iscc
%ISCC% packaging\installer.iss || goto :err

echo.
echo ============================================
echo  构建完成：
echo    onedir:  dist\QuantDesk\QuantDesk.exe
echo    安装器:  packaging\dist\QuantDesk-Setup-1.0.0.exe
echo ============================================
exit /b 0

:err
echo 构建失败（见上方日志）
exit /b 1
