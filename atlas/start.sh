#!/usr/bin/env bash
# ATLAS launcher for macOS and Linux.   Usage:  ./start.sh
#
#   1. finds Python 3.10 or newer (set ATLAS_PYTHON=/path/to/python to pick one)
#   2. creates a private virtual environment in .venv on the first run
#   3. installs requirements.txt only when it changed since the last install
#   4. tries the optional requirements-extras.txt (ATLAS still runs without it)
#   5. starts ATLAS:  python run.py
#
# The script sticks to plain POSIX shell, so "sh start.sh" works too.

set -eu

say() { printf '  %s\n' "$*"; }
fail() {
    printf '\n  ERROR: %s\n\n' "$*" >&2
    exit 1
}

# --- Work from the folder this script lives in (even when started via a symlink).
self=$0
while [ -h "$self" ]; do
    link=$(readlink "$self")
    case $link in
        /*) self=$link ;;
        *) self=$(dirname "$self")/$link ;;
    esac
done
cd -P "$(dirname "$self")" || fail "Can't open the ATLAS folder."

if [ ! -f run.py ] || [ ! -f requirements.txt ]; then
    fail "run.py and requirements.txt must be next to start.sh - keep the atlas folder together."
fi

# --- 1. Python 3.10 or newer -------------------------------------------------
py_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

PY=
if [ -n "${ATLAS_PYTHON:-}" ]; then
    py_ok "$ATLAS_PYTHON" || fail "ATLAS_PYTHON=$ATLAS_PYTHON is not a working Python 3.10 or newer."
    PY=$ATLAS_PYTHON
else
    for cand in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python; do
        if command -v "$cand" >/dev/null 2>&1 && py_ok "$cand"; then
            PY=$(command -v "$cand")
            break
        fi
    done
fi

if [ -z "$PY" ]; then
    cat >&2 <<'EOF'

  ATLAS needs Python 3.10 or newer, and none was found.

    macOS:          brew install python
                    (or use the installer from https://www.python.org/downloads/)
    Debian/Ubuntu:  sudo apt install python3 python3-venv
    Fedora:         sudo dnf install python3

  Already have a newer Python somewhere else? Point ATLAS at it:
    ATLAS_PYTHON=/path/to/python3.12 ./start.sh

EOF
    exit 1
fi

echo
say "ATLAS launcher"
say "Using Python $("$PY" -c 'import platform; print(platform.python_version())') ($PY)"

# --- 2. Private virtual environment in .venv -----------------------------------
VENV=.venv
VPY=$VENV/bin/python

if [ -e "$VENV" ]; then
    if [ -x "$VPY" ] && py_ok "$VPY" && "$VPY" -m pip --version >/dev/null 2>&1; then
        :
    else
        say "The $VENV folder is incomplete or out of date - rebuilding it..."
        rm -rf "$VENV"
    fi
fi

if [ ! -d "$VENV" ]; then
    say "Creating a private Python environment in $VENV (one-time setup)..."
    if ! "$PY" -m venv "$VENV"; then
        rm -rf "$VENV"
        fail "Couldn't create the virtual environment. On Debian/Ubuntu install the venv module first (sudo apt install python3-venv), then run ./start.sh again."
    fi
    if ! "$VPY" -m pip --version >/dev/null 2>&1; then
        rm -rf "$VENV"
        fail "The new virtual environment has no pip. On Debian/Ubuntu: sudo apt install python3-venv python3-pip"
    fi
    say "Updating pip..."
    "$VPY" -m pip install --quiet --disable-pip-version-check --upgrade pip \
        || say "(Couldn't update pip - continuing with the bundled version.)"
fi

# --- 3. Requirements: install only when requirements.txt changed ---------------
# $1 = requirements file, $2 = copy saved after the last successful install
needs_install() {
    [ ! -f "$2" ] || ! cmp -s "$1" "$2"
}

STAMP=$VENV/.atlas-requirements.txt
if needs_install requirements.txt "$STAMP"; then
    say "Installing requirements (this can take a minute the first time)..."
    "$VPY" -m pip install --disable-pip-version-check -r requirements.txt \
        || fail "Installing requirements failed (see above). Check your internet connection and run ./start.sh again. If it keeps failing, delete the .venv folder and retry."
    cp requirements.txt "$STAMP"
fi

# --- 4. Optional extras: nice to have, never fatal -------------------------------
if [ -f requirements-extras.txt ]; then
    XSTAMP=$VENV/.atlas-requirements-extras.txt
    if needs_install requirements-extras.txt "$XSTAMP"; then
        say "Installing optional extras (lets ATLAS type and press shortcuts for you)..."
        if ! "$VPY" -m pip install --disable-pip-version-check -r requirements-extras.txt; then
            echo
            say "Note: the optional extras didn't install. ATLAS still works, but it won't"
            say "be able to type text or press keyboard shortcuts for you. To try again later:"
            say "  $VPY -m pip install -r requirements-extras.txt"
            echo
        fi
        # Remember this attempt either way, so a failing extra doesn't slow every start.
        cp requirements-extras.txt "$XSTAMP"
    fi
fi

# --- 5. Start ATLAS (same effect as activating the venv) ---------------------
VIRTUAL_ENV=$(pwd -P)/$VENV
PATH=$VIRTUAL_ENV/bin:$PATH
export VIRTUAL_ENV PATH
unset PYTHONHOME

say "Starting ATLAS. Keep this terminal open; press Ctrl+C to shut it down."
exec "$VIRTUAL_ENV/bin/python" run.py
