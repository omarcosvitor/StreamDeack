param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '1.1.0',
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'
$ProjectDir = $PSScriptRoot
$BuildDir = Join-Path $ProjectDir 'build'
$DistDir = Join-Path $ProjectDir 'dist'
$ReleaseDir = Join-Path $ProjectDir 'release'
$VenvDir = Join-Path $ProjectDir '.venv'

Push-Location $ProjectDir
try {
    $BuildPython = Join-Path $VenvDir 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $BuildPython)) {
        python -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) {
            throw "Nao foi possivel criar o ambiente de build (codigo $LASTEXITCODE)."
        }
    }

    & $BuildPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectDir 'requirements-build.txt')
    if ($LASTEXITCODE -ne 0) {
        throw "Nao foi possivel instalar as dependencias de build (codigo $LASTEXITCODE)."
    }

    New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
    $IconPath = Join-Path $BuildDir 'StreamDeck.ico'
    & $BuildPython (Join-Path $ProjectDir 'make_icon.py') $IconPath
    if ($LASTEXITCODE -ne 0) {
        throw "Nao foi possivel gerar o icone (codigo $LASTEXITCODE)."
    }

    & $BuildPython -m PyInstaller `
        --noconfirm `
        --clean `
        --name StreamDeck `
        --onedir `
        --windowed `
        --icon $IconPath `
        --add-data 'index.html;.' `
        --exclude-module decklinux `
        --distpath $DistDir `
        --workpath $BuildDir `
        deck.py

    if ($LASTEXITCODE -ne 0) {
        throw "O PyInstaller falhou com o codigo $LASTEXITCODE."
    }

    if ($SkipInstaller) {
        Write-Host "Aplicativo gerado em: $DistDir\StreamDeck\StreamDeck.exe"
        return
    }

    $IsccCandidates = @(
        (Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

    $Iscc = $IsccCandidates | Select-Object -First 1
    if (-not $Iscc) {
        throw 'Inno Setup 6 nao encontrado. Instale com: winget install JRSoftware.InnoSetup'
    }

    New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
    & $Iscc "/DMyAppVersion=$Version" (Join-Path $ProjectDir 'installer.iss')
    if ($LASTEXITCODE -ne 0) {
        throw "O Inno Setup falhou com o codigo $LASTEXITCODE."
    }

    Write-Host "Instalador gerado em: $ReleaseDir\StreamDeck-Setup-$Version.exe"
}
finally {
    Pop-Location
}
