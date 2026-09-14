@echo off
REM ============================================================
REM  A股每日复盘 —— 一键运行
REM  双击本文件即可；也可以把参数跟在后面，例如：
REM      一键复盘.bat --no-news
REM      一键复盘.bat --refresh
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
    echo        请先安装 Python 3（安装时勾选 "Add Python to PATH"）。
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   A股每日复盘   ^(解释器: %PYEXE%^)
echo ============================================================
echo.

%PYEXE% -B "stock_review.py" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo 报告目录: %~dp0reports
    if exist "%~dp0reports" start "" "%~dp0reports"
) else (
    echo [失败] 退出码 %RC%，请查看上方日志。
)
echo.
pause
exit /b %RC%
