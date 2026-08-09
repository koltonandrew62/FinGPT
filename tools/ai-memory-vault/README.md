# AI Memory Vault — setup

Installer for [ai-memory-vault](https://github.com/jaredrhod/ai-memory-vault), a
system that turns an Obsidian vault into Claude Code's persistent memory: it
lives outside the model so it has no size ceiling, and the AI pulls back only
the slice a task needs.

The upstream project ships one big build file that Claude runs interactively —
it interviews you, then writes your boot config and your whole vault structure.
That build has to run **on your own machine**, where Obsidian and your files
are. These scripts do the setup around it: the folders, the build file, and the
Claude Code skill, so the only thing left is the interview.

## Prerequisites

- **Claude Code** installed locally (`claude` on your PATH)
- **git**
- **Obsidian** — optional for the build (the vault is plain Markdown), required
  to actually read and edit your notes. <https://obsidian.md>

## Run it

macOS / Linux:

```sh
chmod +x tools/ai-memory-vault/setup-memory-vault.sh
./tools/ai-memory-vault/setup-memory-vault.sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File tools\ai-memory-vault\setup-memory-vault.ps1
```

It asks two questions — where your vault should live, and where Claude Code's
working folder should live — then does the rest. Pass `--dry-run` (`-DryRun`) to
see exactly what it would touch first.

### Options

| bash | PowerShell | Meaning |
|---|---|---|
| `--vault PATH` | `-Vault PATH` | Obsidian vault folder (default `~/Documents/Brain`) |
| `--work-dir PATH` | `-WorkDir PATH` | Claude Code working folder (default `~/ai-brain`) |
| `--skill-dir PATH` | `-SkillDir PATH` | Skill install location |
| `--no-skill` | `-NoSkill` | Don't install the Claude Code skill |
| `--dry-run` | `-DryRun` | Print what would happen; change nothing |
| `--help` | `Get-Help` | Usage |

## Then build the vault

```sh
cd ~/ai-brain     # or wherever you put the working folder
claude
/ai-memory-vault
```

That runs the interactive build: about ten minutes of questions about your work,
your projects, the people around you, and the tasks you repeat — then it writes
everything. `START-HERE.md` in your working folder has the same instructions
plus a paste-ready prompt if you skipped the skill install.

## What the setup script actually does

1. Clones the upstream repo into a temp folder (retries on network failure) and
   deletes it when finished.
2. Creates your **vault folder**, empty. The interactive build fills it in —
   pre-seeding it would collide with what the build wants to write.
3. Creates your **working folder** and stages `ai-memory-vault.md` plus
   `templates/` into it.
4. Installs the build as a personal Claude Code skill at
   `~/.claude/skills/ai-memory-vault/SKILL.md`, so `/ai-memory-vault` works from
   anywhere.
5. Checks whether Obsidian is installed and tells you what to do if not.
6. Writes `START-HERE.md` with your real paths filled in.

It refuses to put the working folder inside the vault. That separation is the
point of the two-layer boot: `CLAUDE.md` (identity, vault path, the rules that
can't lapse) lives in the working folder and survives context compaction, while
`VAULT-INDEX.md` (your profile and the map of the vault) lives in the vault and
stays tool-agnostic.

### What it does not touch

Nothing is installed system-wide and no settings are changed. It never writes
inside your vault beyond creating the folder, and it never modifies an existing
`CLAUDE.md`. Re-running is safe: it refreshes the upstream build file and
templates and nothing else. If it finds a `VAULT-INDEX.md` already in the vault
it warns you and changes nothing — the interactive build will offer to back it
up before overwriting.

## Why the scripts fetch upstream instead of bundling it

The upstream content is licensed **CC BY-NC-SA 4.0**; this repository is MIT.
Vendoring the build file and templates here would put share-alike, noncommercial
content inside an MIT tree, so the scripts download them at run time instead.
You also get whatever version upstream is on, rather than a snapshot that rots.

## Note on privacy

The vault the build creates is a personal profile: who you are, the people
around you, your priorities, and optionally health, beliefs, and routine. It is
plain files on your own disk and it belongs there — **do not commit a built
vault to this repository or any other public repo.** Nothing in this folder
contains personal data, and the setup script never writes any.

## Credit

The AI Memory Vault system is by **Jared Rhodenizer** ([@jaredrhod](https://github.com/jaredrhod)),
licensed CC BY-NC-SA 4.0 — <https://github.com/jaredrhod/ai-memory-vault>.
Only these setup scripts live here.
