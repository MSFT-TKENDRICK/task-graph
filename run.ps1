<#
.SYNOPSIS
    Task runner for the task-graph workspace.

.DESCRIPTION
    Wraps virtualenv bootstrap, the interactive shell, the daily commands and
    the checks, so none of it has to be remembered or typed with a path.

    Run `.\run.ps1` with no arguments to list the available tasks. Any task
    this file does not define is forwarded to `tg`, so `.\run.ps1 triage
    --limit 5` works without a case for it here.

    The real provisioning lives in scripts/app_setup.py, which the GitHub
    Copilot app runs directly as its Setup script. This does not duplicate it.

.EXAMPLE
    .\run.ps1 setup
    .\run.ps1 shell
    .\run.ps1 why task-a1b2c3
#>

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Task = if ($args.Count -ge 1) { [string]$args[0] } else { 'help' }
$Rest = if ($args.Count -gt 1) { @($args[1..($args.Count - 1)]) } else { @() }

$Root = $PSScriptRoot
$VenvDir = Join-Path $Root '.venv'
$Py = Join-Path $VenvDir 'Scripts\python.exe'
$SetupScript = Join-Path $Root 'scripts\app_setup.py'

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Resolve-BasePython {
    # The launcher first: it resolves the newest interpreter even when none is
    # on PATH, which is the usual state of a fresh Windows checkout.
    if (Get-Command py -ErrorAction SilentlyContinue) { return @('py', '-3') }
    if (Get-Command python -ErrorAction SilentlyContinue) { return @('python') }
    throw "No Python interpreter found. Install Python 3.11+ and re-run."
}

function Invoke-Setup {
    $base = Resolve-BasePython
    $exe = $base[0]
    $prefix = if ($base.Count -gt 1) { @($base[1..($base.Count - 1)]) } else { @() }
    $arguments = @($prefix) + @($SetupScript) + @($Rest)
    Write-Host "$ $exe $($arguments -join ' ')" -ForegroundColor DarkGray
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "setup failed" }
}

function Initialize-Environment {
    if (-not (Test-Path $Py)) {
        Write-Step "No virtualenv yet; running setup first"
        $script:Rest = @()
        Invoke-Setup
    }
}

function Invoke-Tg {
    param([string[]]$Arguments)
    Initialize-Environment
    & $Py -m task_graph.cli @Arguments
    exit $LASTEXITCODE
}

function Invoke-Module {
    param([string]$Module, [string[]]$Arguments)
    Initialize-Environment
    & $Py -m $Module @Arguments
    exit $LASTEXITCODE
}

function Show-Help {
    Write-Host ""
    Write-Host "task-graph workspace tasks" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Setup"
    Write-Host "    setup          Create .venv and install task-graph editable"
    Write-Host "    doctor         Check connectors, embeddings and storage"
    Write-Host "    init           Register the MCP server for Copilot CLI"
    Write-Host ""
    Write-Host "  Use"
    Write-Host "    shell          Interactive tg shell (what the app's Run button opens)"
    Write-Host "    sync           Pull from every available source"
    Write-Host "    triage         Ranked open work, with explanations"
    Write-Host "    status         Local graph counts"
    Write-Host ""
    Write-Host "  Checks"
    Write-Host "    test           Run the offline test suite"
    Write-Host "    lint           ruff check"
    Write-Host "    fmt            ruff format"
    Write-Host ""
    Write-Host "  Housekeeping"
    Write-Host "    clean          Remove caches and build artifacts (.venv kept)"
    Write-Host "    clean-all      Also remove .venv"
    Write-Host ""
    Write-Host "Any other task is forwarded to tg, and extra arguments come with it:" -ForegroundColor DarkGray
    Write-Host "    .\run.ps1 triage --limit 5" -ForegroundColor DarkGray
    Write-Host "    .\run.ps1 search 'billing timeout'" -ForegroundColor DarkGray
    Write-Host ""
}

function Invoke-Clean {
    Write-Step "Removing caches and build artifacts"
    foreach ($pattern in '__pycache__', '.pytest_cache', '.ruff_cache', '*.egg-info') {
        Get-ChildItem -Path $Root -Filter $pattern -Recurse -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notlike "$VenvDir*" } |
            ForEach-Object { Remove-Item -Recurse -Force $_.FullName -ErrorAction SilentlyContinue }
    }
    Write-Host "Clean. (.venv kept - use 'clean-all' to remove it too.)"
}

switch ($Task) {
    'help' { Show-Help }
    '-h' { Show-Help }
    '--help' { Show-Help }
    'setup' { Invoke-Setup }
    'test' { Invoke-Module -Module 'pytest' -Arguments $Rest }
    'lint' { Invoke-Module -Module 'ruff' -Arguments (@('check') + $Rest) }
    'fmt' { Invoke-Module -Module 'ruff' -Arguments (@('format') + $Rest) }
    'clean' { Invoke-Clean }
    'clean-all' {
        Invoke-Clean
        if (Test-Path $VenvDir) {
            Write-Step "Removing .venv"
            Remove-Item -Recurse -Force $VenvDir
        }
    }
    default { Invoke-Tg -Arguments (@($Task) + $Rest) }
}
