$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $root 'start_video_production_console.cmd'
if (-not (Test-Path -LiteralPath $launcher)) { throw "Launcher missing: $launcher" }

$key = 'HKCU:\Software\Classes\video-production-console'
New-Item -Path $key -Force | Out-Null
Set-ItemProperty -Path $key -Name '(Default)' -Value 'URL:Video Production Console Protocol'
New-ItemProperty -Path $key -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null
New-Item -Path "$key\shell\open\command" -Force | Out-Null
$command = "cmd.exe /c `"`"$launcher`"`""
Set-ItemProperty -Path "$key\shell\open\command" -Name '(Default)' -Value $command

Write-Host 'Video Production Console launcher installed.' -ForegroundColor Green
