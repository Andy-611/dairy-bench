param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

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

    $backendDirectory = Join-Path $RepositoryRoot "backend"
    $backendCommand = (
        "title Dairy Bench Backend && cd /d `"{0}`" && " +
        "python -m uvicorn company_bench.web:create_app --factory " +
        "--host 127.0.0.1 --port 8000"
    ) -f $backendDirectory
    Start-Process -FilePath $env:ComSpec -ArgumentList "/k", $backendCommand
}
finally {
    Remove-Item Env:NEWAPI_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:DAIRY_BENCH_NEWAPI_MODELS -ErrorAction SilentlyContinue
    Remove-Item Env:DAIRY_BENCH_NEWAPI_MODEL -ErrorAction SilentlyContinue
    $newApiPlainKey = $null
}
