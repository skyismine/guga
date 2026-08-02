@echo off
REM 命令行分析, 用法: run_analyze.bat 600519
chcp 65001 >nul
set PYTHON=D:\miniconda3\envs\quant_simple\python.exe
if not exist "%PYTHON%" set PYTHON=python
"%PYTHON%" -X utf8 run_analyze.py %*
pause
