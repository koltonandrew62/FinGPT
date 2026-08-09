<#
.SYNOPSIS
    Part 1 ("Get Connected") of the AI Memory Vault build, for Windows.

.DESCRIPTION
    Creates the two folders the system needs, stages the upstream build file and
    templates, and optionally installs the build as a Claude Code skill. It then
    hands off to the interactive build (Part 2), which is what actually writes
    your CLAUDE.md, VAULT-INDEX.md and vault structure.

    Upstream project: https://github.com/jaredrhod/ai-memory-vault
    by Jared Rhodenizer (@jaredrhod), CC BY-NC-SA 4.0.

    This script vendors none of that content - it fetches it at run time.

.PARAMETER Vault
    Obsidian vault folder. Default: $HOME\Documents\Brain

.PARAMETER WorkDir
    Claude Code working folder. Default: $HOME\ai-brain

.PARAMETER SkillDir
    Skill install location. Default: $HOME\.claude\skills\ai-memory-vault

.PARAMETER NoSkill
    Skip installing the build as a Claude Code skill.

.PARAMETER DryRun
    Print what would happen; change nothing.

.EXAMPLE
    .\setup-memory-vault.ps1

.EXAMPLE
    .\setup-memory-vault.ps1 -Vault 'D:\Brain' -WorkDir 'D:\ai-brain'
#>

[CmdletBinding()]
param(
    [string] $Vault,
    [string] $WorkDir,
    [string] $SkillDir = (Join-Path $HOME '.claude\skills\ai-memory-vault'),
    [switch] $NoSkill,
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'

$UpstreamRepo   = 'https://github.com/jaredrhod/ai-memory-vault.git'
$BuildFile      = 'ai-memory-vault.md'
$DefaultWorkDir = Join-Path $HOME 'ai-brain'
$DefaultVault   = Join-Path $HOME 'Documents\Brain'

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Green }
function Write-Info { param([string]$Message) Write-Host "    $Message" -ForegroundColor DarkGray }
function Write-Warn { param([string]$Message) Write-Host " !! $Message" -ForegroundColor Yellow }

# User-facing failure: a plain message, not a PowerShell exception dump.
function Write-Fail {
    param([string] $Message)
    Write-Host "error: $Message" -ForegroundColor Red
    exit 1
}

function Read-PathWithDefault {
    param([string] $Question, [string] $Default)
    $answer = Read-Host "$Question [$Default]"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return [Environment]::ExpandEnvironmentVariables($answer.Trim('"').Trim())
}

function New-Dir {
    param([string] $Path)
    if ($DryRun) { Write-Info "[dry-run] mkdir $Path"; return }
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Copy-Staged {
    param([string] $Source, [string] $Destination)
    if ($DryRun) { Write-Info "[dry-run] copy $Source -> $Destination"; return }
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

# --------------------------------------------------------------- preflight --

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Fail 'git is required but was not found on PATH. Install it from https://git-scm.com and retry.'
}

Write-Host ''
Write-Host 'AI Memory Vault - setup' -ForegroundColor White
Write-Info 'Part 1 of the build: folders, build file, and the Claude Code skill.'
Write-Info 'Upstream: github.com/jaredrhod/ai-memory-vault (Jared Rhodenizer, CC BY-NC-SA 4.0)'
if ($DryRun) { Write-Host 'Dry run - nothing will be written.' -ForegroundColor Yellow }
Write-Host ''

# ------------------------------------------------------------------- paths --

if ([string]::IsNullOrWhiteSpace($Vault)) {
    $Vault = Read-PathWithDefault 'Where should your Obsidian vault live?' $DefaultVault
}
if ([string]::IsNullOrWhiteSpace($WorkDir)) {
    $WorkDir = Read-PathWithDefault "Where should Claude Code's working folder live?" $DefaultWorkDir
}

if (-not [System.IO.Path]::IsPathRooted($Vault))   { Write-Fail "vault path must be absolute: $Vault" }
if (-not [System.IO.Path]::IsPathRooted($WorkDir)) { Write-Fail "work-dir path must be absolute: $WorkDir" }

$sep     = [System.IO.Path]::DirectorySeparatorChar
$Vault   = [System.IO.Path]::GetFullPath($Vault).TrimEnd($sep)
$WorkDir = [System.IO.Path]::GetFullPath($WorkDir).TrimEnd($sep)

# The working folder must stay outside the vault - that separation is the whole
# point of the two-layer boot (CLAUDE.md outside, VAULT-INDEX.md inside).
if ($WorkDir.StartsWith("$Vault$sep", [StringComparison]::OrdinalIgnoreCase)) {
    Write-Host 'error: the working folder must not live inside the vault.' -ForegroundColor Red
    Write-Host "    vault:     $Vault"
    Write-Host "    work-dir:  $WorkDir"
    Write-Host '  Keep the vault as pure notes and put the working folder somewhere else.'
    exit 1
}

Write-Host ''
Write-Info "vault      $Vault"
Write-Info "work-dir   $WorkDir"
if (-not $NoSkill) { Write-Info "skill      $SkillDir" }
Write-Host ''

# ---------------------------------------------------------------- upstream --

Write-Step 'Fetching the build file from upstream'
$cloneTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("ai-memory-vault-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
$src      = Join-Path $cloneTmp 'src'

try {
    $attempt = 1
    $delay   = 2
    while ($true) {
        git clone --quiet --depth 1 $UpstreamRepo $src 2>$null
        if ($LASTEXITCODE -eq 0) { break }
        if ($attempt -ge 4) {
            throw "could not clone $UpstreamRepo after $attempt attempts. Check your connection and retry."
        }
        Write-Warn "clone failed (attempt $attempt) - retrying in ${delay}s"
        Start-Sleep -Seconds $delay
        $attempt++
        $delay *= 2
    }

    if (-not (Test-Path -LiteralPath (Join-Path $src $BuildFile))) {
        throw "upstream is missing $BuildFile - the project layout may have changed."
    }
    $templateCount = (Get-ChildItem -LiteralPath (Join-Path $src 'templates') -File).Count
    Write-Info "got $BuildFile and $templateCount templates"

    # --------------------------------------------------------------- vault --

    Write-Step 'Preparing the vault folder'
    if (Test-Path -LiteralPath $Vault) {
        if (Test-Path -LiteralPath (Join-Path $Vault 'VAULT-INDEX.md')) {
            Write-Warn 'This vault already has a VAULT-INDEX.md - a memory vault is already set up here.'
            Write-Warn 'The interactive build will ask before touching it. Nothing was changed.'
        } else {
            Write-Info 'using existing folder (left untouched)'
        }
    } else {
        New-Dir $Vault
        Write-Info 'created'
    }

    # ---------------------------------------------------------- working dir --

    Write-Step 'Preparing the working folder'
    New-Dir $WorkDir
    New-Dir (Join-Path $WorkDir 'templates')

    Copy-Staged (Join-Path $src $BuildFile) (Join-Path $WorkDir $BuildFile)
    foreach ($t in Get-ChildItem -LiteralPath (Join-Path $src 'templates') -File) {
        Copy-Staged $t.FullName (Join-Path $WorkDir "templates\$($t.Name)")
    }
    Write-Info "staged $BuildFile and templates\"

    if (Test-Path -LiteralPath (Join-Path $WorkDir 'CLAUDE.md')) {
        Write-Warn "$WorkDir\CLAUDE.md already exists - left untouched."
        Write-Warn 'The interactive build appends to an existing CLAUDE.md rather than replacing it.'
    }

    # ---------------------------------------------------------------- skill --

    if (-not $NoSkill) {
        Write-Step 'Installing the Claude Code skill'
        New-Dir $SkillDir
        Copy-Staged (Join-Path $src $BuildFile) (Join-Path $SkillDir 'SKILL.md')
        Write-Info 'run it any time with /ai-memory-vault'
    }

    # ------------------------------------------------------------- obsidian --

    Write-Step 'Checking for Obsidian'
    # Join-Path throws on a null root, and these vars are unset off Windows.
    $obsidianPaths = @()
    foreach ($root in @($env:LOCALAPPDATA, ${env:ProgramFiles}, ${env:ProgramFiles(x86)})) {
        if (-not [string]::IsNullOrWhiteSpace($root)) {
            $obsidianPaths += (Join-Path $root 'Obsidian\Obsidian.exe')
        }
    }
    $obsidianFound = $false
    foreach ($p in $obsidianPaths) {
        if (Test-Path -LiteralPath $p) { $obsidianFound = $true; break }
    }
    if (-not $obsidianFound -and (Get-Command obsidian -ErrorAction SilentlyContinue)) { $obsidianFound = $true }

    if ($obsidianFound) {
        Write-Info 'found'
    } else {
        Write-Warn 'Obsidian not detected. The vault is plain Markdown so the build works without it,'
        Write-Warn 'but install it from https://obsidian.md to actually read and edit your notes.'
        Write-Warn "In Obsidian: 'Open folder as vault' -> $Vault"
        Write-Warn "Then Settings -> Files & Links -> turn ON 'Automatically update internal links'."
    }

    # ----------------------------------------------------------- START-HERE --

    Write-Step 'Writing START-HERE.md'

    $startHere = @'
# Start here

Setup (Part 1) is done. This folder is your Claude Code working folder - the
one you launch `claude` from. Your vault is the memory it writes to.

- **Vault:** `__VAULT_DIR__`
- **Working folder:** `__WORK_DIR__`

Nothing personal has been written yet. The interactive build (Part 2) does that:
it interviews you for about ten minutes, then creates your `CLAUDE.md` here and
your whole vault structure over there.

## Run the build

```
cd __WORK_DIR__
claude
```

Then either run the skill:

```
/ai-memory-vault
```

or, if you skipped the skill install, paste this:

> Read `__WORK_DIR__/ai-memory-vault.md` end to end and execute it. It is a
> system builder file - do not summarize it, run it. Start at Part 2, Phase 1.
> Part 1 is already done: my Obsidian vault is at `__VAULT_DIR__` and you are
> running in my working folder, so you have filesystem access to both. Go
> through every phase in order, interactively, and don't skip any.

## What lands where

| File | Goes | Written by |
|---|---|---|
| `CLAUDE.md` | this folder | the build (Phase 4.5) |
| `VAULT-INDEX.md` | vault root | the build (Phase 4.2) |
| `Active Priorities.md` | vault root | the build (Phase 4.4) |
| `Daily Note Template.md` | vault `01 - Daily Notes/` | the build (Phase 4.3) |
| folder structure + indexes | vault | the build (Phase 4.1, 4.6) |
| Job notes | vault, per project | the build (Phase 4.7) |

`CLAUDE.md` stays **out** of the vault on purpose - the vault stays pure notes,
so you can point other projects and other AIs at it without tangling them.

## Prefer to fill it in by hand?

`templates\` holds the starter versions of all four files, with every spot you
need marked `[FILL IN: ...]`. Copy one where it belongs and either edit it
yourself or tell Claude "fill this in for me" - each template carries that
instruction for the AI.

## Re-running setup

The setup script is safe to re-run. It refreshes the upstream build file and
templates and never touches `CLAUDE.md` or anything inside your vault.

---

Built from [ai-memory-vault](https://github.com/jaredrhod/ai-memory-vault) by
Jared Rhodenizer (@jaredrhod), CC BY-NC-SA 4.0.
'@

    $startHere = $startHere.Replace('__VAULT_DIR__', $Vault).Replace('__WORK_DIR__', $WorkDir)

    if ($DryRun) {
        Write-Info "[dry-run] write $WorkDir\START-HERE.md"
    } else {
        Set-Content -LiteralPath (Join-Path $WorkDir 'START-HERE.md') -Value $startHere -Encoding UTF8
    }
}
catch {
    Write-Host "error: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
finally {
    if (Test-Path -LiteralPath $cloneTmp) {
        Remove-Item -LiteralPath $cloneTmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# -------------------------------------------------------------------- done --

Write-Host ''
Write-Host 'Setup complete.' -ForegroundColor Green
Write-Host ''
Write-Host '  Next:'
Write-Host "    cd $WorkDir"
Write-Host '    claude'
if (-not $NoSkill) {
    Write-Host '    /ai-memory-vault'
} else {
    Write-Host '    then paste the prompt in START-HERE.md'
}
Write-Host ''
Write-Host '  The build interviews you, then writes CLAUDE.md here and your'
Write-Host "  vault structure at $Vault."
Write-Host ''
