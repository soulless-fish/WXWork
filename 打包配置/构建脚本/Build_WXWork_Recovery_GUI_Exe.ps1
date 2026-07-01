param(
    [string]$PythonExe = "python",
    [string]$ExeName = "WXWorkRecoveryGUI"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
# 构建脚本已归档到“打包配置\构建脚本”，这里向上探测项目根目录，避免脚本移动后相对路径全部失效。
$RootCandidates = @(
    $ScriptDir,
    (Split-Path -Parent $ScriptDir),
    (Split-Path -Parent (Split-Path -Parent $ScriptDir))
) | Where-Object { $_ }
$BaseDir = $null
foreach ($Candidate in $RootCandidates) {
    if (
        (Test-Path -LiteralPath (Join-Path $Candidate "wxwork_recovery_gui.py")) -and
        (Test-Path -LiteralPath (Join-Path $Candidate "recover_wxwork_partial_messages.py")) -and
        (Test-Path -LiteralPath (Join-Path $Candidate "organize_wxwork_recovered_messages.py"))
    ) {
        $BaseDir = $Candidate
        break
    }
}
if (-not $BaseDir) {
    throw "未能根据脚本位置自动定位项目根目录，请检查 GUI、恢复、整理脚本是否仍在项目根目录。"
}
$GuiScriptPath = Join-Path $BaseDir "wxwork_recovery_gui.py"
$RecoveryScriptPath = Join-Path $BaseDir "recover_wxwork_partial_messages.py"
$OrganizerScriptPath = Join-Path $BaseDir "organize_wxwork_recovered_messages.py"
$EncryptedDbReaderScriptPath = Join-Path $BaseDir "read_wxwork_encrypted_databases.py"
$DistDir = Join-Path $BaseDir "dist"
$BuildDir = Join-Path $BaseDir "build-gui"
$SpecDir = Join-Path $BaseDir "打包配置"
$ResolvedPythonExe = (Get-Command $PythonExe -ErrorAction Stop).Source
# 正式 GUI 默认先打到临时文件名，再回写成正式文件，避免旧版 EXE 被占用时直接覆盖失败。
$UseTemporaryBuildName = $ExeName -eq "WXWorkRecoveryGUI"
$BuildExeName = if ($UseTemporaryBuildName) { "${ExeName}_new" } else { $ExeName }
$FinalExePath = Join-Path $DistDir "$ExeName.exe"
$BuildExePath = Join-Path $DistDir "$BuildExeName.exe"
$BuildSpecPath = Join-Path $SpecDir "$BuildExeName.spec"

if (-not (Test-Path -LiteralPath $GuiScriptPath)) {
    throw "GUI script not found: $GuiScriptPath"
}

if (-not (Test-Path -LiteralPath $RecoveryScriptPath)) {
    throw "Recovery script not found: $RecoveryScriptPath"
}

if (-not (Test-Path -LiteralPath $OrganizerScriptPath)) {
    throw "Organizer script not found: $OrganizerScriptPath"
}

if (-not (Test-Path -LiteralPath $EncryptedDbReaderScriptPath)) {
    throw "Encrypted database reader script not found: $EncryptedDbReaderScriptPath"
}

if (-not (Test-Path -LiteralPath $SpecDir)) {
    New-Item -ItemType Directory -Path $SpecDir | Out-Null
}

Write-Host "Installing build dependencies..."
& $PythonExe -m pip install pyinstaller pillow pymem psutil
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Building GUI executable..."
# 统一拼装 PyInstaller 参数，便于把恢复脚本、整理脚本和说明文档一起打进 EXE。
$PyInstallerArgs = @()
$PyInstallerArgs += "--clean"
$PyInstallerArgs += "--noconfirm"
$PyInstallerArgs += "--onefile"
$PyInstallerArgs += "--windowed"
$PyInstallerArgs += "--name"
$PyInstallerArgs += $BuildExeName
$PyInstallerArgs += "--distpath"
$PyInstallerArgs += $DistDir
$PyInstallerArgs += "--workpath"
$PyInstallerArgs += $BuildDir
$PyInstallerArgs += "--specpath"
$PyInstallerArgs += $SpecDir
$PyInstallerArgs += "--hidden-import"
$PyInstallerArgs += "recover_wxwork_partial_messages"
$PyInstallerArgs += "--hidden-import"
$PyInstallerArgs += "organize_wxwork_recovered_messages"
$PyInstallerArgs += "--hidden-import"
$PyInstallerArgs += "read_wxwork_encrypted_databases"
$PyInstallerArgs += "--hidden-import"
$PyInstallerArgs += "PIL"
$PyInstallerArgs += "--hidden-import"
$PyInstallerArgs += "PIL.Image"
$PyInstallerArgs += "--hidden-import"
$PyInstallerArgs += "PIL.ImageOps"
$PyInstallerArgs += "--hidden-import"
$PyInstallerArgs += "PIL.ImageTk"
$PyInstallerArgs += "--hidden-import"
$PyInstallerArgs += "pymem"
$PyInstallerArgs += "--hidden-import"
$PyInstallerArgs += "psutil"
$PyInstallerArgs += "--hidden-import"
$PyInstallerArgs += "sqlite3"
$PyInstallerArgs += "--hidden-import"
$PyInstallerArgs += "_sqlite3"
$PyInstallerArgs += "--hidden-import"
$PyInstallerArgs += "argparse"
$PyInstallerArgs += "--hidden-import"
$PyInstallerArgs += "binascii"
$PyInstallerArgs += "--hidden-import"
$PyInstallerArgs += "csv"
$PyInstallerArgs += "--hidden-import"
$PyInstallerArgs += "ctypes"
$PyInstallerArgs += "--hidden-import"
$PyInstallerArgs += "json"
$PyInstallerArgs += "--hidden-import"
$PyInstallerArgs += "struct"
$PyInstallerArgs += "--add-data"
$PyInstallerArgs += "$RecoveryScriptPath;."
$PyInstallerArgs += "--add-data"
$PyInstallerArgs += "$OrganizerScriptPath;."
$PyInstallerArgs += "--add-data"
$PyInstallerArgs += "$EncryptedDbReaderScriptPath;."

# 这些说明文档会被 GUI 菜单直接打开，因此源码目录整理后也要按新分类目录一起打进 EXE。
$BundledDocPaths = @(
    (Join-Path $BaseDir "文档\\使用说明\\WXWork_Recovery_GUI_Guide.md"),
    (Join-Path $BaseDir "文档\\使用说明\\WXWorkRecoveryGUI_按钮功能详解.md"),
    (Join-Path $BaseDir "文档\\交接资料\\WXWork_Chat_Recovery_Playbook.md"),
    (Join-Path $BaseDir "文档\\交接资料\\WXWork_Recovery_Codex_Handoff.md"),
    (Join-Path $BaseDir "文档\\设计资料\\项目详细链路说明-聊天恢复与知识库接入.md")
)

foreach ($DocPath in $BundledDocPaths) {
    if (Test-Path -LiteralPath $DocPath) {
        $PyInstallerArgs += @("--add-data", "$DocPath;.")
    }
}

$PyInstallerArgs += $GuiScriptPath

# 当前环境下直接调用 pyinstaller.exe 只会拉起一个外层启动器并立即返回，
# 脚本会误把旧的 dist 产物当成“本次已经打包完成”。
# 这里改为由当前 Python 直接执行 `-m PyInstaller`，这样才能拿到真实的构建输出和退出码。
$PythonBuildArgs = @("-m", "PyInstaller") + $PyInstallerArgs
Push-Location $BaseDir
& $ResolvedPythonExe @PythonBuildArgs
$PyInstallerExitCode = $LASTEXITCODE
Pop-Location
if ($PyInstallerExitCode -ne 0) {
    exit $PyInstallerExitCode
}

if (-not (Test-Path -LiteralPath $BuildExePath)) {
    throw "Build finished but EXE was not found: $BuildExePath"
}

if ($UseTemporaryBuildName) {
    if (Test-Path -LiteralPath $FinalExePath) {
        try {
            Remove-Item -LiteralPath $FinalExePath -Force
        } catch {
            throw "旧版正式 EXE 正在被占用，无法替换：$FinalExePath。请先关闭正在运行的 GUI 后重试。"
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
