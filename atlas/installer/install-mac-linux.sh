#!/usr/bin/env bash
# ATLAS installer for macOS and Linux
# ---------------------------------------------------------------------------------------------
# Downloads ATLAS, installs it, adds an app + Desktop shortcut and opens it. Run it again any time
# to update: your settings (.env) and data folder are kept.
#
# One-line install (paste into Terminal):
#   curl -fsSL https://raw.githubusercontent.com/koltonandrew62/FinGPT/refs/heads/claude/eloquent-einstein-uqi56u/atlas/installer/install-mac-linux.sh | bash
#
# Optional environment variables: ATLAS_NAME (assistant name), ATLAS_INSTALL_DIR, ATLAS_BRANCH,
# ATLAS_REPO, ATLAS_ZIP (path to a downloaded ZIP), ATLAS_SOURCE_DIR (an extracted atlas folder),
# ATLAS_NO_LAUNCH=1 (don't open it at the end).
set -euo pipefail

REPO="${ATLAS_REPO:-koltonandrew62/FinGPT}"
BRANCH="${ATLAS_BRANCH:-claude/eloquent-einstein-uqi56u}"
OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
  INSTALL_DIR="${ATLAS_INSTALL_DIR:-$HOME/Applications/ATLAS-files}"
else
  INSTALL_DIR="${ATLAS_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/atlas}"
fi

say() { printf '  \033[36m>\033[0m %s\n' "$*"; }
ok() { printf '  \033[32m+\033[0m %s\n' "$*"; }
die() { printf '  \033[31mx %s\033[0m\n' "$*" >&2; exit 1; }

printf '\n  ATLAS installer\n  ---------------\n'

# 1. Python 3.10+ ---------------------------------------------------------------------------------
say "Looking for Python 3.10 or newer..."
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$(command -v "$cand")"; break
  fi
done
if [ -z "$PY" ] && [ "$OS" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
  say "Installing Python with Homebrew..."
  brew install python@3.12 && PY="$(brew --prefix)/bin/python3.12"
fi
[ -n "$PY" ] || die "Python 3.10+ is required. Install it from https://www.python.org/downloads/ and run this installer again."
"$PY" -c 'import venv' 2>/dev/null || die "Python's venv module is missing (on Debian/Ubuntu: sudo apt install python3-venv). Then run this again."
ok "Python: $PY"

# 2. Get the files ----------------------------------------------------------------------------------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SRC=""
if [ -n "${ATLAS_SOURCE_DIR:-}" ]; then
  SRC="$ATLAS_SOURCE_DIR"
elif [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/../run.py" ] && [ -z "${ATLAS_ZIP:-}" ]; then
  SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # running from an extracted copy
else
  ZIP="${ATLAS_ZIP:-}"
  if [ -z "$ZIP" ]; then
    ZIP="$TMP/atlas.zip"
    URL="https://github.com/$REPO/archive/refs/heads/$BRANCH.zip"
    say "Downloading ATLAS from github.com/$REPO ..."
    curl -fsSL "$URL" -o "$ZIP" || die "Download failed ($URL). If the repository is private, download the ZIP in your browser and run: ATLAS_ZIP=/path/to/file.zip bash install-mac-linux.sh"
  fi
  say "Unpacking..."
  "$PY" - "$ZIP" "$TMP/src" <<'PYEOF'
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PYEOF
  SRC="$(dirname "$(find "$TMP/src" -maxdepth 3 -name run.py -path '*atlas/run.py' | head -n 1)")"
  [ -f "$SRC/run.py" ] || die "The download does not contain the atlas folder."
fi
say "Installing to $INSTALL_DIR"

# 3. Copy into place, keeping the user's settings and data -----------------------------------------
mkdir -p "$INSTALL_DIR"
"$PY" - "$SRC" "$INSTALL_DIR" <<'PYEOF'
import shutil, sys
from pathlib import Path
src, dst = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
if src != dst:
    skip = {"data", ".venv", "__pycache__", ".env"}
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=lambda d, names: [n for n in names if n in skip])
PYEOF

# 4. Python environment -----------------------------------------------------------------------------
VENV="$INSTALL_DIR/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  say "Creating a private Python environment..."
  "$PY" -m venv "$VENV"
fi
say "Installing components (first run takes a few minutes)..."
"$VENV/bin/python" -m pip install --upgrade pip --disable-pip-version-check -q
"$VENV/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt" --disable-pip-version-check -q \
  || die "Installing the Python packages failed. Check your internet connection and run the installer again."
"$VENV/bin/python" -m pip install -r "$INSTALL_DIR/requirements-extras.txt" --disable-pip-version-check -q \
  || printf '  \033[33m! Optional keyboard control did not install; everything else works.\033[0m\n'
ok "Components installed"

# 5. Assistant name (stored only in your local .env) ------------------------------------------------
CURRENT="$("$VENV/bin/python" - "$INSTALL_DIR" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
from app.config import ENV_PATH, load_env_file
print(load_env_file(ENV_PATH).get("ASSISTANT_NAME", ""))
PYEOF
)"
NAME="${ATLAS_NAME:-}"
if [ -z "$NAME" ] && [ -z "$CURRENT" ] && [ -r /dev/tty ]; then
  printf '  What should your assistant be called? (press Enter for ATLAS): '
  read -r NAME </dev/tty || NAME=""
fi
NAME="$(printf '%s' "$NAME" | tr -d '/\\:*?"<>|\r\n\t' | cut -c1-40)"
if [ -n "$NAME" ]; then
  "$VENV/bin/python" - "$INSTALL_DIR" "$NAME" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
from app.config import update_env_file
update_env_file({"ASSISTANT_NAME": sys.argv[2]})
PYEOF
else
  NAME="${CURRENT:-ATLAS}"
fi

# 6. App + Desktop shortcut ----------------------------------------------------------------------------
DESKTOP="$HOME/Desktop"
if command -v xdg-user-dir >/dev/null 2>&1; then DESKTOP="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"; fi
if [ "$OS" = "Darwin" ]; then
  APP="$HOME/Applications/$NAME.app"
  for old in "$HOME/Applications"/*.app; do   # an earlier install under another name
    if [ -f "$old/Contents/MacOS/atlas-launch" ] && [ "$old" != "$APP" ]; then rm -rf "$old"; rm -f "$DESKTOP/$(basename "$old" .app)"; fi
  done
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
  cat > "$APP/Contents/MacOS/atlas-launch" <<EOF
#!/bin/bash
exec "$VENV/bin/python" "$INSTALL_DIR/launch.pyw"
EOF
  chmod +x "$APP/Contents/MacOS/atlas-launch"
  cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$NAME</string>
  <key>CFBundleDisplayName</key><string>$NAME</string>
  <key>CFBundleIdentifier</key><string>local.atlas.assistant</string>
  <key>CFBundleExecutable</key><string>atlas-launch</string>
  <key>CFBundleIconFile</key><string>atlas</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>NSMicrophoneUsageDescription</key><string>Voice commands</string>
</dict></plist>
EOF
  if command -v iconutil >/dev/null 2>&1 && command -v sips >/dev/null 2>&1; then
    SET="$TMP/atlas.iconset"; mkdir -p "$SET"
    for s in 16 32 128 256 512; do
      sips -z $s $s "$INSTALL_DIR/installer/atlas.png" --out "$SET/icon_${s}x${s}.png" >/dev/null 2>&1 || true
      d=$((s * 2)); [ $d -le 1024 ] && sips -z $d $d "$INSTALL_DIR/installer/atlas.png" --out "$SET/icon_${s}x${s}@2x.png" >/dev/null 2>&1 || true
    done
    iconutil -c icns "$SET" -o "$APP/Contents/Resources/atlas.icns" 2>/dev/null || true
  fi
  touch "$APP"
  ln -sfn "$APP" "$DESKTOP/$NAME"
  ok "App '$NAME' added to ~/Applications and your Desktop"
  LAUNCH=(open "$APP")
else
  mkdir -p "$HOME/.local/share/applications"
  ENTRY="[Desktop Entry]
Type=Application
Name=$NAME
Comment=Personal AI assistant
Exec=\"$VENV/bin/python\" \"$INSTALL_DIR/launch.pyw\"
Icon=$INSTALL_DIR/installer/atlas.png
Terminal=false
Categories=Utility;
StartupWMClass=atlas"
  printf '%s\n' "$ENTRY" > "$HOME/.local/share/applications/atlas.desktop"
  if [ -d "$DESKTOP" ]; then
    printf '%s\n' "$ENTRY" > "$DESKTOP/atlas.desktop"
    chmod +x "$DESKTOP/atlas.desktop"
    command -v gio >/dev/null 2>&1 && gio set "$DESKTOP/atlas.desktop" metadata::trusted true 2>/dev/null || true
  fi
  ok "Shortcut '$NAME' added to your applications menu and Desktop"
  LAUNCH=("$VENV/bin/python" "$INSTALL_DIR/launch.pyw")
fi

# 7. Launch ---------------------------------------------------------------------------------------------
if [ -z "${ATLAS_NO_LAUNCH:-}" ]; then
  say "Opening $NAME..."
  nohup "${LAUNCH[@]}" >/dev/null 2>&1 &
fi
printf '\n  \033[32mDone.\033[0m Next time, just open "%s" from your Desktop.\n  On first launch, paste your OpenRouter API key when asked.\n\n' "$NAME"
