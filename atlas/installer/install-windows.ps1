# ATLAS installer for Windows
# ---------------------------------------------------------------------------------------------
# Downloads ATLAS, installs it to %LOCALAPPDATA%\ATLAS, adds a Desktop + Start-menu shortcut and
# opens it. Run it again any time to update: your settings (.env) and data folder are kept.
#
# One-line install (paste into PowerShell):
#   irm https://raw.githubusercontent.com/koltonandrew62/FinGPT/refs/heads/claude/eloquent-einstein-uqi56u/atlas/installer/install-windows.ps1 | iex
#
# Optional environment variables: ATLAS_NAME (assistant name), ATLAS_INSTALL_DIR, ATLAS_BRANCH,
# ATLAS_REPO, ATLAS_ZIP (path to an already-downloaded ZIP of the repo).
# This file is ASCII-only on purpose (it is often run through Invoke-Expression).

& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is far faster without the progress bar
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

    $Repo = if ($env:ATLAS_REPO) { $env:ATLAS_REPO } else { 'koltonandrew62/FinGPT' }
    $Branch = if ($env:ATLAS_BRANCH) { $env:ATLAS_BRANCH } else { 'claude/eloquent-einstein-uqi56u' }
    $InstallDir = if ($env:ATLAS_INSTALL_DIR) { $env:ATLAS_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA 'ATLAS' }

    function Say([string]$m) { Write-Host "  > $m" -ForegroundColor Cyan }
    function Ok([string]$m) { Write-Host "  + $m" -ForegroundColor Green }

    function Find-Python {
        # Returns the full path of a Python 3.10+ interpreter, or $null.
        $ErrorActionPreference = 'Continue'
        $probe = 'import sys; print(sys.version_info[0]*100+sys.version_info[1]); print(sys.executable)'
        $candidates = @()
        if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @('py', '-3') }
        if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += , @('python') }
        foreach ($dir in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles\Python*")) {
            Get-ChildItem -Path $dir -Filter 'python.exe' -Recurse -Depth 2 -ErrorAction SilentlyContinue |
                ForEach-Object { $candidates += , @($_.FullName) }
        }
        foreach ($c in $candidates) {
            try {
                $exe = $c[0]
                $rest = @($c | Select-Object -Skip 1)
                $out = & $exe @rest -c $probe 2>$null
                if ($LASTEXITCODE -eq 0 -and $out -and $out.Count -ge 2) {
                    if ([int]$out[0] -ge 310) { return $out[1].Trim() }
                }
            } catch {}
        }
        return $null
    }

    Write-Host ''
    Write-Host '  ATLAS installer' -ForegroundColor White
    Write-Host '  ---------------' -ForegroundColor DarkGray

    # 1. Python ----------------------------------------------------------------------------------
    Say 'Looking for Python 3.10 or newer...'
    $py = Find-Python
    if (-not $py) {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Say 'Python not found - installing Python 3.12 with winget (this can take a minute)...'
            & winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
            $py = Find-Python
        }
        if (-not $py) {
            throw 'Python 3.10+ is required. Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this installer again.'
        }
    }
    Ok "Python: $py"

    # 2. Get the files -----------------------------------------------------------------------------
    $tmp = Join-Path $env:TEMP ('atlas-install-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $source = $null
        $local = if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { $null }
        if ($local -and (Test-Path (Join-Path $local 'run.py')) -and -not $env:ATLAS_ZIP) {
            $source = $local   # running from an extracted copy: install that copy
            Say "Installing from $source"
        } else {
            $zip = $env:ATLAS_ZIP
            if (-not $zip) {
                $zip = Join-Path $tmp 'atlas.zip'
                $url = "https://github.com/$Repo/archive/refs/heads/$Branch.zip"
                Say "Downloading ATLAS from github.com/$Repo ..."
                try {
                    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
                } catch {
                    throw "Download failed ($url). If the repository is private, download the ZIP from GitHub in your browser and run: `$env:ATLAS_ZIP='C:\path\to\file.zip'; then this installer again."
                }
            }
            Say 'Unpacking...'
            Expand-Archive -Path $zip -DestinationPath $tmp -Force
            $found = Get-ChildItem -Path $tmp -Directory -Recurse -Depth 2 |
                Where-Object { Test-Path (Join-Path $_.FullName 'run.py') } | Select-Object -First 1
            if (-not $found) { throw 'The download does not contain the atlas folder.' }
            $source = $found.FullName
        }

        # 3. Copy into place, keeping the user's settings and data ---------------------------------
        Say "Installing to $InstallDir"
        New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
        & robocopy $source $InstallDir /E /XD data .venv __pycache__ /XF .env /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "Copying files failed (robocopy exit code $LASTEXITCODE)." }
    } finally {
        Remove-Item -Recurse -Force -Path $tmp -ErrorAction SilentlyContinue
    }

    # 4. Python environment ------------------------------------------------------------------------
    $venv = Join-Path $InstallDir '.venv'
    $venvPy = Join-Path $venv 'Scripts\python.exe'
    $venvPyw = Join-Path $venv 'Scripts\pythonw.exe'
    if (-not (Test-Path $venvPy)) {
        Say 'Creating a private Python environment...'
        & $py -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw 'Creating the Python environment failed.' }
    }
    Say 'Installing components (first run takes a few minutes)...'
    & $venvPy -m pip install --upgrade pip --disable-pip-version-check -q | Out-Host
    & $venvPy -m pip install -r (Join-Path $InstallDir 'requirements.txt') --disable-pip-version-check -q | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Installing the Python packages failed. Check your internet connection and run the installer again.' }
    & $venvPy -m pip install -r (Join-Path $InstallDir 'requirements-extras.txt') --disable-pip-version-check -q | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-Host '  ! Optional keyboard control did not install; everything else works.' -ForegroundColor Yellow }
    Ok 'Components installed'

    # 5. Assistant name (stored only in your local .env) -------------------------------------------
    $envFile = Join-Path $InstallDir '.env'
    $current = $null
    if (Test-Path $envFile) {
        $line = Select-String -Path $envFile -Pattern '^\s*ASSISTANT_NAME\s*=\s*(.+)$' | Select-Object -First 1
        if ($line) { $current = $line.Matches[0].Groups[1].Value.Trim().Trim('"').Trim("'") }
    }
    $name = $env:ATLAS_NAME
    if (-not $name -and -not $current) {
        $name = Read-Host '  What should your assistant be called? (press Enter for ATLAS)'
    }
    if ($name) {
        $name = ($name -replace '[\\/:*?"<>|\r\n\t]', '').Trim()
        if ($name.Length -gt 40) { $name = $name.Substring(0, 40) }
    }
    if ($name) {
        & $venvPy -c "import sys; sys.path.insert(0, sys.argv[1]); from app.config import update_env_file; update_env_file({'ASSISTANT_NAME': sys.argv[2]})" $InstallDir $name
        if ($LASTEXITCODE -ne 0) { throw 'Saving the assistant name failed.' }
    } elseif ($current) {
        $name = $current
    } else {
        $name = 'ATLAS'
    }
    $shortcutName = ($name -replace '[\\/:*?"<>|]', '').Trim()
    if (-not $shortcutName) { $shortcutName = 'ATLAS' }

    # 6. Shortcuts ---------------------------------------------------------------------------------
    $launcher = Join-Path $InstallDir 'launch.pyw'
    $icon = Join-Path $InstallDir 'installer\atlas.ico'
    $shell = New-Object -ComObject WScript.Shell
    $places = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))
    foreach ($folder in $places) {
        if (-not $folder) { continue }
        # remove a shortcut from an earlier install under a different name
        Get-ChildItem -Path $folder -Filter '*.lnk' -ErrorAction SilentlyContinue | ForEach-Object {
            try {
                $old = $shell.CreateShortcut($_.FullName)
                if ($old.Arguments -like "*$launcher*" -and $_.BaseName -ne $shortcutName) { Remove-Item $_.FullName -Force }
            } catch {}
        }
        $lnk = $shell.CreateShortcut((Join-Path $folder "$shortcutName.lnk"))
        $lnk.TargetPath = $venvPyw
        $lnk.Arguments = '"' + $launcher + '"'
        $lnk.WorkingDirectory = $InstallDir
        $lnk.IconLocation = "$icon,0"
        $lnk.Description = "$shortcutName - personal AI assistant"
        $lnk.Save()
    }
    Ok "Shortcut '$shortcutName' added to your Desktop and Start menu"

    # 7. Launch ------------------------------------------------------------------------------------
    if (-not $env:ATLAS_NO_LAUNCH) {
        Say "Opening $shortcutName..."
        Start-Process -FilePath $venvPyw -ArgumentList ('"' + $launcher + '"') -WorkingDirectory $InstallDir
    }
    Write-Host ''
    Write-Host "  Done. Next time, just double-click '$shortcutName' on your Desktop." -ForegroundColor Green
    Write-Host '  On first launch, paste your OpenRouter API key when asked.' -ForegroundColor Gray
    Write-Host ''
}
