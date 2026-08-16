@echo off
REM Oopz Bot 一键部署（Windows），双击即可运行。
REM 实际逻辑都在 deploy.py，这里只负责找到一个够新的 Python 并把参数透传过去。
setlocal
cd /d "%~dp0"
chcp 65001 >nul

set "PY_CMD="
set "VER_CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"

REM py 启动器最可靠，它能直接挑出 3.10+ 的解释器。
REM 这里用 && 判断上一条命令的结果，避免依赖延迟展开。
py -3 -c "%VER_CHECK%" >nul 2>nul && set "PY_CMD=py -3"
if defined PY_CMD goto :run

python -c "%VER_CHECK%" >nul 2>nul && set "PY_CMD=python"
if defined PY_CMD goto :run

echo.
echo 找不到 Python 3.10 或更高版本。
echo.
echo 装法二选一：
echo   1. 微软商店搜索 "Python"，装 3.11 或更新的版本
echo   2. 命令行执行：winget install Python.Python.3.12
echo.
echo 安装时记得勾选 "Add Python to PATH"。装好后重新双击本文件。
echo.
pause
exit /b 1

:run
%PY_CMD% deploy.py %*
set "EXIT_CODE=%errorlevel%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo 脚本以退出码 %EXIT_CODE% 结束。
)
REM 双击运行时窗口会立刻关掉，留一下让用户看到结果
pause
exit /b %EXIT_CODE%
