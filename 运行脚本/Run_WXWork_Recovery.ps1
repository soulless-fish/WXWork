param(
    [string]$CorpId,
    [int]$TargetPid = 0,
    [string]$OutputDir,
    [string]$PythonExe = "python",
    [switch]$SkipDependencyInstall,
    [switch]$PreferExe
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
# 启动脚本已移到“运行脚本”目录，这里优先从脚本目录及其父目录反推项目根目录。
$RootCandidates = @(
    $ScriptDir,
    (Split-Path -Parent $ScriptDir)
) | Where-Object { $_ }
$BaseDir = $null
foreach ($Candidate in $RootCandidates) {
    if (Test-Path -LiteralPath (Join-Path $Candidate "recover_wxwork_partial_messages.py")) {
        $BaseDir = $Candidate
        break
    }
}
if (-not $BaseDir) {
    throw "未能根据脚本位置自动定位项目根目录，请检查 recover_wxwork_partial_messages.py 是否仍在项目根目录。"
}
if (-not $OutputDir) {
    $OutputDir = $BaseDir
}

$ScriptPath = Join-Path $BaseDir "recover_wxwork_partial_messages.py"
$ExePath = Join-Path (Join-Path $BaseDir "dist") "WXWorkRecoveryCli.exe"

if (-not $CorpId) {
    $CorpId = Read-Host "Enter CorpId"
}

if (-not $CorpId) {
    throw "CorpId is required."
}

if (-not (Test-Path -LiteralPath $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir | Out-Null
}

Write-Host ""
Write-Host "Before extraction, do this in WXWork:"
Write-Host "1. Open the target corp/account."
Write-Host "2. Open the target chat."
Write-Host "3. Scroll older history to warm the cache."
Write-Host ""

$canUseExe = Test-Path -LiteralPath $ExePath
$pythonCmd = Get-Command $PythonExe -ErrorAction SilentlyContinue

if (($PreferExe -or -not $pythonCmd) -and $canUseExe) {
    $arguments = @("--corp-id", $CorpId, "--output-dir", $OutputDir)
    if ($TargetPid -gt 0) {
        $arguments += @("--pid", $TargetPid)
    }

    Write-Host "Running packaged executable:"
    Write-Host "  $ExePath $($arguments -join ' ')"
    & $ExePath @arguments
    exit $LASTEXITCODE
}

if (-not $pythonCmd) {
    throw "Python was not found and packaged executable is missing. Build the EXE first."
}

if (-not (Test-Path -LiteralPath $ScriptPath)) {
    throw "Recovery script not found: $ScriptPath"
}

if (-not $SkipDependencyInstall) {
    Write-Host "Installing or checking required Python packages..."
    & $PythonExe -m pip install pymem psutil
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

$arguments = @($ScriptPath, "--corp-id", $CorpId, "--output-dir", $OutputDir)
if ($TargetPid -gt 0) {
    $arguments += @("--pid", $TargetPid)
}

Write-Host "Running Python recovery script:"
Write-Host "  $PythonExe $($arguments -join ' ')"
& $PythonExe @arguments
exit $LASTEXITCODE
