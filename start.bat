@echo off
setlocal
chcp 65001 >nul
title LingJingAPI
cd /d "%~dp0"

set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0"

if not exist "runtime\python\python.exe" (
    echo [启动失败] 客户端启动组件不完整：缺少 runtime\python\python.exe
    echo 请重新安装轻量客户端。AI 运行环境可在客户端打开后单独安装。
    pause
    exit /b 1
)

if not exist "app\gui\main_gateway.py" (
    echo [启动失败] 客户端程序文件不完整：缺少 app\gui\main_gateway.py
    pause
    exit /b 1
)

"runtime\python\python.exe" -s -u -B "app\gui\main_gateway.py"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo 客户端异常退出（错误码 %EXIT_CODE%）。
    echo 可运行 check-env.bat 检查环境，并查看 runtime\logs\ 中的日志。
    pause
)
exit /b %EXIT_CODE%
