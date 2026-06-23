# Kemi one-line installer for Windows (PowerShell):
#   irm https://raw.githubusercontent.com/emred530-blip/Kemi/main/scripts/install.ps1 | iex
#
# Creates an isolated venv at %USERPROFILE%\.kemi\venv, installs Kemi into it
# and puts a `kemi` launcher on your PATH. Safe to re-run to upgrade.
$ErrorActionPreference = "Stop"

$repo   = if ($env:KEMI_REPO)   { $env:KEMI_REPO }   else { "https://github.com/emred530-blip/Kemi" }
$branch = if ($env:KEMI_BRANCH) { $env:KEMI_BRANCH } else { "main" }
$venv   = Join-Path $env:USERPROFILE ".kemi\venv"
$binDir = Join-Path $env:USERPROFILE ".kemi\bin"

function Find-Python {
    foreach ($cmd in @("py -3", "python", "python3")) {
        $parts = $cmd.Split(" ")
        $exe = Get-Command $parts[0] -ErrorAction SilentlyContinue
        if ($exe) {
            try {
                & $parts[0] $parts[1..($parts.Length-1)] -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)"
                if ($LASTEXITCODE -eq 0) { return $cmd }
            } catch {}
        }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "Python 3.10+ is required. Install it from https://www.python.org/downloads/ (tick 'Add to PATH')." -ForegroundColor Yellow
    return
}

Write-Host "==> Creating venv at $venv"
$pyParts = $py.Split(" ")
& $pyParts[0] $pyParts[1..($pyParts.Length-1)] -m venv $venv

$venvPy = Join-Path $venv "Scripts\python.exe"
Write-Host "==> Installing kemi from $repo@$branch"
& $venvPy -m pip install --quiet --upgrade pip
try {
    & $venvPy -m pip install --quiet --upgrade "kemi[crypto] @ git+$repo@$branch"
} catch {
    & $venvPy -m pip install --quiet --upgrade "kemi @ git+$repo@$branch"
}

New-Item -ItemType Directory -Force -Path $binDir | Out-Null
$shim = Join-Path $binDir "kemi.cmd"
"@echo off`r`n`"$venvPy`" -m kemi %*" | Set-Content -Encoding ASCII $shim

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$binDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$binDir", "User")
    Write-Host "   added $binDir to your PATH (restart the terminal to pick it up)"
}

Write-Host ""
Write-Host "Kemi installed. Set sail:  kemi app" -ForegroundColor Green
