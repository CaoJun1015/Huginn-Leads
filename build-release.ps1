param([ValidatePattern("^[A-Za-z0-9][A-Za-z0-9.-]*$")][string]$Version = "0.1.0-test", [switch]$SkipDependencyInstall)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$releaseRoot = Join-Path $projectRoot "release"
$bundleName = "Huginn-Leads-$Version-win64"
$bundleRoot = Join-Path $releaseRoot $bundleName
$zipPath = Join-Path $releaseRoot "$bundleName.zip"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Missing .venv. Run install.cmd before building a release."
}

Push-Location (Join-Path $projectRoot "frontend")
try {
    if (-not $SkipDependencyInstall) {
        npm.cmd ci
        if ($LASTEXITCODE) { throw "npm ci failed with exit code $LASTEXITCODE" }
    }
    npm.cmd run build
    if ($LASTEXITCODE) { throw "Frontend build failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}

if (-not $SkipDependencyInstall) {
    & $python -m pip install -r (Join-Path $projectRoot "requirements-dev.txt")
    if ($LASTEXITCODE) { throw "Dependency installation failed with exit code $LASTEXITCODE" }
}
$oldPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = Join-Path $projectRoot "src"
try {
    & $python -m unittest discover -s (Join-Path $projectRoot "tests") -v
    if ($LASTEXITCODE) { throw "Tests failed with exit code $LASTEXITCODE" }
} finally {
    $env:PYTHONPATH = $oldPythonPath
}

$buildRoot = Join-Path $projectRoot "build\pyinstaller-$Version"
foreach ($target in @($buildRoot, $bundleRoot, $zipPath)) {
    if (Test-Path -LiteralPath $target) { throw "Output already exists; choose a new Version: $target" }
}
New-Item -ItemType Directory -Path $buildRoot, $bundleRoot -Force | Out-Null

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --name HuginnLeads `
    --paths (Join-Path $projectRoot "src") `
    --hidden-import nonebot.drivers.fastapi `
    --add-data "$(Join-Path $projectRoot 'src\huginn_leads\static');huginn_leads/static" `
    --add-data "$(Join-Path $projectRoot 'src\huginn_leads\migrations');huginn_leads/migrations" `
    --workpath (Join-Path $buildRoot "work") `
    --specpath (Join-Path $buildRoot "spec") `
    --distpath (Join-Path $buildRoot "dist") `
    (Join-Path $projectRoot "bot.py")
if ($LASTEXITCODE) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

Copy-Item -Path (Join-Path $buildRoot "dist\HuginnLeads\*") -Destination $bundleRoot -Recurse
Copy-Item -LiteralPath (Join-Path $projectRoot ".env.example") -Destination $bundleRoot
Copy-Item -LiteralPath (Join-Path $projectRoot "packaging\start.cmd") -Destination $bundleRoot
Copy-Item -LiteralPath (Join-Path $projectRoot "restore.cmd") -Destination $bundleRoot
Copy-Item -LiteralPath (Join-Path $projectRoot "docs\Windows测试包使用与恢复.md") -Destination $bundleRoot
Copy-Item -LiteralPath (Join-Path $projectRoot "LICENSE") -Destination $bundleRoot
Copy-Item -LiteralPath (Join-Path $projectRoot "THIRD_PARTY.md") -Destination $bundleRoot

foreach ($required in @("HuginnLeads.exe", "start.cmd", "restore.cmd", ".env.example", "LICENSE", "THIRD_PARTY.md", "Windows测试包使用与恢复.md")) {
    if (-not (Test-Path -LiteralPath (Join-Path $bundleRoot $required) -PathType Leaf)) { throw "Missing bundle file: $required" }
}
$unexpected = Get-ChildItem -LiteralPath $bundleRoot -Recurse -File | Where-Object { $_.Name -eq ".env" -or $_.Name -match '\.(db|log)$|-(wal|shm)$' }
if ($unexpected) { throw "Unexpected runtime data in bundle" }
Compress-Archive -LiteralPath $bundleRoot -DestinationPath $zipPath -CompressionLevel Optimal
Write-Host "Release ready: $zipPath"
