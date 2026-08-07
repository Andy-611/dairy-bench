Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$backendDirectory = Split-Path -Parent $PSScriptRoot
$backendSourceDirectory = Join-Path $backendDirectory "src"
$newApiStateDirectory = Join-Path $env:LOCALAPPDATA "DairyBench"
$newApiCredentialPath = Join-Path $newApiStateDirectory "newapi-token.clixml"
$newApiModelsPath = Join-Path $newApiStateDirectory "newapi-claude-models.json"
$newApiPlainKey = $null

try {
    if ((Test-Path -LiteralPath $newApiCredentialPath) -and
        (Test-Path -LiteralPath $newApiModelsPath)) {
        $newApiCredential = Import-Clixml -LiteralPath $newApiCredentialPath
        $newApiPlainKey = $newApiCredential.GetNetworkCredential().Password
        $newApiCatalog = Get-Content -LiteralPath $newApiModelsPath -Raw |
            ConvertFrom-Json
        $newApiModels = @(
            $newApiCatalog |
                ForEach-Object { [string]$_ } |
                Where-Object { $_ }
        )
        if ($newApiPlainKey -and $newApiModels.Count -gt 0) {
            $env:NEWAPI_API_KEY = $newApiPlainKey
            $env:DAIRY_BENCH_NEWAPI_MODELS = $newApiModels -join ","
            $env:DAIRY_BENCH_NEWAPI_MODEL = $newApiModels[0]
        }
    }

    $env:PYTHONPATH = $backendSourceDirectory
    Set-Location -LiteralPath $backendDirectory
    & python -m uvicorn company_bench.web:create_app --factory `
        --host 127.0.0.1 --port 8000
    if ($LASTEXITCODE -ne 0) {
        throw "Dairy Bench backend exited with code $LASTEXITCODE."
    }
}
finally {
    Remove-Item Env:NEWAPI_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:DAIRY_BENCH_NEWAPI_MODELS -ErrorAction SilentlyContinue
    Remove-Item Env:DAIRY_BENCH_NEWAPI_MODEL -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    $newApiPlainKey = $null
}
