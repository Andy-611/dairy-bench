Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$backendDirectory = Split-Path -Parent $PSScriptRoot
$runtimePathsScript = Join-Path $PSScriptRoot "runtime_paths.ps1"
. $runtimePathsScript
$runtimeHome = Get-DairyBenchRuntimeHome -BackendDirectory $backendDirectory
$newApiProfiles = @(
    @{ Id = "newapi-model"; Prefix = "DAIRY_BENCH_NEWAPI_MODEL" },
    @{ Id = "newapi-codex"; Prefix = "DAIRY_BENCH_NEWAPI_CODEX" },
    @{ Id = "newapi-claude-code"; Prefix = "DAIRY_BENCH_NEWAPI_CLAUDE" }
)
$configuredVariables = [Collections.Generic.List[string]]::new()

try {
    foreach ($profile in $newApiProfiles) {
        $profileDirectory = Join-Path $runtimeHome "credentials\$($profile.Id)"
        $credentialPath = Join-Path $profileDirectory "token.clixml"
        $modelsPath = Join-Path $profileDirectory "models.json"
        if ((Test-Path -LiteralPath $credentialPath) -and
            (Test-Path -LiteralPath $modelsPath)) {
            $credential = Import-Clixml -LiteralPath $credentialPath
            $plainKey = $credential.GetNetworkCredential().Password
            $modelCatalog = Get-Content -LiteralPath $modelsPath -Raw |
                ConvertFrom-Json
            $models = @(
                $modelCatalog |
                    ForEach-Object { [string]$_ } |
                    Where-Object { $_ }
            )
            if ($plainKey -and $models.Count -gt 0) {
                $values = @{
                    "$($profile.Prefix)_API_KEY" = $plainKey
                    "$($profile.Prefix)_MODELS" = $models -join ","
                    "$($profile.Prefix)_MODEL" = $models[0]
                }
                foreach ($entry in $values.GetEnumerator()) {
                    [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "Process")
                    $configuredVariables.Add($entry.Key)
                }
            }
            $plainKey = $null
            $credential = $null
        }
    }

    Set-Location -LiteralPath $backendDirectory
    & python -m uvicorn company_bench.web.app:create_app --factory `
        --app-dir src --host 127.0.0.1 --port 8000
    if ($LASTEXITCODE -ne 0) {
        throw "Dairy Bench backend exited with code $LASTEXITCODE."
    }
}
finally {
    foreach ($variable in $configuredVariables) {
        [Environment]::SetEnvironmentVariable($variable, $null, "Process")
    }
}
