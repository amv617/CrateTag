#!/bin/bash
# First-time install of CrateTag.
#
# Checks for what CrateTag needs (Python, and Homebrew if Python has to be installed), installs
# anything missing, builds CrateTag.app, puts it in Applications and opens it.
#
# Run it from Terminal (works even though the file came from a download):
#   bash ~/Downloads/CrateTag/"Install CrateTag.command"   (or CrateTag-main, if downloaded from GitHub)
set -uo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"

pause_and_exit() {
  echo
  read -n 1 -s -r -p "Press any key to close this window." </dev/tty 2>/dev/null || true
  echo
  exit "${1:-0}"
}

close_this_window() {
  local my_tty py
  my_tty="$(tty 2>/dev/null || true)"
  if [ -n "${CRATETAG_KEEP_OPEN:-}" ] || ! command -v osascript >/dev/null 2>&1 || \
     [ -z "$my_tty" ] || [ "$my_tty" = "not a tty" ]; then
    pause_and_exit 0
  fi
  echo
  echo "Done. This window closes in 5 seconds."
  sleep 5
  local script="tell application \"Terminal\"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is \"$my_tty\" then
        close w
        return
      end if
    end repeat
  end repeat
end tell"
  py="$HERE/.venv/bin/python"
  [ -x "$py" ] || py="$(command -v python3 || true)"
  if [ -n "$py" ]; then
    nohup "$py" -c 'import os, subprocess, sys, time
os.setsid(); time.sleep(0.7)
subprocess.run(["osascript", "-e", sys.argv[1]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)' \
      "$script" </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
    sleep 0.4
    exit 0
  fi
  pause_and_exit 0
}

echo "CrateTag installer"
echo "Folder: $HERE"
echo

# Files from a download carry macOS's quarantine flag; clear it so the other scripts can run.
xattr -dr com.apple.quarantine "$HERE" 2>/dev/null || true
chmod +x "$HERE"/*.sh "$HERE"/*.command 2>/dev/null || true

# 1. Required software
DEPS_ARGS=()
if [ -n "${CRATETAG_ACOUSTID:-}" ]; then DEPS_ARGS+=(--acoustid); fi
PY="$("$HERE/install_deps.sh" "${DEPS_ARGS[@]+"${DEPS_ARGS[@]}"}" | tail -1)"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo
  echo "Setup stopped: CrateTag needs Python 3.10 or newer. See the messages above."
  pause_and_exit 1
fi

# 2. Build and install the app
echo
echo "Building CrateTag.app (a minute or two the first time)…"
if ! CRATETAG_PYTHON="$PY" "$HERE/build_mac.sh" --test --install; then
  echo
  echo "The build failed. See the messages above. You can still try: ./run_from_source.sh"
  pause_and_exit 1
fi

echo
echo "CrateTag is installed in Applications."
if [ -z "${CRATETAG_NO_LAUNCH:-}" ] && command -v open >/dev/null 2>&1; then
  open "/Applications/CrateTag.app" || true
fi
echo "Next: open Settings in CrateTag and add your free Discogs token for the best genres."
close_this_window
