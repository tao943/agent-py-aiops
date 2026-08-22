[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$')]
    [string]$RunId,

    [Parameter(Mandatory = $true)]
    [string]$OwnerUserId,

    [Parameter(Mandatory = $true)]
    [string]$KnowledgeBaseId,

    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [ValidateSet('cls')]
    [string]$EvidenceSource = 'cls',

    [switch]$Resume
)

$ErrorActionPreference = 'Stop'
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
$composeFile = Join-Path $repositoryRoot 'infra\compose.yaml'
$backendRoot = Join-Path $repositoryRoot 'apps\backend'

foreach ($command in @('docker', 'uv')) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required command is unavailable: $command"
    }
}

Write-Host '[auto-closure] applying database migrations'
Push-Location $backendRoot
try {
    & uv run alembic -x "project_config=$resolvedConfig" upgrade head
    if ($LASTEXITCODE -ne 0) {
        throw 'Database migration failed.'
    }
}
finally {
    Pop-Location
}

Write-Host '[auto-closure] starting isolated order fixture and Prometheus'
& docker compose -f $composeFile --profile live-eval up -d --build --no-deps live-eval-order-api prometheus
if ($LASTEXITCODE -ne 0) {
    throw 'Live Eval Docker services failed to start.'
}

$readiness = @(
    'http://127.0.0.1:8000/health',
    'http://127.0.0.1:9093/-/ready',
    'http://127.0.0.1:18082/health'
)
foreach ($uri in $readiness) {
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $uri -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $ready) {
        throw "Required local service is not ready: $uri"
    }
}

$prometheusReady = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    & docker compose -f $composeFile --profile live-eval exec -T prometheus wget -q -O - http://127.0.0.1:9090/-/ready *> $null
    if ($LASTEXITCODE -eq 0) {
        $prometheusReady = $true
        break
    }
    Start-Sleep -Milliseconds 500
}
if (-not $prometheusReady) {
    throw 'Prometheus is not ready inside the isolated Compose network.'
}

$arguments = @(
    'run',
    '--scenario', 'APY-LIVE-ORDER-POOL-LEAK-001',
    '--run-id', $RunId,
    '--owner-user-id', $OwnerUserId,
    '--knowledge-base-id', $KnowledgeBaseId,
    '--config', $resolvedConfig,
    '--evidence-source', $EvidenceSource,
    '--strategy', "single",
    '--auto-closure'
)
if ($Resume) {
    $arguments += '--resume'
}

Write-Host '[auto-closure] waiting for automatic alert, diagnosis, recovery, and verification'
Push-Location $repositoryRoot
try {
    & uv run --project apps/backend python -m super_ai.evaluation.live.cli @arguments
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $exitCode
