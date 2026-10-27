"""User settings, stored as JSON in ~/Library/Application Support/CrateTag/."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import APP_NAME


def config_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Settings:
    # Sources
    contact_email: str = ""  # MusicBrainz asks apps to identify a contact; optional
    use_musicbrainz: bool = True
    use_discogs: bool = True
    use_deezer: bool = True  # free, no account or key
    use_itunes: bool = True  # Apple iTunes Search API: free, no key; used as a backup (about 20 searches a minute)
    discogs_token: str = ""
    use_acoustid: bool = False
    acoustid_key: str = ""
    fpcalc_path: str = ""  # blank = search PATH and common Homebrew locations

    # What to propose
    year_source: str = "auto"  # auto | musicbrainz | discogs | deezer | itunes
    year_mode: str = "earliest"  # "earliest" (original release year) or "best_match" (year of the closest match)
    genre_source: str = "auto"  # auto | discogs_style | discogs_genre | musicbrainz | deezer | itunes
    ignore_words: str = ""  # comma-separated words/names to drop from titles before searching
    max_genres: int = 1
    genre_separator: str = "; "
    genre_aliases: dict = field(default_factory=lambda: {
        "Folk, World, & Country": "World",
        "Hip Hop": "Hip-Hop",
    })

    # Writing behavior
    write_mode: str = "both"  # both | year | genre
    only_fill_blanks: bool = False
    min_confidence: int = 70  # rows at or above this are pre-checked
    backup_mode: str = "undo"  # undo (tiny log of old values) | copies (full file copies) | none
    make_backups: bool = True  # kept for older settings files; backup_mode decides
    backup_dir: str = str(Path.home() / "Music" / "CrateTag Backups")
    keep_modified_date: bool = False

    # Remembered UI state
    settings_version: int = 5
    last_folder: str = str(Path.home() / "Music")
    header_state: str = ""  # column order, widths and hidden columns
    header_cols: str = ""   # which column set header_state belongs to
    show_crates: bool = True
    crate_drive: str = "all"  # "all" or a drive's _Serato_ folder
    splitter_sizes: list = field(default_factory=list)
    shortcuts: dict = field(default_factory=dict)
    check_updates: bool = True  # look for a newer version on GitHub at launch
    theme: str = "deck"        # "deck" (dark, 0.10+) or "classic" (the original look); applies at launch
    side_page: str = "crates"  # what the left column shows: "crates" or "folders"  # command id -> "Ctrl+R" ("" = none); only changes from defaults

    @classmethod
    def path(cls) -> Path:
        return config_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        p = cls.path()
        s = cls()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                names = {f.name for f in fields(cls)}
                # 0.2 briefly dropped the "Hip Hop" -> "Hip-Hop" spelling; restore it if untouched.
                if data.get("genre_aliases") == {"Folk, World, & Country": "World"}:
                    data["genre_aliases"] = {"Folk, World, & Country": "World", "Hip Hop": "Hip-Hop"}
                # Before 0.4 "Discogs style" silently fell back to other sources; that is now "Auto".
                if int(data.get("settings_version", 0)) < 4:
                    if data.get("genre_source", "discogs_style") == "discogs_style":
                        data["genre_source"] = "auto"
                    data["settings_version"] = 4
                # 0.6: full-file backups replaced by a tiny undo log as the default
                if int(data.get("settings_version", 0)) < 5:
                    data["backup_mode"] = "undo"
                    data["settings_version"] = 5
                for k, v in data.items():
                    if k in names:
                        setattr(s, k, v)
            except (OSError, ValueError):
                pass
        return s

    def save(self) -> None:
        p = self.path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
