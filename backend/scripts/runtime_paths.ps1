function Get-DairyBenchRuntimeHome {
    param([Parameter(Mandatory = $true)][string]$BackendDirectory)

    if ($env:DAIRY_BENCH_HOME) {
        return [IO.Path]::GetFullPath($env:DAIRY_BENCH_HOME)
    }
    $repositoryRoot = Split-Path -Parent $BackendDirectory
    return Join-Path $repositoryRoot ".dairy-bench"
}
