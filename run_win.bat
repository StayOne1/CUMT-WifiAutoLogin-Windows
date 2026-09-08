@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ======================================
echo   CUMT 校园网自动登录 (Windows 版)
echo ======================================

where uv >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 uv，请先安装 uv：
    echo   winget install astral-sh.uv
    echo   或访问 https://docs.astral.sh/uv/getting-started/installation/
    pause
    exit /b 1
)

echo [依赖] 正在检查/同步依赖...
uv sync

echo [启动] 正在启动程序...
uv run win_login_app.py %*
pause
