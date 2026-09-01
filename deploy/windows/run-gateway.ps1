$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $ProjectRoot

# Load only simple KEY=VALUE lines from the ignored local lab environment.
# Values are placed in the child process environment and are never printed.
$LabEnvFile = Join-Path $ProjectRoot "dev\lab\.env"
if (Test-Path -LiteralPath $LabEnvFile) {
    foreach ($Line in Get-Content -LiteralPath $LabEnvFile) {
        if ($Line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            if (-not [Environment]::GetEnvironmentVariable($Matches[1], "Process")) {
                [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2])
            }
        }
    }
}

uv run --project $ProjectRoot domoai-mcp-gateway
