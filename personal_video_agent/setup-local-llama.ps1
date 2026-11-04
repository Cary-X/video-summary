param(
    [string]$RuntimeArchive,
    [string]$ModelFile
)

$ErrorActionPreference = 'Stop'
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
    throw 'This installer is for Windows x64. Use PVA_LLAMA_SERVER and PVA_LLAMA_MODEL on other platforms.'
}

$dataDir = Join-Path $PSScriptRoot 'data'
$runtimeDir = Join-Path $dataDir 'llama.cpp'
$modelDir = Join-Path $dataDir 'models'
New-Item -ItemType Directory -Force -Path $runtimeDir, $modelDir | Out-Null
$server = Join-Path $runtimeDir 'llama-server.exe'
$model = Join-Path $modelDir 'qwen3-1.7b.gguf'

function Receive-File([string]$Url, [string]$Destination) {
    & curl.exe --fail --location --retry 2 --connect-timeout 15 --max-time 1800 --output $Destination $Url
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $Url" }
}

if (-not (Test-Path -LiteralPath $server)) {
    $release = 'b10929'
    $asset = "llama-$release-bin-win-vulkan-x64.zip"
    if (-not $RuntimeArchive) {
        $RuntimeArchive = Join-Path $dataDir $asset
        if (-not (Test-Path -LiteralPath $RuntimeArchive)) {
            Receive-File "https://github.com/ggml-org/llama.cpp/releases/download/$release/$asset" "$RuntimeArchive.part"
            Move-Item -LiteralPath "$RuntimeArchive.part" -Destination $RuntimeArchive
        }
    }
    $expected = '0527be2bcb79797337c4c500e6c25a62e0cdb572b14ef2db1066c94706208936'
    if ((Get-FileHash -LiteralPath $RuntimeArchive -Algorithm SHA256).Hash -ne $expected) {
        throw "The llama.cpp archive does not match the published SHA256: $RuntimeArchive"
    }
    Expand-Archive -LiteralPath $RuntimeArchive -DestinationPath $runtimeDir -Force
    if (-not (Test-Path -LiteralPath $server)) { throw 'llama-server.exe is missing from the extracted release.' }
}

if (-not (Test-Path -LiteralPath $model)) {
    $stagingModel = "$model.part"
    if (-not $ModelFile) {
        $digest = '3d0b790534fe4b79525fc3692950408dca41171676ed7e21db57af5c65ef6ab6'
        $ollamaDir = if ($env:OLLAMA_MODELS) { $env:OLLAMA_MODELS } else { Join-Path $env:USERPROFILE '.ollama/models' }
        $blob = Join-Path $ollamaDir "blobs/sha256-$digest"
        if ((Test-Path -LiteralPath $blob) -and
            ((Get-FileHash -LiteralPath $blob -Algorithm SHA256).Hash -eq $digest)) {
            $ModelFile = $blob
        }
    }
    if ($ModelFile) {
        Copy-Item -LiteralPath $ModelFile -Destination $stagingModel
    } else {
        Receive-File 'https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q4_K_M.gguf' $stagingModel
    }
    $reader = [IO.File]::OpenRead($stagingModel)
    try {
        $header = New-Object byte[] 4
        if ($reader.Read($header, 0, 4) -ne 4 -or [Text.Encoding]::ASCII.GetString($header) -ne 'GGUF') {
            throw 'The model file is not a GGUF.'
        }
    } finally {
        $reader.Dispose()
    }
    Move-Item -LiteralPath $stagingModel -Destination $model
}

Write-Host "Runtime: $server"
Write-Host "Model:   $model"
Write-Host 'Choose Local llama.cpp in Raccoon Agent Settings.'
