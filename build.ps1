# Cai Install Web GUI - build script (same packaging method as official 2.5 release: Nuitka --onefile x64)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$VERSION = '2.6'
$OUTNAME = "cai.install.web.ui$VERSION"
$outDir  = "dist_$OUTNAME"

if (Test-Path $outDir) { Remove-Item $outDir -Recurse -Force }
if (Test-Path "$OUTNAME.exe") { Remove-Item "$OUTNAME.exe" -Force }

Write-Host "[1/3] Nuitka onefile build starting..."

$args = @(
  '-m','nuitka',
  '--onefile',
  '--assume-yes-for-downloads',
  '--windows-console-mode=force',
  '--enable-plugin=tk-inter',
  '--include-package=flask',
  '--include-package=flask_socketio',
  '--include-package=engineio',
  '--include-package=socketio',
  '--include-package=httpx',
  '--include-package=httpcore',
  '--include-package=aiofiles',
  '--include-package=colorlog',
  '--include-package=vdf',
  '--include-package=ujson',
  '--include-package=jinja2',
  '--include-package=werkzeug',
  '--include-package=click',
  '--include-data-dir=templates=templates',
  '--include-data-dir=static=static',
  '--include-data-dir=assets=assets',
  "--output-dir=$outDir",
  "--output-filename=$OUTNAME.exe",
  '--company-name=Cai',
  '--product-name=Cai Install Web GUI',
  "--file-version=$VERSION",
  "--product-version=$VERSION",
  '--file-description=Cai Install Web GUI',
  'app.py'
)

python @args
if ($LASTEXITCODE -ne 0) { throw "Nuitka build failed with exit code $LASTEXITCODE" }

Write-Host "[2/3] Moving artifact to project root..."
Move-Item "$outDir\$OUTNAME.exe" "$OUTNAME.exe" -Force
Remove-Item $outDir -Recurse -Force

Write-Host "[3/3] Done."
$f = Get-Item "$OUTNAME.exe"
Write-Host ("Built: {0}  {1:N2} MB" -f $f.Name, ($f.Length / 1MB))
