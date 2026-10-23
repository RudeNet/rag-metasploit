<#
.SYNOPSIS
  Start the Metasploit API Explorer web app (http://127.0.0.1:8120 by default).

.EXAMPLE
  scripts\start.ps1
  scripts\start.ps1 -Port 8200
  scripts\start.ps1 -BindHost 0.0.0.0     # reachable from other machines (the web UI has no login!)
#>
param([string]$BindHost = "127.0.0.1", [int]$Port = 8120)
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
if (-not (Test-Path ".venv\Scripts\python.exe")) { throw "Run scripts\setup.ps1 first." }
& .venv\Scripts\python.exe -m msfrag web --host $BindHost --port $Port
