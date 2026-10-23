<#
.SYNOPSIS
  One-time setup for Metasploit API Explorer on Windows: Python venv + packages, Ollama models.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -ChatModel qwen3:8b
#>
param(
    [string]$ChatModel = "qwen3:14b",
    [string]$EmbedModel = "nomic-embed-text",
    [switch]$SkipModels
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

# 1. Python
Step "Checking Python (3.10 or newer)"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "Python not found. Install Python 3.10+ from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')." }
$ver = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]"3.10") { throw "Python $ver found, but 3.10 or newer is required." }
Write-Host "Python $ver"

# 2. Virtual environment + packages
Step "Creating virtual environment (.venv) and installing packages"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & python -m venv .venv }
& .venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& .venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Write-Host "Packages installed"

# 3. Ollama
Step "Checking Ollama"
$ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $ollama -and (Test-Path "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe")) { $ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" }
if (-not $ollama) {
    Write-Host "Ollama is not installed. Install it with:" -ForegroundColor Yellow
    Write-Host "    winget install --id Ollama.Ollama -e" -ForegroundColor Yellow
    Write-Host "or download it from https://ollama.com/download, then run this script again." -ForegroundColor Yellow
    exit 1
}
Write-Host "Ollama: $ollama"
try { Invoke-RestMethod http://127.0.0.1:11434/api/version -TimeoutSec 5 | Out-Null }
catch {
    Write-Host "Starting the Ollama server..."
    Start-Process -FilePath $ollama -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 5
}

# 4. Models
if (-not $SkipModels) {
    Step "Pulling models: $ChatModel (chat) and $EmbedModel (API-page embeddings)"
    & $ollama pull $ChatModel
    & $ollama pull $EmbedModel
    if ($ChatModel -ne "qwen3:14b") {
        Write-Host "Using a non-default chat model - saving it to settings.json"
        "{`n  `"chat_model`": `"$ChatModel`"`n}" | Set-Content -Encoding utf8 settings.json
    }
}

# 5. Check
Step "Checking the installation"
& .venv\Scripts\python.exe -m msfrag doctor
Write-Host "`nSetup complete. Scrape the API reference, index it, then start the app:" -ForegroundColor Green
Write-Host "    .venv\Scripts\python scripts\scrape_api_docs.py" -ForegroundColor Green
Write-Host "    .venv\Scripts\python scripts\ingest_api_docs.py metasploit-api-docs --no-summary" -ForegroundColor Green
Write-Host "    scripts\start.ps1        (then open http://127.0.0.1:8120)" -ForegroundColor Green
