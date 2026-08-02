@echo off
REM 启动 Web 仪表盘
chcp 65001 >nul
set PYTHON=D:\miniconda3\envs\quant_simple\python.exe
if not exist "%PYTHON%" set PYTHON=python
"%PYTHON%" -X utf8 run_web.py
pause
