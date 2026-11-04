param([string]$IndexUrl = 'https://pypi.org/simple')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$appPython = Join-Path $PSScriptRoot '.venv64\Scripts\python.exe'
$visionPython = Join-Path $PSScriptRoot '.venv-paddleocr\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $visionPython)) {
    & $appPython -m venv (Join-Path $PSScriptRoot '.venv-paddleocr')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated vision environment' }
}
& $visionPython -m pip install --index-url $IndexUrl -r (Join-Path $PSScriptRoot 'requirements-vision.txt')
if ($LASTEXITCODE -ne 0) { throw 'Local vision dependency installation failed' }
& $visionPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Local vision dependencies are inconsistent' }
Write-Output 'Local PaddleOCR-VL runtime installed. Models download on the first image read.'
