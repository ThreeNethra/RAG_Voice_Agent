# One-time setup. Run from the project folder in PowerShell:   .\scripts\setup.ps1
# Creates a private Python 3.12 environment (.venv), installs the speech libraries, builds the document index,
# downloads the natural voice, and checks the machine.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Find-Python {
    foreach ($v in "3.12", "3.11", "3.13", "3.10") {
        try { $p = & py "-$v" -c "import sys;print(sys.executable)" 2>$null; if ($LASTEXITCODE -eq 0 -and $p) { return @($v, $p) } } catch {}
    }
    return $null
}
$found = Find-Python
if (-not $found) {
    Write-Host "No suitable Python found. The speech libraries do not support Python 3.14 yet." -ForegroundColor Yellow
    Write-Host "Installing Python 3.12 with winget (a normal installer; your existing Python stays untouched)..."
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    $found = Find-Python
    if (-not $found) { Write-Host "Close this window, open a new PowerShell and run setup.ps1 again." -ForegroundColor Yellow; exit 1 }
}
Write-Host "Using Python $($found[0]) at $($found[1])"
if (-not (Test-Path .venv)) { & py "-$($found[0])" -m venv .venv }
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r requirements-voice.txt
if ($LASTEXITCODE -ne 0) { Write-Host "Package install failed. Send me the red text above." -ForegroundColor Red; exit 1 }
& $py manage.py index
& $py manage.py fetch-models
& $py manage.py doctor
Write-Host "`nNext:  .\.venv\Scripts\python.exe manage.py invite --name me --groups all-staff,exec" -ForegroundColor Green
Write-Host "Then:  .\.venv\Scripts\python.exe manage.py selftest      (speaks, listens and answers, no microphone needed)"
Write-Host "Then:  .\scripts\start.ps1"
