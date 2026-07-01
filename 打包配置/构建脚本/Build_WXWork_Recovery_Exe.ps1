param(
    [string]$PythonExe = "python",
    [string]$ExeName = "WXWorkRecoveryCli"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
# 构建脚本已归档到“打包配置\构建脚本”，这里向上探测项目根目录，兼容后续继续调整脚本目录。
$RootCandidates = @(
    $ScriptDir,
    (Split-Path -Parent $ScriptDir),
    (Split-Path -Parent (Split-Path -Parent $ScriptDir))
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
$ScriptPath = Join-Path $BaseDir "recover_wxwork_partial_messages.py"
$DistDir = Join-Path $BaseDir "dist"
$BuildDir = Join-Path $BaseDir "build"
$SpecDir = Join-Path $BaseDir "打包配置"
$UseTemporaryBuildName = $ExeName -eq "WXWorkRecoveryCli"
$BuildExeName = if ($UseTemporaryBuildName) { "${ExeName}_build" } else { $ExeName }
$FinalExePath = Join-Path $DistDir "$ExeName.exe"
$BuildExePath = Join-Path $DistDir "$BuildExeName.exe"
$BuildSpecPath = Join-Path $SpecDir "$BuildExeName.spec"

if (-not (Test-Path -LiteralPath $ScriptPath)) {
    throw "Recovery script not found: $ScriptPath"
}

if (-not (Test-Path -LiteralPath $SpecDir)) {
    New-Item -ItemType Directory -Path $SpecDir | Out-Null
}

Write-Host "Installing build dependencies..."
& $PythonExe -m pip install pyinstaller pymem psutil
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Building single-file executable..."
& $PythonExe -m PyInstaller `
    --clean `
    --noconfirm `
    --onefile `
    --name $BuildExeName `
    --distpath $DistDir `
    --workpath $BuildDir `
    --specpath $SpecDir `
    --hidden-import pymem `
    --hidden-import psutil `
    $ScriptPath

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

if (-not (Test-Path -LiteralPath $BuildExePath)) {
    throw "Build finished but EXE was not found: $BuildExePath"
}

if ($UseTemporaryBuildName) {
    if (Test-Path -LiteralPath $FinalExePath) {
        try {
            Remove-Item -LiteralPath $FinalExePath -Force
        } catch {
            throw "旧版正式 CLI EXE 正在被占用，无法替换：$FinalExePath。请先关闭正在运行的 CLI 后重试。"
        }
    }
    Move-Item -LiteralPath $BuildExePath -Destination $FinalExePath -Force
}

if (Test-Path -LiteralPath $BuildSpecPath) {
    Remove-Item -LiteralPath $BuildSpecPath -Force -ErrorAction SilentlyContinue
}

if (Test-Path -LiteralPath $BuildDir) {
    Remove-Item -LiteralPath $BuildDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Build complete:"
Write-Host "  $FinalExePath"
Write-Host ""
Write-Host "Example:"
Write-Host "  & '$FinalExePath' --corp-id <CorpId> --output-dir '$BaseDir'"
