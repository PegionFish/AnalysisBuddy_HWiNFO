# AnalysisBuddy_HWiNFO 发布包打包脚本（PowerShell 5.1 兼容，无第三方依赖）。
#
# 产出（默认 dist/ 目录）：
#   AnalysisBuddy_hwinfo-log_v<version>.zip   —— plugin.json 位于 zip 根（4.2 硬性要求 1）
#   SHA256SUMS.txt                            —— <sha256>  <zip名>（G4 校验和纪律）
#
# 内嵌自检（G2 缺口自补）：
#   1. zip 条目无绝对路径 / ".." 越界；
#   2. 根目录含 plugin.json；
#   3. zip 内 plugin.json 的 id/version 与仓库根一致；
#   4. 可选：设置环境变量 AB_RELEASE_TAG=vX.Y.Z 时，要求其与 manifest version 一致
#      （CI 在 tag 触发时传入，保证 zip 内 version 与 Release tag 自洽）。
#
# 用法：powershell -ExecutionPolicy Bypass -File scripts/pack.ps1 [-OutDir dist]

param(
    [string]$OutDir = "dist"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

# ---- 读取 manifest（决定产物名与自检基准） ----

$manifest = Get-Content -LiteralPath (Join-Path $root "plugin.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$id = $manifest.id
$version = $manifest.version
if ([string]::IsNullOrWhiteSpace($id) -or [string]::IsNullOrWhiteSpace($version)) {
    throw "plugin.json 缺少 id 或 version"
}

# CI 可选 tag 一致性校验（zip 内 version 与 tag 自洽，§4.2 第 3 条）
if ($env:AB_RELEASE_TAG) {
    $expectedTag = "v$version"
    if ($env:AB_RELEASE_TAG -ne $expectedTag) {
        throw "AB_RELEASE_TAG='$($env:AB_RELEASE_TAG)' 与 manifest version '$version' 不一致（期望 '$expectedTag'）"
    }
}

# ---- 白名单文件（zip 根 = 仓库根，天然排除 .git/tests/.github/scripts/__pycache__） ----

$files = @("plugin.json", "main.py", "parser.py", "config.json", "README.md", "LICENSE")
foreach ($f in $files) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $f))) {
        throw "打包必需文件缺失：$f"
    }
}

# ---- 写 zip（逐条目显式命名，保证条目前向斜杠与根布局，规避 Compress-Archive 前缀问题） ----

$zipName = "AnalysisBuddy_${id}_v${version}.zip"
# OutDir 支持绝对路径（CI/手动验证用）与相对路径（相对仓库根）
if ([System.IO.Path]::IsPathRooted($OutDir)) {
    $outDirFull = $OutDir
} else {
    $outDirFull = Join-Path $root $OutDir
}
$outDirFull = [System.IO.Path]::GetFullPath($outDirFull)
New-Item -ItemType Directory -Force -Path $outDirFull | Out-Null
$zipPath = Join-Path $outDirFull $zipName
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

$fs = [System.IO.File]::Create($zipPath)
$zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($f in $files) {
        $entry = $zip.CreateEntry($f, [System.IO.Compression.CompressionLevel]::Optimal)
        $es = $entry.Open()
        try {
            $bytes = [System.IO.File]::ReadAllBytes((Join-Path $root $f))
            $es.Write($bytes, 0, $bytes.Length)
        } finally {
            $es.Dispose()
        }
    }
} finally {
    $zip.Dispose()
    $fs.Dispose()
}

# ---- 内嵌自检：条目布局 / 根含 plugin.json / id、version 一致 ----

$archive = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
try {
    $names = @($archive.Entries | ForEach-Object { $_.FullName })
    foreach ($n in $names) {
        if ($n -match "^[A-Za-z]:" -or $n.StartsWith("/") -or $n -match "(^|/)\.\." -or $n -match "\\") {
            throw "非法 zip 条目（绝对路径/越界/反斜杠）：$n"
        }
    }
    if ($names -notcontains "plugin.json") {
        throw "zip 根缺少 plugin.json（根目录必须是 plugin.json，§4.2 硬性要求 1）"
    }
    $entry = $archive.GetEntry("plugin.json")
    $reader = New-Object System.IO.StreamReader($entry.Open())
    try {
        $innerJson = $reader.ReadToEnd()
    } finally {
        $reader.Dispose()
    }
    $innerManifest = $innerJson | ConvertFrom-Json
    if ($innerManifest.id -ne $id -or $innerManifest.version -ne $version) {
        throw "zip 内 id/version（$($innerManifest.id)/$($innerManifest.version)）与仓库根（$id/$version）不一致"
    }
} finally {
    $archive.Dispose()
}

# ---- SHA256SUMS（G4 校验和纪律：发布资产可核验） ----

$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLower()
"$hash  $zipName" | Set-Content -LiteralPath (Join-Path $outDirFull "SHA256SUMS.txt") -Encoding ascii

Write-Host "packed: $zipPath"
Write-Host "sha256: $hash"
Write-Host "entries: $($files -join ', ')"
