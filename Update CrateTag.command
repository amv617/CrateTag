#!/bin/bash
# Update CrateTag in one step.
#
# Double-click this file (or choose CrateTag > Update from Downloads… in the app).
# It finds the newest CrateTag download in your Downloads folder (a .zip, or a folder
# if your browser unzipped it), quits CrateTag, copies the new files into this folder,
# rebuilds CrateTag.app, installs it in Applications and opens it.
#
# You can also pass a zip or folder:  "./Update CrateTag.command" ~/Desktop/CrateTag.zip
# Or get the latest version from GitHub (CrateTag > Check for Updates… uses this):
#   "./Update CrateTag.command" --github
set -euo pipefail

pause_and_exit() {
  echo
  read -n 1 -s -r -p "Press any key to close this window." || true
  echo
  exit "${1:-0}"
}

# On success, close this Terminal window by itself after a short pause.
# (On any error the window stays open so the message can be read.)
close_this_window() {
  local my_tty
  my_tty="$(tty 2>/dev/null || true)"
  if [ -n "${CRATETAG_KEEP_OPEN:-}" ] || ! command -v osascript >/dev/null 2>&1 || \
     [ -z "$my_tty" ] || [ "$my_tty" = "not a tty" ]; then
    pause_and_exit 0
  fi
  echo
  echo "Done. This window closes in 3 seconds."
  sleep 3
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
  # Close the window from a helper detached from this terminal session, so Terminal
  # doesn't see a running process and ask "Do you want to terminate…?".
  local py="$INSTALL_DIR/.venv/bin/python"
  [ -x "$py" ] || py="$(command -v python3 || true)"
  if [ -n "$py" ]; then
    nohup "$py" -c 'import os, subprocess, sys, time
os.setsid(); time.sleep(0.7)
subprocess.run(["osascript", "-e", sys.argv[1]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)' \
      "$script" </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
    sleep 0.4  # give the helper time to detach before this shell (and its terminal session) ends
    exit 0
  fi
  pause_and_exit 0
}

# Run from a temporary copy so this file can be replaced by the new version safely.
if [ -z "${CRATETAG_UPDATER_COPY:-}" ]; then
  INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
  TMP_COPY="$(mktemp -d)/update.command"
  cp "$0" "$TMP_COPY"
  chmod +x "$TMP_COPY"
  CRATETAG_UPDATER_COPY=1 CRATETAG_INSTALL_DIR="$INSTALL_DIR" exec "$TMP_COPY" "$@"
fi

INSTALL_DIR="$CRATETAG_INSTALL_DIR"
cd "$INSTALL_DIR"
version_of() { sed -n 's/^__version__ = "\(.*\)"/\1/p' "$1/cratetag/__init__.py" 2>/dev/null | head -1; }
CURRENT="$(version_of "$INSTALL_DIR")"

echo "CrateTag updater"
echo "Installed copy: $INSTALL_DIR (version ${CURRENT:-unknown})"
echo

# 1. Find the update
GITHUB_REPO="amv617/CrateTag"
SOURCE="${1:-}"
GITMODE=""
WORK="$(mktemp -d)"
if [ "$SOURCE" = "--github" ]; then
  echo "Checking GitHub ($GITHUB_REPO)…"
  if [ -d "$INSTALL_DIR/.git" ] && command -v git >/dev/null 2>&1; then
    # This folder is a Git copy of the repository: fetch the latest and update it in place below
    GITMODE=1
    git -C "$INSTALL_DIR" remote get-url origin >/dev/null 2>&1 || \
      git -C "$INSTALL_DIR" remote add origin "https://github.com/$GITHUB_REPO.git"
    if ! git -C "$INSTALL_DIR" fetch --quiet origin main; then
      echo "Couldn't reach GitHub. Check your internet connection and try again."
      pause_and_exit 1
    fi
    NEWVER="$(git -C "$INSTALL_DIR" show origin/main:cratetag/__init__.py | sed -n 's/^__version__ = "\(.*\)"/\1/p' | head -1)"
    SOURCE="GitHub ($GITHUB_REPO)"
  else
    # Not a Git copy: download the latest version as a zip
    if ! curl -fsSL "https://github.com/$GITHUB_REPO/archive/refs/heads/main.zip" -o "$WORK/main.zip"; then
      echo "Couldn't download the update from GitHub. Check your internet connection and try again."
      pause_and_exit 1
    fi
    SOURCE="$WORK/main.zip"
  fi
fi
if [ -z "$GITMODE" ]; then
  if [ -z "$SOURCE" ]; then
    # Newest CrateTag*.zip or unzipped CrateTag* folder in Downloads (but not this folder itself)
    while IFS= read -r f; do
      [ -e "$f" ] || continue
      if [ -d "$f" ]; then
        [ "$(cd "$f" && pwd)" = "$INSTALL_DIR" ] && continue
        [ -f "$f/cratetag/__init__.py" ] || [ -f "$f/CrateTag/cratetag/__init__.py" ] || continue
      else
        case "$f" in *.zip) ;; *) continue ;; esac
      fi
      SOURCE="$f"
      break
    done < <(ls -td "$HOME/Downloads"/CrateTag* 2>/dev/null)
  fi
  if [ -z "$SOURCE" ] || [ ! -e "$SOURCE" ]; then
    echo "No CrateTag download found in your Downloads folder."
    echo "Download the new CrateTag.zip first, then run this again."
    pause_and_exit 1
  fi
  echo "Update found: $SOURCE"

  if [ -f "$SOURCE" ]; then
    if command -v ditto >/dev/null 2>&1; then ditto -x -k "$SOURCE" "$WORK"; else unzip -q "$SOURCE" -d "$WORK"; fi
    NEW="$WORK/CrateTag"
    [ -d "$NEW" ] || NEW="$(dirname "$(find "$WORK" -maxdepth 3 -path '*/cratetag/__init__.py' | head -1)")/.."
  else
    NEW="$SOURCE"
    [ -f "$NEW/cratetag/__init__.py" ] || NEW="$NEW/CrateTag"
  fi
  NEW="$(cd "$NEW" && pwd)"
  if [ ! -f "$NEW/cratetag/__init__.py" ]; then
    echo "That download doesn't look like CrateTag."
    pause_and_exit 1
  fi
  NEWVER="$(version_of "$NEW")"
fi
echo "New version: ${NEWVER:-unknown}"

if [ -n "$CURRENT" ] && [ "$CURRENT" = "$NEWVER" ] && [ -z "${CRATETAG_FORCE:-}" ]; then
  read -r -p "You already have $CURRENT. Reinstall anyway? [y/N] " ans || ans=""
  case "$ans" in [yY]*) ;; *) echo "Nothing changed."; pause_and_exit 0 ;; esac
fi

# 2. Quit CrateTag if it's running
if command -v osascript >/dev/null 2>&1; then
  osascript -e 'tell application "CrateTag" to quit' >/dev/null 2>&1 || true
  sleep 1
fi

# 3. Copy the new files in (keeps the private Python environment so this stays fast)
echo
if [ -n "$GITMODE" ]; then
  echo "Updating files from GitHub…"
  # Keep anything local, just in case: uncommitted changes go in a stash, the old version in a branch
  git -C "$INSTALL_DIR" rev-parse --verify HEAD >/dev/null 2>&1 && \
    git -C "$INSTALL_DIR" branch -f before-update HEAD >/dev/null 2>&1 || true
  if [ -n "$(git -C "$INSTALL_DIR" status --porcelain 2>/dev/null)" ]; then
    git -C "$INSTALL_DIR" stash push --include-untracked --quiet -m "Local changes before CrateTag update" || true
  fi
  if ! git -C "$INSTALL_DIR" checkout --quiet -B main origin/main; then
    echo "Couldn't update the files from GitHub. Nothing was installed."
    pause_and_exit 1
  fi
  git -C "$INSTALL_DIR" branch --quiet --set-upstream-to=origin/main main 2>/dev/null || true
elif command -v rsync >/dev/null 2>&1; then
  echo "Copying files…"
  rsync -a --exclude '.git' --exclude '.venv' --exclude 'build' --exclude 'dist' --exclude '__pycache__' "$NEW/" "$INSTALL_DIR/"
else
  echo "Copying files…"
  (cd "$NEW" && tar cf - --exclude .git --exclude .venv --exclude build --exclude dist .) | (cd "$INSTALL_DIR" && tar xf -)
fi
chmod +x "$INSTALL_DIR"/*.sh "$INSTALL_DIR"/*.command 2>/dev/null || true
# Clear macOS's "downloaded from the internet" flag so these files can be double-clicked
xattr -dr com.apple.quarantine "$INSTALL_DIR" 2>/dev/null || true

# 4. Rebuild and install the app
echo
echo "Building CrateTag.app (about a minute)…"
if ! "$INSTALL_DIR/build_mac.sh" --install; then
  echo
  echo "The build failed. Your files were updated; you can still start CrateTag with ./run_from_source.sh"
  pause_and_exit 1
fi

rm -rf "$WORK"
echo
echo "CrateTag ${NEWVER} is installed in Applications."
if [ -z "${CRATETAG_NO_LAUNCH:-}" ] && command -v open >/dev/null 2>&1; then
  open "/Applications/CrateTag.app" || true
fi
if [ -z "$GITMODE" ] && [ -f "$SOURCE" ] && [ "$(dirname "$SOURCE")" = "$HOME/Downloads" ]; then
  echo "You can delete $(basename "$SOURCE") from Downloads."
fi
close_this_window

