@echo off
REM ============================================================
REM  一键复盘 + 飞书推送
REM  运行前请先编辑 push_config.json 填好飞书机器人 Webhook。
REM  调试时先跑一次  一键复盘并推送.bat --push-dry-run  看内容。
REM ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYEXE="
py -3 -c "import sys" >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE python -c "import sys" >nul 2>nul && set "PYEXE=python"
if not defined PYEXE python3 -c "import sys" >nul 2>nul && set "PYEXE=python3"

if not defined PYEXE (
    echo.
    echo [错误] 没有找到 Python 解释器。
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   A股复盘 + 飞书推送   ^(解释器: %PYEXE%^)
echo ============================================================
echo.

%PYEXE% -B "stock_review.py" --push %*
set "RC=%ERRORLEVEL%"

echo.
echo 报告目录: %~dp0reports
echo.
pause
exit /b %RC%
