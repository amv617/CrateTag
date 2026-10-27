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
