# Engineer Paper Radar - one-line installer for Windows (PowerShell 5.1 or 7).
#
#   irm https://raw.githubusercontent.com/jdong-p/engineer-paper-radar/main/install.ps1 | iex
#
# 1. Git and Python 3.8+: installs Git.Git / Python.Python.3.12 with winget when missing, then reloads PATH in
#    this window, so the new git / python work without opening a new terminal.
# 2. The radar: clones it into Documents\engineer-paper-radar (or `git pull` when it is already there, or when
#    this script runs from inside a radar folder, e.g. Install_Radar.cmd).
# 3. Hands off to `install.py --setup`: two optional questions, then STARTUP.html opens.
#
# Options (environment variables, since `irm | iex` takes no arguments):
#   $env:RADAR_DIR = 'D:\radar'   clone folder (default: <Documents>\engineer-paper-radar)
#   $env:RADAR_DRY_RUN = '1'      only print what would happen; nothing is installed, cloned or started
# Plain ASCII on purpose: Windows PowerShell 5.1 reads a .ps1 without a BOM in the ANSI code page.

& {
    $ErrorActionPreference = 'Stop'
    $RepoUrl = 'https://github.com/jdong-p/engineer-paper-radar.git'
    $Dry = [bool]$env:RADAR_DRY_RUN

    function Say($msg, $color = 'Cyan') { Write-Host "  $msg" -ForegroundColor $color }
    function Act($what, [scriptblock]$do) {
        if ($Dry) { Say "[dry run] $what" 'DarkYellow' } else { Say $what; & $do }
    }
    function Refresh-Path {
        $m = [Environment]::GetEnvironmentVariable('Path', 'Machine')
        $u = [Environment]::GetEnvironmentVariable('Path', 'User')
        $env:Path = (@($m, $u) | Where-Object { $_ }) -join ';'
        foreach ($d in @("$env:ProgramFiles\Git\cmd", "$env:LOCALAPPDATA\Programs\Git\cmd")) {
            if ((Test-Path $d) -and ($env:Path -notlike "*$d*")) { $env:Path += ";$d" }
        }
    }
    function Find-Python {
        # the first Python 3.8+ as an array: @('py','-3'), @('python'), @('C:\...\python.exe')
        $cands = @(@('py', '-3'), @('python'), @('python3'))
        foreach ($root in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles")) {
            if (Test-Path $root) {
                Get-ChildItem $root -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
                    Sort-Object Name -Descending | ForEach-Object {
                        $exe = Join-Path $_.FullName 'python.exe'
                        if (Test-Path $exe) { $cands += , @($exe) }
                    }
            }
        }
        foreach ($c in $cands) {
            if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
            $rest = @($c | Select-Object -Skip 1)
            try {
                $ok = & $c[0] @rest -c "import sys; print(int(sys.version_info >= (3, 8)))" 2>$null
            } catch { $ok = $null }
            if ("$ok".Trim() -eq '1') { return , $c }
        }
        return $null
    }
    function Winget-Install($id) {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            throw "winget is not available. Install $id by hand (https://git-scm.com / https://www.python.org/downloads/), then run this again."
        }
        Act "winget install --id $id -e" {
            winget install --id $id -e --source winget --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) { Say "winget returned $LASTEXITCODE for $id (already installed?)" 'Yellow' }
        }
        if (-not $Dry) { Refresh-Path }
    }

    Write-Host ''
    Write-Host '  Engineer Paper Radar - installer' -ForegroundColor Green
    if ($Dry) { Say 'dry run: nothing will be installed, cloned or started' 'DarkYellow' }

    # 1. Git
    Refresh-Path
    if (Get-Command git -ErrorAction SilentlyContinue) { Say ("Git: " + (git --version)) 'Green' }
    else { Say 'Git: missing' 'Yellow'; Winget-Install 'Git.Git' }
    if (-not $Dry -and -not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw 'Git is still not found. Open a new PowerShell window and run the installer again.'
    }

    # 2. Python 3.8+
    $py = Find-Python
    if ($py) { Say ("Python 3.8+: " + ($py -join ' ')) 'Green' }
    else { Say 'Python 3.8+: missing' 'Yellow'; Winget-Install 'Python.Python.3.12'; $py = Find-Python }
    if (-not $py) {
        if ($Dry) { $py = @('py', '-3') }
        else { throw 'Python 3.8+ is still not found. Open a new PowerShell window and run the installer again.' }
    }

    # 3. The radar folder
    $here = $null
    foreach ($d in @($PSScriptRoot, (Get-Location).Path)) {
        if ($d -and (Test-Path (Join-Path $d 'briefing.py')) -and (Test-Path (Join-Path $d 'install.py'))) { $here = $d; break }
    }
    if (-not $here) {
        $here = $env:RADAR_DIR
        if (-not $here) { $here = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'engineer-paper-radar' }
    }
    if (Test-Path (Join-Path $here '.git')) {
        Act "git pull --ff-only in $here" {
            git -C $here pull --ff-only
            if ($LASTEXITCODE -ne 0) { Say 'git pull did not run (local changes?): continuing with this version' 'Yellow' }
        }
    } elseif (Test-Path (Join-Path $here 'briefing.py')) {
        Say "$here is not a git clone (ZIP copy): STARTUP.html offers Update Radar Now" 'Yellow'
    } elseif ((Test-Path $here) -and (Get-ChildItem $here -Force | Select-Object -First 1)) {
        throw "$here exists and is not the radar. Set `$env:RADAR_DIR to another folder and run again."
    } else {
        Act "git clone $RepoUrl $here" {
            git clone $RepoUrl $here
            if ($LASTEXITCODE -ne 0) { throw 'git clone failed (offline?)' }
        }
    }

    # 4. Setup and the Control Center
    $rest = @($py | Select-Object -Skip 1)
    Act ("cd $here; " + ($py -join ' ') + ' install.py --setup') {
        Set-Location $here
        & $py[0] @rest install.py --setup
    }
}
