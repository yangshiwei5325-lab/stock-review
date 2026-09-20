@echo off
REM ============================================================
REM  A股每日复盘 —— 生成报告并推送到飞书
REM  供 Windows 任务计划程序调用，也可双击手动运行
REM ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM ---- 挑选解释器：优先 Python 3，回退 Python 2.7（脚本两者都兼容）----
set "PYEXE="
py -3 -c "import sys" >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE python -c "import sys" >nul 2>nul && set "PYEXE=python"
if not defined PYEXE python3 -c "import sys" >nul 2>nul && set "PYEXE=python3"

if not defined PYEXE (
    echo [错误] 没有找到可用的 Python 解释器。
    exit /b 1
)

REM ---- 日志目录（任务计划里出问题全靠它排查）----
if not exist "logs" mkdir "logs"

echo. >> "logs\daily_push.log"
echo ============================================================ >> "logs\daily_push.log"
echo [%date% %time%] 开始（解释器: %PYEXE%） >> "logs\daily_push.log"

REM -B 禁用 .pyc，避免残留字节码干扰；--push 生成并推送
%PYEXE% -B "stock_review.py" --push %* >> "logs\daily_push.log" 2>&1
set "RC=%ERRORLEVEL%"

echo [%date% %time%] 结束，退出码 = %RC% >> "logs\daily_push.log"
exit /b %RC%
