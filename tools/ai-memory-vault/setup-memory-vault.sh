#!/usr/bin/env bash
#
# setup-memory-vault.sh — Part 1 ("Get Connected") of the AI Memory Vault build.
#
# Creates the two folders the system needs, stages the upstream build file and
# templates, and optionally installs the build as a Claude Code skill. It then
# hands off to the interactive build (Part 2), which is what actually writes
# your CLAUDE.md, VAULT-INDEX.md and vault structure.
#
# Upstream project: https://github.com/jaredrhod/ai-memory-vault
# by Jared Rhodenizer (@jaredrhod), CC BY-NC-SA 4.0.
#
# This script vendors none of that content — it fetches it at run time.

set -euo pipefail

UPSTREAM_REPO="https://github.com/jaredrhod/ai-memory-vault.git"
BUILD_FILE="ai-memory-vault.md"

DEFAULT_WORK_DIR="$HOME/ai-brain"
DEFAULT_VAULT_DIR="$HOME/Documents/Brain"
DEFAULT_SKILL_DIR="$HOME/.claude/skills/ai-memory-vault"

WORK_DIR=""
VAULT_DIR=""
SKILL_DIR="$DEFAULT_SKILL_DIR"
INSTALL_SKILL=1
DRY_RUN=0

CLONE_TMP=""

# ---------------------------------------------------------------- output ----

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_GRN=$'\033[32m'
  C_YEL=$'\033[33m'; C_RED=$'\033[31m'; C_OFF=$'\033[0m'
else
  C_BOLD=""; C_DIM=""; C_GRN=""; C_YEL=""; C_RED=""; C_OFF=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$C_GRN$C_BOLD" "$C_OFF" "$*"; }
info() { printf '    %s%s%s\n' "$C_DIM" "$*" "$C_OFF"; }
warn() { printf '%s !!%s %s\n' "$C_YEL" "$C_OFF" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
${C_BOLD}setup-memory-vault.sh${C_OFF} — set up the AI Memory Vault for Claude Code

${C_BOLD}Usage:${C_OFF}
  ./setup-memory-vault.sh [options]

${C_BOLD}Options:${C_OFF}
  --vault PATH       Obsidian vault folder    (default: $DEFAULT_VAULT_DIR)
  --work-dir PATH    Claude Code working dir  (default: $DEFAULT_WORK_DIR)
  --skill-dir PATH   Skill install location   (default: $DEFAULT_SKILL_DIR)
  --no-skill         Skip installing the build as a Claude Code skill
  --dry-run          Print what would happen; change nothing
  -h, --help         Show this help

With no --vault/--work-dir, you are prompted (defaults shown in brackets).

${C_BOLD}What it does:${C_OFF}
  1. Fetches the upstream build file and templates from
     $UPSTREAM_REPO
  2. Creates your vault folder (empty — the interactive build fills it in)
  3. Creates your working folder and stages the build file + templates there
  4. Installs the build as a Claude Code skill, so you can run /ai-memory-vault
  5. Writes START-HERE.md with the exact prompt to kick off the build

${C_BOLD}What it does NOT do:${C_OFF}
  Nothing is installed system-wide, no settings are changed, and no existing
  file is overwritten except the upstream copies it manages itself. Your
  CLAUDE.md and everything inside your vault are never touched.
EOF
}

# ------------------------------------------------------------------ util ----

expand_tilde() {
  case "$1" in
    "~")   printf '%s' "$HOME" ;;
    "~/"*) printf '%s' "$HOME/${1#\~/}" ;;
    *)     printf '%s' "$1" ;;
  esac
}

prompt_for() {
  # prompt_for <varname> <question> <default>
  local __var="$1" __q="$2" __def="$3" __ans=""
  if [ ! -t 0 ]; then
    printf -v "$__var" '%s' "$__def"
    return
  fi
  printf '%s %s[%s]%s: ' "$__q" "$C_DIM" "$__def" "$C_OFF"
  IFS= read -r __ans || true
  [ -n "$__ans" ] || __ans="$__def"
  printf -v "$__var" '%s' "$(expand_tilde "$__ans")"
}

do_mkdir() {
  if [ "$DRY_RUN" -eq 1 ]; then
    info "[dry-run] mkdir -p $1"
  else
    mkdir -p "$1"
  fi
}

do_copy() {
  # do_copy <src> <dest>
  if [ "$DRY_RUN" -eq 1 ]; then
    info "[dry-run] cp $1 -> $2"
  else
    cp -f "$1" "$2"
  fi
}

cleanup() {
  [ -n "$CLONE_TMP" ] && [ -d "$CLONE_TMP" ] && rm -rf "$CLONE_TMP"
  return 0
}
trap cleanup EXIT

# --------------------------------------------------------------- options ----

while [ $# -gt 0 ]; do
  case "$1" in
    --vault)     [ $# -ge 2 ] || die "--vault needs a path";     VAULT_DIR="$(expand_tilde "$2")"; shift 2 ;;
    --work-dir)  [ $# -ge 2 ] || die "--work-dir needs a path";  WORK_DIR="$(expand_tilde "$2")";  shift 2 ;;
    --skill-dir) [ $# -ge 2 ] || die "--skill-dir needs a path"; SKILL_DIR="$(expand_tilde "$2")"; shift 2 ;;
    --no-skill)  INSTALL_SKILL=0; shift ;;
    --dry-run)   DRY_RUN=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           die "unknown option: $1 (try --help)" ;;
  esac
done

command -v git >/dev/null 2>&1 || die "git is required but was not found on PATH."

say ""
say "${C_BOLD}AI Memory Vault — setup${C_OFF}"
say "${C_DIM}Part 1 of the build: folders, build file, and the Claude Code skill.${C_OFF}"
say "${C_DIM}Upstream: github.com/jaredrhod/ai-memory-vault (Jared Rhodenizer, CC BY-NC-SA 4.0)${C_OFF}"
[ "$DRY_RUN" -eq 1 ] && say "${C_YEL}Dry run — nothing will be written.${C_OFF}"
say ""

# ----------------------------------------------------------------- paths ----

[ -n "$VAULT_DIR" ] || prompt_for VAULT_DIR \
  "Where should your Obsidian vault live?" "$DEFAULT_VAULT_DIR"
[ -n "$WORK_DIR" ] || prompt_for WORK_DIR \
  "Where should Claude Code's working folder live?" "$DEFAULT_WORK_DIR"

case "$VAULT_DIR" in /*) ;; *) die "vault path must be absolute: $VAULT_DIR" ;; esac
case "$WORK_DIR"  in /*) ;; *) die "work-dir path must be absolute: $WORK_DIR" ;; esac

# The working folder must stay outside the vault — that separation is the whole
# point of the two-layer boot (CLAUDE.md outside, VAULT-INDEX.md inside).
case "$WORK_DIR/" in
  "$VAULT_DIR"/*) die "the working folder must not live inside the vault.
    vault:     $VAULT_DIR
    work-dir:  $WORK_DIR
  Keep the vault as pure notes and put the working folder somewhere else." ;;
esac

say ""
info "vault      $VAULT_DIR"
info "work-dir   $WORK_DIR"
[ "$INSTALL_SKILL" -eq 1 ] && info "skill      $SKILL_DIR"
say ""

# ---------------------------------------------------------------- upstream --

step "Fetching the build file from upstream"
CLONE_TMP="$(mktemp -d "${TMPDIR:-/tmp}/ai-memory-vault.XXXXXX")"

attempt=1
delay=2
until git clone --quiet --depth 1 "$UPSTREAM_REPO" "$CLONE_TMP/src" 2>/dev/null; do
  if [ "$attempt" -ge 4 ]; then
    die "could not clone $UPSTREAM_REPO after $attempt attempts. Check your connection and retry."
  fi
  warn "clone failed (attempt $attempt) — retrying in ${delay}s"
  sleep "$delay"
  attempt=$((attempt + 1))
  delay=$((delay * 2))
done

SRC="$CLONE_TMP/src"
[ -f "$SRC/$BUILD_FILE" ] || die "upstream is missing $BUILD_FILE — the project layout may have changed."
info "got $BUILD_FILE and $(ls "$SRC/templates" | wc -l | tr -d ' ') templates"

# ------------------------------------------------------------------ vault ---

step "Preparing the vault folder"
if [ -d "$VAULT_DIR" ]; then
  if [ -f "$VAULT_DIR/VAULT-INDEX.md" ]; then
    warn "This vault already has a VAULT-INDEX.md — a memory vault is already set up here."
    warn "The interactive build will ask before touching it. Nothing was changed."
  else
    info "using existing folder (left untouched)"
  fi
else
  do_mkdir "$VAULT_DIR"
  [ "$DRY_RUN" -eq 1 ] || info "created"
fi

# ------------------------------------------------------------- working dir --

step "Preparing the working folder"
do_mkdir "$WORK_DIR"
do_mkdir "$WORK_DIR/templates"

do_copy "$SRC/$BUILD_FILE" "$WORK_DIR/$BUILD_FILE"
for t in "$SRC"/templates/*; do
  do_copy "$t" "$WORK_DIR/templates/$(basename "$t")"
done
info "staged $BUILD_FILE and templates/"

if [ -f "$WORK_DIR/CLAUDE.md" ]; then
  warn "$WORK_DIR/CLAUDE.md already exists — left untouched."
  warn "The interactive build appends to an existing CLAUDE.md rather than replacing it."
fi

# ------------------------------------------------------------------ skill ---

if [ "$INSTALL_SKILL" -eq 1 ]; then
  step "Installing the Claude Code skill"
  do_mkdir "$SKILL_DIR"
  do_copy "$SRC/$BUILD_FILE" "$SKILL_DIR/SKILL.md"
  info "run it any time with /ai-memory-vault"
fi

# -------------------------------------------------------------- obsidian ----

step "Checking for Obsidian"
OBSIDIAN_FOUND=0
for candidate in \
  "/Applications/Obsidian.app" \
  "$HOME/Applications/Obsidian.app" \
  "/var/lib/flatpak/app/md.obsidian.Obsidian" \
  "$HOME/.local/share/flatpak/app/md.obsidian.Obsidian"
do
  [ -e "$candidate" ] && OBSIDIAN_FOUND=1 && break
done
command -v obsidian >/dev/null 2>&1 && OBSIDIAN_FOUND=1

if [ "$OBSIDIAN_FOUND" -eq 1 ]; then
  info "found"
else
  warn "Obsidian not detected. The vault is plain Markdown so the build works without it,"
  warn "but install it from https://obsidian.md to actually read and edit your notes."
  warn "In Obsidian: 'Open folder as vault' -> $VAULT_DIR"
  warn "Then Settings -> Files & Links -> turn ON 'Automatically update internal links'."
fi

# ------------------------------------------------------------ START-HERE ----

step "Writing START-HERE.md"

render_start_here() {
  while IFS= read -r line; do
    line=${line//__VAULT_DIR__/$VAULT_DIR}
    line=${line//__WORK_DIR__/$WORK_DIR}
    printf '%s\n' "$line"
  done <<'TEMPLATE'
# Start here

Setup (Part 1) is done. This folder is your Claude Code working folder — the
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
> system builder file — do not summarize it, run it. Start at Part 2, Phase 1.
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

`CLAUDE.md` stays **out** of the vault on purpose — the vault stays pure notes,
so you can point other projects and other AIs at it without tangling them.

## Prefer to fill it in by hand?

`templates/` holds the starter versions of all four files, with every spot you
need marked `[FILL IN: ...]`. Copy one where it belongs and either edit it
yourself or tell Claude "fill this in for me" — each template carries that
instruction for the AI.

## Re-running setup

The setup script is safe to re-run. It refreshes the upstream build file and
templates and never touches `CLAUDE.md` or anything inside your vault.

---

Built from [ai-memory-vault](https://github.com/jaredrhod/ai-memory-vault) by
Jared Rhodenizer (@jaredrhod), CC BY-NC-SA 4.0.
TEMPLATE
}

if [ "$DRY_RUN" -eq 1 ]; then
  info "[dry-run] write $WORK_DIR/START-HERE.md"
else
  render_start_here > "$WORK_DIR/START-HERE.md"
fi

# ------------------------------------------------------------------ done ----

say ""
say "${C_GRN}${C_BOLD}Setup complete.${C_OFF}"
say ""
say "  Next:"
say "    ${C_BOLD}cd $WORK_DIR${C_OFF}"
say "    ${C_BOLD}claude${C_OFF}"
if [ "$INSTALL_SKILL" -eq 1 ]; then
  say "    ${C_BOLD}/ai-memory-vault${C_OFF}"
else
  say "    then paste the prompt in START-HERE.md"
fi
say ""
say "  The build interviews you, then writes CLAUDE.md here and your"
say "  vault structure at $VAULT_DIR."
say ""
