param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("model", "codex", "claude-code")]
    [string]$Profile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$newApiBaseUrl = "https://newapi.deepwisdom.ai"
$backendDirectory = Split-Path -Parent $PSScriptRoot
$runtimePathsScript = Join-Path $PSScriptRoot "runtime_paths.ps1"
. $runtimePathsScript
$runtimeHome = Get-DairyBenchRuntimeHome -BackendDirectory $backendDirectory
$profileId = "newapi-$Profile"
$newApiStateDirectory = Join-Path $runtimeHome "credentials\$profileId"
$newApiCredentialPath = Join-Path $newApiStateDirectory "token.clixml"
$newApiModelsPath = Join-Path $newApiStateDirectory "models.json"
$newApiCapabilitiesPath = Join-Path $newApiStateDirectory "model-capabilities.json"
$newApiSecureKey = Read-Host "NewAPI API key for $profileId" -AsSecureString
$newApiCredential = [PSCredential]::new($profileId, $newApiSecureKey)
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
            Where-Object { $_ } |
            Sort-Object -Unique
    )
    if ($newApiModels.Count -eq 0) {
        throw "The NewAPI credential exposes no models."
    }

    New-Item -ItemType Directory -Path $newApiStateDirectory -Force | Out-Null
    Remove-Item -LiteralPath $newApiCapabilitiesPath -Force -ErrorAction SilentlyContinue
    $newApiCredential | Export-Clixml -LiteralPath $newApiCredentialPath -Force
    ConvertTo-Json -InputObject ([string[]]$newApiModels) |
        Set-Content -LiteralPath $newApiModelsPath -Encoding UTF8

    Write-Host "[Dairy Bench] $profileId credential saved under the project runtime directory."
    Write-Host "[Dairy Bench] Models available for this policy:"
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
