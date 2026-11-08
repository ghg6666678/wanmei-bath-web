@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   洗澡助手
echo ============================================
where python >nul 2>nul
if errorlevel 1 (
  echo 找不到 python，请先安装 Python 3.9+ 并加入 PATH
  pause
  exit /b 1
)
python server.py 8770
pause
