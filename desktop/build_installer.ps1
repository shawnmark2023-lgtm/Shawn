$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$csc = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) {
    throw "未找到 .NET Framework x64 编译器: $csc"
}

$onedirDir = Join-Path $root "dist\CCTRCloudExport_onedir"
if (-not (Test-Path (Join-Path $onedirDir "CCTRCloudExportDesktop.exe"))) {
    throw "请先以 CCTR_BUILD_MODE=onedir 构建 dist\CCTRCloudExport_onedir"
}
Write-Host "使用精简高速目录版载荷打包安装包: $onedirDir"

$stage = Join-Path $root "build\installer_stage"
if (Test-Path $stage) {
    Remove-Item -Recurse -Force $stage
}
New-Item -ItemType Directory -Path $stage | Out-Null

$uninstallCs = Join-Path $root "desktop\installer\UninstallApp.cs"
$setupCs = Join-Path $root "desktop\installer\SetupApp.cs"
$uninstallExe = Join-Path $stage "Uninstall.exe"

Write-Host "=== 1/3 编译轻量独立卸载程序 Uninstall.exe ==="
& $csc /nologo /codepage:65001 /optimize+ /platform:x64 /target:winexe /out:$uninstallExe `
    /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll `
    $uninstallCs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "=== 2/3 使用 LZMA (tar.xz) 高比压缩桌面端目录与卸载器载荷 ==="
$appTarXz = Join-Path $stage "app.tar.xz"
$uninstallGz = Join-Path $stage "uninstall.gz"

& (Join-Path $root ".venv\Scripts\python.exe") -c @"
import gzip, pathlib, shutil, tarfile

src_dir = pathlib.Path(r'$onedirDir')
with tarfile.open(r'$appTarXz', 'w:xz', preset=6) as tf:
    for item in sorted(src_dir.rglob('*')):
        rel = item.relative_to(src_dir).as_posix()
        tf.add(str(item), arcname=rel, recursive=False)

with open(r'$uninstallExe', 'rb') as fi, gzip.open(r'$uninstallGz', 'wb', compresslevel=9) as fo:
    shutil.copyfileobj(fi, fo, length=1024*1024)

print('app.tar.xz MB:', round(pathlib.Path(r'$appTarXz').stat().st_size / 1048576, 1))
"@
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "=== 3/3 编译轻量高速一键安装包 CCTRCloudExportSetup.exe ==="
$setupOut = Join-Path $root "成品\CCTRCloudExportSetup.exe"
& $csc /nologo /codepage:65001 /optimize+ /platform:x64 /target:winexe /out:$setupOut `
    /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:Microsoft.CSharp.dll `
    "/resource:$appTarXz,app.tar.xz" "/resource:$uninstallGz,uninstall.gz" `
    $setupCs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Get-Item $setupOut | Select-Object Name, Length, LastWriteTime
