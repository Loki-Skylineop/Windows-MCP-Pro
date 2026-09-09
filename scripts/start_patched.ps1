<#
.SYNOPSIS
    Run the Windows-MCP server directly from this checkout (patched source).

.DESCRIPTION
    The published server is normally started with `uvx windows-mcp-pro serve ...`,
    which runs the *published* package - none of the local changes in `src/`.
    This script runs the same CLI from this working copy instead, by putting
    `src` first on PYTHONPATH, so the Edit / Grep / Job tools and the patched
    PowerShell tool are actually served.

    Interpreter: any Python that already has the server dependencies. By default
    the `python` on PATH is used (on this machine that is the uv tool
    environment created for windows-mcp, which has every dependency installed).
    Override with -Python or the WMCP_PYTHON environment variable.

.EXAMPLE
    # Side-by-side test on another port, leaving the live server alone:
    .\scripts\start_patched.ps1 -Port 8001

.EXAMPLE
    # Replace the live server on port 8000, then reconnect the client:
    .\scripts\start_patched.ps1 -Port 8000 -StopExisting

.EXAMPLE
    # Keep it attached to the current console (Ctrl+C to stop):
    .\scripts\start_patched.ps1 -Port 8001 -Foreground
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$BindAddress = '0.0.0.0',
    [string]$AuthKey = 'ce63bd75-64e9-4e39-9a2e-36879f82fc16',
    [string]$CertFile = 'C:\Users\Gname\Desktop\123\certs\fullchain.pem',
    [string]$KeyFile = 'C:\Users\Gname\Desktop\123\certs\private.key',
    [string]$Python = $env:WMCP_PYTHON,
    [switch]$StopExisting,
    [switch]$Foreground,
    [switch]$NoSsl
)

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$src = Join-Path $repo 'src'
if (-not (Test-Path (Join-Path $src 'windows_mcp\__main__.py'))) {
    throw "Not a Windows-MCP checkout: $src\windows_mcp\__main__.py not found."
}

if (-not $Python) {
    $Python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $Python -or -not (Test-Path $Python)) {
    throw 'No usable Python found. Pass -Python <path to python.exe> or set $env:WMCP_PYTHON.'
}

Write-Host "repo   : $repo"
Write-Host "python : $Python"

# --- optionally free the port -------------------------------------------------
if ($StopExisting) {
    $owners = @()
    try {
        $owners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique
    } catch {
        Write-Host "No listener found on port $Port."
    }
    foreach ($procId in $owners) {
        $existing = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($existing) {
            Write-Host "Stopping $($existing.ProcessName) (pid $procId) on port $Port"
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
    }
    if ($owners.Count -gt 0) { Start-Sleep -Seconds 2 }
}

# --- environment: patched source wins over the installed package --------------
if ($env:PYTHONPATH) { $env:PYTHONPATH = "$src;$env:PYTHONPATH" } else { $env:PYTHONPATH = $src }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$serverArgs = @(
    '-m', 'windows_mcp', 'serve',
    '--transport', 'streamable-http',
    '--host', $BindAddress,
    '--port', "$Port",
    '--auth-key', $AuthKey
)
if (-not $NoSsl) {
    if (Test-Path $CertFile) { $serverArgs += @('--ssl-certfile', $CertFile) }
    else { Write-Warning "Certificate not found, starting without TLS: $CertFile" }
    if (Test-Path $KeyFile) { $serverArgs += @('--ssl-keyfile', $KeyFile) }
    elseif (Test-Path $CertFile) { Write-Warning "Key not found: $KeyFile" }
}

if ($Foreground) {
    Write-Host "Starting in the foreground on port $Port (Ctrl+C to stop)..."
    & $Python @serverArgs
    exit $LASTEXITCODE
}

$logDir = Join-Path $env:LOCALAPPDATA 'windows-mcp\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$outLog = Join-Path $logDir "server-$Port-$stamp.out.log"
$errLog = Join-Path $logDir "server-$Port-$stamp.err.log"

$proc = Start-Process -FilePath $Python -ArgumentList $serverArgs `
    -WorkingDirectory $repo -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog

Start-Sleep -Seconds 4
if ($proc.HasExited) {
    Write-Warning "Server exited immediately (code $($proc.ExitCode)). Last lines:"
    if (Test-Path $errLog) { Get-Content $errLog -Tail 30 }
    if (Test-Path $outLog) { Get-Content $outLog -Tail 30 }
    exit 1
}

$scheme = if ($NoSsl) { 'http' } else { 'https' }
Write-Host ""
Write-Host "Patched server running: pid $($proc.Id)"
Write-Host "URL      : ${scheme}://<this-host>:$Port/mcp"
Write-Host "stdout   : $outLog"
Write-Host "stderr   : $errLog"
Write-Host "Stop with: Stop-Process -Id $($proc.Id) -Force"
Write-Host ""
Write-Host 'Reconnect the MCP client afterwards so it re-reads the tool list (Edit, Grep, Job).'
