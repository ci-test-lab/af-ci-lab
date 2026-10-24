# Clones vcpkg at the builtin-baseline pinned in vcpkg.json and bootstraps it.
# CMake installs the dependencies listed in vcpkg.json (manifest mode) during
# configure, so set VCPKG_ROOT to the same directory before configuring.
#
# Usage: install-deps-windows.ps1 [-VcpkgRoot <dir>]   (default: $env:VCPKG_ROOT or ~/vcpkg)
param(
    [string]$VcpkgRoot = $(if ($env:VCPKG_ROOT) { $env:VCPKG_ROOT } else { Join-Path $HOME 'vcpkg' })
)
$ErrorActionPreference = 'Stop'

$manifest = Join-Path $PSScriptRoot '..\..\vcpkg.json'
$baseline = (Get-Content $manifest -Raw | ConvertFrom-Json).'builtin-baseline'
if ($baseline -notmatch '^[0-9a-f]{40}$') { throw "Invalid builtin-baseline in vcpkg.json: $baseline" }

if (-not (Test-Path (Join-Path $VcpkgRoot '.git'))) {
    git clone https://github.com/microsoft/vcpkg.git $VcpkgRoot
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
}
git -C $VcpkgRoot fetch --quiet origin $baseline
if ($LASTEXITCODE) { exit $LASTEXITCODE }
git -C $VcpkgRoot checkout --quiet --detach $baseline
if ($LASTEXITCODE) { exit $LASTEXITCODE }

& (Join-Path $VcpkgRoot 'bootstrap-vcpkg.bat') -disableMetrics
if ($LASTEXITCODE) { exit $LASTEXITCODE }

Write-Host "vcpkg $baseline is ready in $VcpkgRoot; set VCPKG_ROOT to it before configuring."
