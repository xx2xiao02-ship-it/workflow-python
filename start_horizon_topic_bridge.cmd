@echo off
setlocal
cd /d "%~dp0"

set "HORIZON_ROOT=%~dp0tmp\Horizon"
set "HORIZON_CONFIG=%~dp0tmp\Horizon\data\config.json"
set "HORIZON_BRIDGE_HOST=127.0.0.1"
set "HORIZON_BRIDGE_PORT=8791"

if not exist "%HORIZON_ROOT%\.venv\Scripts\python.exe" (
  echo Horizon 隔离环境不存在：%HORIZON_ROOT%\.venv\Scripts\python.exe
  exit /b 1
)

echo 启动 Horizon 选题测试服务：http://%HORIZON_BRIDGE_HOST%:%HORIZON_BRIDGE_PORT%
"%HORIZON_ROOT%\.venv\Scripts\python.exe" "%~dp0tools\horizon_topic_bridge.py"
endlocal
