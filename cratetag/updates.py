"""Check GitHub for a newer CrateTag.

The repository is public, so no login is needed: CrateTag reads the version number from
cratetag/__init__.py on the main branch and compares it with its own.
"""

from __future__ import annotations

import re

from . import __version__

GITHUB_REPO = "amv617/CrateTag"
REPO_URL = f"https://github.com/{GITHUB_REPO}"
VERSION_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/cratetag/__init__.py"
# GitHub's API returns the file as it is right now; raw.githubusercontent.com can lag a few minutes behind.
API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/contents/cratetag/__init__.py?ref=main"


def parse_version(text: str) -> str | None:
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text or "", re.M)
    return m.group(1) if m else None


def version_tuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.findall(r"\d+", v or ""))


def is_newer(latest: str | None, current: str = __version__) -> bool:
    return bool(latest) and version_tuple(latest) > version_tuple(current)


def latest_version(timeout: float = 8.0, get=None) -> str:
    """The version on GitHub's main branch. Raises OSError if GitHub can't be reached."""
    if get is None:
        import requests

        def get(url):
            headers = {"Cache-Control": "no-cache"}
            if url == API_URL:
                headers["Accept"] = "application/vnd.github.raw"
            r = requests.get(url, timeout=timeout, headers=headers)
            r.raise_for_status()
            return r.text
    text, last = "", None
    for url in (API_URL, VERSION_URL):  # the API first (no delay); the raw file if the API is busy
        try:
            text = get(url)
            if parse_version(text):
                break
        except Exception as e:  # noqa: BLE001
            last = e
    if not text and last is not None:
        raise OSError(f"Couldn't reach GitHub: {last}") from last
    v = parse_version(text)
    if not v:
        raise OSError("GitHub's copy didn't have a version number")
    return v


RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def latest_release(timeout: float = 8.0, get_json=None) -> dict:
    """The newest published .dmg: {"version", "dmg_url", "size", "notes"}. Raises OSError if unavailable."""
    if get_json is None:
        import requests

        def get_json(url):
            r = requests.get(url, timeout=timeout, headers={"Accept": "application/vnd.github+json"})
            r.raise_for_status()
            return r.json()
    try:
        data = get_json(RELEASES_API)
    except Exception as e:  # noqa: BLE001
        raise OSError(f"Couldn't reach GitHub: {e}") from e
    version = (data.get("tag_name") or "").lstrip("vV")
    dmg = next((a for a in data.get("assets", []) if a.get("name", "").lower().endswith(".dmg")), None)
    if not version or dmg is None:
        raise OSError("GitHub doesn't have a CrateTag .dmg to download yet")
    return {"version": version, "dmg_url": dmg.get("browser_download_url", ""), "size": dmg.get("size", 0),
            "notes": data.get("body") or ""}


def install_kind() -> str:
    """"dmg" for a copy installed from the .dmg (it updates by downloading the newer .dmg),
    otherwise "source" (built from a CrateTag folder; it updates by rebuilding from GitHub)."""
    try:
        from . import _build_info
        return getattr(_build_info, "DIST", "source") or "source"
    except Exception:  # noqa: BLE001
        return "source"


# Runs after CrateTag quits: swaps in the new app from the downloaded .dmg and reopens it.
SWAP_SCRIPT = r'''#!/bin/bash
APP="$1"; DMG="$2"; PID="$3"
LOG="$HOME/Library/Logs/CrateTag/update.log"
mkdir -p "$(dirname "$LOG")"; exec >>"$LOG" 2>&1
echo "$(date): updating $APP from $DMG"
for _ in $(seq 1 120); do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
MNT="$(mktemp -d)"
if ! hdiutil attach -nobrowse -quiet -mountpoint "$MNT" "$DMG"; then
  echo "Couldn't open the disk image"; open "$DMG"; exit 1
fi
OK=0
if [ -d "$MNT/CrateTag.app" ]; then
  OLD="$(mktemp -d)/CrateTag.app"
  if mv "$APP" "$OLD" 2>/dev/null; then
    if ditto "$MNT/CrateTag.app" "$APP"; then OK=1; rm -rf "$(dirname "$OLD")"
    else rm -rf "$APP"; mv "$OLD" "$APP"; fi
  fi
fi
hdiutil detach -quiet "$MNT" || true
if [ "$OK" = 1 ]; then
  xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
  touch "$APP"; rm -f "$DMG"
  echo "Updated."; open "$APP"
else
  # Couldn't replace the app (for example no permission for this Applications folder):
  # reopen the old one and show the new .dmg so it can be dragged in by hand.
  echo "Couldn't replace the app; opening the disk image instead."
  open "$APP"; open "$DMG"
fi
'''
