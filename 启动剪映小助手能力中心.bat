@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动剪映小助手能力中心……
echo 浏览器地址：http://127.0.0.1:8766
set "CONSOLE_PYTHON=%~dp0services\douyin-monitor\.venv\Scripts\python.exe"
if not exist "%CONSOLE_PYTHON%" (
  echo 缺少项目专用 Python 环境，无法启动能力中心。
  pause
  exit /b 1
)
"%CONSOLE_PYTHON%" tools\local_service_governance.py start --service capcut_capability_center
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo 服务未启动；端口冲突时不会结束无关进程。
exit /b %RC%
