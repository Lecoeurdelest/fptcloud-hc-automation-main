param(
    [string]$TerraformPath = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$CheckDir = Join-Path $RepoRoot "runs\fptcloud-connect-check"
$EnvFile = Join-Path $RepoRoot ".env"

function Resolve-TerraformPath {
    param([string]$RequestedPath)

    if ($RequestedPath) {
        return $RequestedPath
    }

    $PathCommand = Get-Command terraform -ErrorAction SilentlyContinue
    if ($PathCommand) {
        return $PathCommand.Source
    }

    $WingetPackageDir = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (Test-Path $WingetPackageDir) {
        $WingetTerraform = Get-ChildItem -Path $WingetPackageDir -Recurse -Filter terraform.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -like "*Hashicorp.Terraform*" } |
            Select-Object -First 1

        if ($WingetTerraform) {
            return $WingetTerraform.FullName
        }
    }

    throw "Terraform executable was not found. Install Terraform or pass -TerraformPath C:\path\to\terraform.exe"
}

if (-not (Test-Path $EnvFile)) {
    throw "Missing .env file at $EnvFile"
}

Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), "Process")
    }
}

if (-not $env:FPTCLOUD_API_URL) {
    $env:FPTCLOUD_API_URL = "https://console-api.fptcloud.com/api"
}

Push-Location $CheckDir
try {
    $ResolvedTerraformPath = Resolve-TerraformPath $TerraformPath
    Write-Host "Using Terraform: $ResolvedTerraformPath"
    Write-Host ""

    Write-Host "Initializing Terraform provider..."
    & $ResolvedTerraformPath init -no-color -input=false

    Write-Host ""
    Write-Host "Checking FPT Cloud connection with terraform plan..."
    Write-Host "This is a non-apply check. It will not create resources."
    Write-Host ""
    & $ResolvedTerraformPath plan -no-color -input=false
}
finally {
    Pop-Location
}
