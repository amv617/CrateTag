#!/bin/bash
# Build CrateTag.app for macOS.
# Usage:  ./build_mac.sh                   build the app into dist/
#         ./build_mac.sh --test            run the tests first
#         ./build_mac.sh --install         also replace /Applications/CrateTag.app
#         ./build_mac.sh --test --install  both
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
# Files from a download carry macOS's quarantine flag, which blocks double-clicking the updater
xattr -dr com.apple.quarantine "$HERE" 2>/dev/null || true

# Build in a clean environment. Settings like these, left over from another Qt or Python program,
# can make the build load a second copy of Qt (the "implemented in both" warnings).
unset QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH QML2_IMPORT_PATH QML_IMPORT_PATH \
      DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH DYLD_FALLBACK_LIBRARY_PATH DYLD_FALLBACK_FRAMEWORK_PATH \
      DYLD_INSERT_LIBRARIES PYTHONPATH PYTHONHOME 2>/dev/null || true
LOG_DIR="$HOME/Library/Logs/CrateTag"
mkdir -p "$LOG_DIR" 2>/dev/null || LOG_DIR="$HERE"
LOG="$LOG_DIR/build.log"
APPS="${CRATETAG_APPS_DIR:-/Applications}"   # where CrateTag.app is installed

RUN_TESTS=0
INSTALL=0
for arg in "$@"; do
  case "$arg" in
    --test) RUN_TESTS=1 ;;
    --install) INSTALL=1 ;;
    *) echo "Unknown option: $arg"; exit 2 ;;
  esac
done

# Python: use the one the installer found, else look for one, else install it.
PY="${CRATETAG_PYTHON:-}"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  PY="$("$HERE/install_deps.sh" | tail -1)"
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "Python 3.10 or newer is required and couldn't be installed. See the messages above."
  exit 1
fi
echo "Using $($PY --version)"

if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt pyinstaller pytest

if [ "$RUN_TESTS" = 1 ]; then
  QT_QPA_PLATFORM=offscreen python -m pytest -q tests
fi

# Let the built app know where its source folder is, for CrateTag > Update from Downloads…
python - "$HERE" <<'EOF'
import json, sys
open("cratetag/_build_info.py", "w").write("SOURCE_DIR = %s\n" % json.dumps(sys.argv[1]))
EOF

# Quit CrateTag and move the installed copy out of the way while building, so nothing can pick up
# its copy of Qt. It's put back if the build fails.
PARKED=""
if [ "$INSTALL" = 1 ] && [ -d "$APPS/CrateTag.app" ]; then
  osascript -e 'tell application "CrateTag" to quit' >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -f "$APPS/CrateTag.app/" >/dev/null 2>&1 || break
    sleep 1
  done
  PARKED="$(mktemp -d)/CrateTag.app"
  mv "$APPS/CrateTag.app" "$PARKED" 2>/dev/null || PARKED=""
fi
restore_parked() {
  if [ -n "$PARKED" ] && [ -d "$PARKED" ] && [ ! -d "$APPS/CrateTag.app" ]; then
    mv "$PARKED" "$APPS/CrateTag.app" && echo "Your previous CrateTag.app was put back."
  fi
}

rm -rf build dist
echo "Building… (details are saved to $LOG)"
# Qt's macOS window system isn't needed to build; offscreen keeps the build away from it.
if ! QT_QPA_PLATFORM=offscreen pyinstaller --noconfirm --clean --windowed --log-level WARN \
  --name CrateTag \
  --osx-bundle-identifier com.cratetag.app \
  --icon cratetag/assets/CrateTag.icns \
  --add-data "cratetag/assets:cratetag/assets" \
  --hidden-import cratetag._build_info \
  --hidden-import cratetag.deck --hidden-import cratetag.theme --hidden-import PySide6.QtSvg \
  --exclude-module tkinter \
  --exclude-module PySide6.QtWebEngineCore \
  --exclude-module PySide6.QtWebEngineWidgets \
  --exclude-module PySide6.Qt3DCore \
  --exclude-module PySide6.QtQuick \
  --exclude-module PySide6.QtQml \
  --exclude-module PySide6.QtMultimedia \
  main.py >"$LOG" 2>&1; then
  echo
  echo "The build failed. The last lines of the log:"
  tail -n 40 "$LOG"
  echo
  echo "Full log: $LOG"
  restore_parked
  exit 1
fi
if [ "$(uname)" = Darwin ] && [ ! -d dist/CrateTag.app ]; then
  echo "The build didn't produce CrateTag.app. Full log: $LOG"
  restore_parked
  exit 1
fi

echo
echo "Built: $HERE/dist/CrateTag.app"

if [ "$INSTALL" = 1 ]; then
  osascript -e 'tell application "CrateTag" to quit' >/dev/null 2>&1 || true
  rm -rf "$APPS/CrateTag.app"
  if command -v ditto >/dev/null 2>&1; then
    ditto dist/CrateTag.app "$APPS/CrateTag.app"
  else
    cp -R dist/CrateTag.app "$APPS/"
  fi
  # Nudge Finder and the Dock to pick up a changed app icon
  touch "$APPS/CrateTag.app" 2>/dev/null || true
  if [ -x /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister ]; then
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APPS/CrateTag.app" >/dev/null 2>&1 || true
  fi
  echo "Installed: $APPS/CrateTag.app"
  if [ -n "$PARKED" ]; then rm -rf "$(dirname "$PARKED")" 2>/dev/null || true; fi
else
  echo "Copy it into Applications with:  cp -R dist/CrateTag.app /Applications/"
fi
