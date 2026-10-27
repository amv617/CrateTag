#!/bin/bash
# Make sure what CrateTag needs is installed, installing anything that's missing.
#
#   ./install_deps.sh            check, install if needed, print the Python to use on the last line
#   ./install_deps.sh --check    only report; install nothing (exit 1 if something is missing)
#   ./install_deps.sh --acoustid also install chromaprint (fpcalc) for AcoustID song identification
#
# What CrateTag needs: Python 3.10 or newer. Python is installed with Homebrew; Homebrew is
# installed first if it isn't there (it installs Apple's Command Line Tools itself if needed).
set -uo pipefail

CHECK_ONLY=0
WANT_ACOUSTID=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --acoustid) WANT_ACOUSTID=1 ;;
  esac
done

say() { echo "$@" >&2; }
ok()  { say "  ✓ $*"; }
no()  { say "  ✗ $*"; }

# ---------------------------------------------------------------- Homebrew
brew_bin() {
  for b in "$(command -v brew 2>/dev/null)" /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -n "$b" ] && [ -x "$b" ]; then echo "$b"; return 0; fi
  done
  return 1
}

load_brew_env() {
  local b
  b="$(brew_bin)" || return 1
  eval "$("$b" shellenv)"
}

install_homebrew() {
  say
  say "Installing Homebrew (this can take several minutes)."
  say "macOS will ask for your Mac password. Nothing appears while you type; that's normal."
  say
  local installer
  installer="$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || {
    say "Couldn't download the Homebrew installer. Check your internet connection."; return 1; }
  # The installer asks questions, so give it the keyboard even when our output is being captured.
  if { : </dev/tty; } 2>/dev/null; then
    /bin/bash -c "$installer" </dev/tty >&2 || return 1
  else
    /bin/bash -c "$installer" >&2 || return 1
  fi
  load_brew_env || return 1
  # Make brew available in future Terminal windows too (Apple Silicon installs to /opt/homebrew)
  local b line
  b="$(brew_bin)"
  line="eval \"\$($b shellenv)\""
  if [ "$b" = "/opt/homebrew/bin/brew" ] && ! grep -qsF "$line" "$HOME/.zprofile"; then
    echo "$line" >> "$HOME/.zprofile"
  fi
  return 0
}

# ---------------------------------------------------------------- Python
python_ok() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

find_python() {
  local c
  for c in python3.15 python3.14 python3.13 python3.12 python3.11 python3.10 python3 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
    if command -v "$c" >/dev/null 2>&1 && python_ok "$c"; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

# ---------------------------------------------------------------- main
say "Checking what CrateTag needs…"
load_brew_env >/dev/null 2>&1 || true
MISSING=0

if PY="$(find_python)"; then
  ok "Python $("$PY" -c 'import platform; print(platform.python_version())') ($PY)"
else
  no "Python 3.10 or newer"
  MISSING=1
fi

if B="$(brew_bin)"; then
  ok "Homebrew ($B)"
  HAVE_BREW=1
else
  if [ "$MISSING" = 1 ] || [ "$WANT_ACOUSTID" = 1 ]; then no "Homebrew"; else say "  - Homebrew (not needed)"; fi
  HAVE_BREW=0
fi

if [ "$WANT_ACOUSTID" = 1 ]; then
  if command -v fpcalc >/dev/null 2>&1 || [ -x /opt/homebrew/bin/fpcalc ] || [ -x /usr/local/bin/fpcalc ]; then
    ok "fpcalc for AcoustID"
  else
    no "fpcalc for AcoustID"
    MISSING=1
  fi
fi

if [ "$CHECK_ONLY" = 1 ]; then
  [ "$MISSING" = 0 ] && exit 0 || exit 1
fi

if [ -z "${PY:-}" ] || { [ "$WANT_ACOUSTID" = 1 ] && ! command -v fpcalc >/dev/null 2>&1; }; then
  if [ "$HAVE_BREW" = 0 ]; then
    install_homebrew || { say; say "Homebrew couldn't be installed. You can install Python from python.org instead, then run this again."; exit 1; }
    ok "Homebrew installed"
  fi
fi

if [ -z "${PY:-}" ]; then
  say
  say "Installing Python with Homebrew…"
  brew install python >&2 || { say "Python couldn't be installed."; exit 1; }
  PY="$(find_python)" || { say "Python was installed but can't be found. Open a new Terminal window and run this again."; exit 1; }
  ok "Python $("$PY" -c 'import platform; print(platform.python_version())') installed"
fi

if [ "$WANT_ACOUSTID" = 1 ] && ! command -v fpcalc >/dev/null 2>&1; then
  say
  say "Installing chromaprint (fpcalc) for AcoustID…"
  brew install chromaprint >&2 && ok "fpcalc installed" || say "  chromaprint couldn't be installed; AcoustID will stay off."
fi

say "Everything CrateTag needs is installed."
echo "$PY"
