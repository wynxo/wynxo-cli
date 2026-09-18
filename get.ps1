# One-line install for wynxo (Windows).
#
#   irm https://raw.githubusercontent.com/wynxo/wynxo-cli/main/get.ps1 | iex
#
# Clones into %USERPROFILE%\.wynxo-src (or updates it), then runs the real
# installer. Short on purpose: nobody reads a long script before running it.

$ErrorActionPreference = "Stop"
$repo = "https://github.com/wynxo/wynxo-cli"
$dest = if ($env:WYNXO_SRC) { $env:WYNXO_SRC } else { Join-Path $HOME ".wynxo-src" }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "  git is required. Install it from https://git-scm.com/download/win"
    exit 1
}

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
    Write-Host "  Python 3.10 or newer is required."
    Write-Host "  Install it from https://python.org/downloads"
    Write-Host "  Tick 'Add python.exe to PATH' in the installer."
    exit 1
}
$exe, $flag = $python

if (Test-Path (Join-Path $dest ".git")) {
    Write-Host "  updating $dest"
    git -C $dest pull --ff-only --quiet
} else {
    Write-Host "  cloning into $dest"
    git clone --quiet --depth 1 $repo $dest
}

$installer = Join-Path $dest "install.py"
if ($flag) { & $exe $flag $installer @args } else { & $exe $installer @args }
exit $LASTEXITCODE
