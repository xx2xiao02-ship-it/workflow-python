@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "CONSOLE_PYTHON=%~dp0services\douyin-monitor\.venv\Scripts\python.exe"
if not exist "%CONSOLE_PYTHON%" (
  echo Required project Python environment is missing.
  exit /b 1
)

rem Keep service control metadata and task snapshots on the persistent project
rem runtime so the Windows start/stop path does not depend on a locked AppData
rem directory.
set "LOCAL_SERVICE_RUNTIME_ROOT=%~dp0.runtime-governance-live\service-control"
set "VIDEO_CONSOLE_RUNTIME_ROOT=%~dp0.runtime-governance-live\data"

rem The production style-package button now requires the Obsidian bridge.
rem Keep the Vault path explicit (it may be an external Obsidian Vault or a
rem project-local compatible Vault) and fail early instead of silently
rem starting the legacy JSON-only distillation chain.
if "%ACCOUNT_KNOWLEDGE_VAULT_PATH%"=="" (
  echo [blocked] ACCOUNT_KNOWLEDGE_VAULT_PATH is not configured.
  echo Set it to your Obsidian Vault before starting the production console.
  exit /b 2
)
if not exist "%ACCOUNT_KNOWLEDGE_VAULT_PATH%" (
  echo [blocked] Obsidian Vault path does not exist: %ACCOUNT_KNOWLEDGE_VAULT_PATH%
  exit /b 2
)

echo Starting Video Production Console through the single-instance governor.
"%CONSOLE_PYTHON%" tools\local_service_governance.py start --service video_production_console
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo Service was not started. The port owner was not killed blindly.
exit /b %RC%
