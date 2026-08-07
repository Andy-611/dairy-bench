Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$newApiBaseUrl = "https://newapi.deepwisdom.ai"
$backendDirectory = Split-Path -Parent $PSScriptRoot
$runtimePathsScript = Join-Path $PSScriptRoot "runtime_paths.ps1"
. $runtimePathsScript
$runtimeHome = Get-DairyBenchRuntimeHome -BackendDirectory $backendDirectory
$newApiStateDirectory = Join-Path $runtimeHome "credentials"
$newApiCredentialPath = Join-Path $newApiStateDirectory "newapi-token.clixml"
$newApiModelsPath = Join-Path $newApiStateDirectory "newapi-claude-models.json"
$newApiSecureKey = Read-Host "NewAPI API key" -AsSecureString
$newApiCredential = [PSCredential]::new("newapi", $newApiSecureKey)
$newApiPlainKey = $newApiCredential.GetNetworkCredential().Password

try {
    if (-not $newApiPlainKey) {
        throw "The NewAPI API key cannot be empty."
    }
    $newApiResponse = Invoke-RestMethod `
        -Uri "$newApiBaseUrl/v1/models" `
        -Headers @{ Authorization = "Bearer $newApiPlainKey" } `
        -Method Get `
        -TimeoutSec 30
    $newApiModels = @(
        $newApiResponse.data |
            ForEach-Object { [string]$_.id } |
            Where-Object { $_ -match "claude" } |
            Sort-Object -Unique
    )
    if ($newApiModels.Count -eq 0) {
        throw "The NewAPI credential exposes no Claude models."
    }

    New-Item -ItemType Directory -Path $newApiStateDirectory -Force | Out-Null
    $newApiCredential | Export-Clixml -LiteralPath $newApiCredentialPath -Force
    ConvertTo-Json -InputObject ([string[]]$newApiModels) |
        Set-Content -LiteralPath $newApiModelsPath -Encoding UTF8

    Write-Host "[Dairy Bench] NewAPI credential saved under the project runtime directory."
    Write-Host "[Dairy Bench] Claude models available in the UI:"
    $newApiModels | ForEach-Object { Write-Host "  $_" }
    Write-Host "[Dairy Bench] Restart a running backend, then launch start.cmd."
}
catch {
    $newApiSafeMessage = [string]$_.Exception.Message
    if ($newApiPlainKey) {
        $newApiSafeMessage = $newApiSafeMessage.Replace($newApiPlainKey, "[REDACTED]")
    }
    throw "[Dairy Bench] NewAPI configuration failed: $newApiSafeMessage"
}
finally {
    $newApiPlainKey = $null
    $newApiCredential = $null
    $newApiSecureKey = $null
}
