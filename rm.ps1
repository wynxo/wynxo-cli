# One-line uninstall for wynxo (Windows).
#
#   irm https://raw.githubusercontent.com/wynxo/wynxo-cli/main/rm.ps1 | iex
#
# Uses the uninstaller from the local checkout when there is one, and
# downloads it otherwise -- so this still works for an install made before
# uninstall.py existed, and for one whose checkout is already damaged.

$ErrorActionPreference = "Stop"
$dest = if ($env:WYNXO_SRC) { $env:WYNXO_SRC } else { Join-Path $HOME ".wynxo-src" }
$raw = "https://raw.githubusercontent.com/wynxo/wynxo-cli/main/uninstall.py"

function Find-Python {
    foreach ($try in @(@("py", "-3"), @("python", $null), @("python3", $null))) {
        $exe, $flag = $try
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            $check = 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'
            if ($flag) { & $exe $flag -c $check 2>$null } else { & $exe -c $check 2>$null }
            if ($LASTEXITCODE -eq 0) { return ,@($exe, $flag) }
        }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host "  Python 3.10 or newer is required and was not found."
    Write-Host "  Remove these by hand instead:"
    Write-Host "    $dest"
    Write-Host "    $env:LOCALAPPDATA\Microsoft\WindowsApps\wynxo.cmd"
    Write-Host "    $env:APPDATA\wynxo  and  $env:LOCALAPPDATA\wynxo"
    exit 1
}
$exe, $flag = $python

$script = Join-Path $dest "uninstall.py"
$cleanup = $null
if (-not (Test-Path $script)) {
    # No local copy (an older install, or a broken checkout). Fetch into a
    # temp file, not into $dest -- that is about to be deleted.
    $script = Join-Path ([System.IO.Path]::GetTempPath()) "wynxo-uninstall.py"
    Invoke-WebRequest -UseBasicParsing -Uri $raw -OutFile $script
    $cleanup = $script
}

# Run the uninstaller from a Python outside the virtualenv that is about to
# be deleted: Windows will not remove a directory holding a running program.
if ($flag) { & $exe $flag $script @args } else { & $exe $script @args }
$status = $LASTEXITCODE

if ($cleanup) { Remove-Item -Force -ErrorAction SilentlyContinue $cleanup }
exit $status
