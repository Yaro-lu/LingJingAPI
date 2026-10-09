@echo off
setlocal
chcp 65001 >nul
title LingJingAPI - 环境检查
cd /d "%~dp0"

set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUTF8=1"

echo ========================================
echo LingJingAPI - 本机环境检查
echo ========================================
echo.

set "FAILED=0"
call :check_file "runtime\python\python.exe" "便携 Python"
call :check_file "runtime\ComfyUI\main.py" "ComfyUI"
call :check_file ".venv\Lib\site-packages\fastapi\__init__.py" "Python 依赖"
call :check_file ".venv\Lib\site-packages\torch\__init__.py" "Torch/CUDA 依赖"
call :check_file "bin\cloudflared.exe" "Cloudflare Tunnel"

if exist "runtime\python\python.exe" (
    echo.
    echo [运行测试]
    "runtime\python\python.exe" -s -B -c "import customtkinter, fastapi, PIL, pystray, requests, torch, uvicorn; assert torch.cuda.is_available(), 'Torch 无法使用 NVIDIA CUDA'; print('  [正常] 客户端依赖与 CUDA 可以加载:', torch.__version__, torch.version.cuda, torch.cuda.get_device_name(0))"
    if errorlevel 1 set "FAILED=1"
)

echo.
echo [显卡检查]
where nvidia-smi >nul 2>nul
if errorlevel 1 (
    echo   [提示] 未检测到 NVIDIA 驱动。本地 GPU 工作流暂时无法运行。
) else (
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
)

echo.
if "%FAILED%"=="0" (
    echo 检查完成：客户端与 GPU 运行环境正常。
) else (
    echo 检查完成：存在缺失项，请重新安装或修复运行环境。
)
pause
exit /b %FAILED%

:check_file
if exist %~1 (
    echo   [正常] %~2
) else (
    echo   [缺失] %~2 - %~1
    set "FAILED=1"
)
exit /b 0
