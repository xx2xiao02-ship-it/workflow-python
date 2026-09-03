@echo off
setlocal

chcp 65001 >nul
cd /d "%~dp0.."
set "PROJECT_ROOT=%CD%"
if "%ACCOUNT_KNOWLEDGE_VAULT_PATH%"=="" (
  echo [blocked] 未配置 ACCOUNT_KNOWLEDGE_VAULT_PATH
  echo 请先在 PowerShell 执行：
  echo $env:ACCOUNT_KNOWLEDGE_VAULT_PATH='D:\你的Obsidian库'
  exit /b 2
)

if not exist "%ACCOUNT_KNOWLEDGE_VAULT_PATH%" (
  echo [blocked] Vault 路径不存在：%ACCOUNT_KNOWLEDGE_VAULT_PATH%
  exit /b 2
)

set "PYTHONPATH=%PROJECT_ROOT%\src;%PYTHONPATH%"
echo [ready] 启动账号知识文案单入口服务：POST http://127.0.0.1:8790/v1/news-to-copy
echo [debug] 隐藏式诊断页面（默认仅显示按钮）：http://127.0.0.1:8790/
echo [safe] 默认只做本地检索；只有请求显式选择 runtime 才会调用模型。
python "%PROJECT_ROOT%\tools\account_knowledge_lab_server.py" --host 127.0.0.1 --port 8790
exit /b %ERRORLEVEL%
