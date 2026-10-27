"""Reading and writing the tags CrateTag cares about.

Only the year (TDRC/TYER) and genre (TCON) frames are ever changed.
Every other frame in the file, including Serato's GEOB frames that hold
cue points, loops, colors and beatgrids, is left exactly as it was.
The file's existing ID3 version (2.3 or 2.4) is kept on save.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from mutagen.id3 import ID3, ID3NoHeaderError, TCON, TDRC, TDAT, TIT2, TPE1, TYER
from mutagen.mp3 import MP3, HeaderNotFoundError

AUDIO_EXTENSIONS = {".mp3"}


@dataclass
class TrackInfo:
    path: Path
    artist: str = ""
    title: str = ""
    album: str = ""
    year: str = ""
    genre: str = ""
    duration: float = 0.0
    id3_version: int = 4  # minor version: 3 or 4
    error: str = ""
    extra: dict = field(default_factory=dict)


def find_mp3s(paths: list[str | Path], recursive: bool = True) -> list[Path]:
    """Expand files and folders into a sorted, de-duplicated list of MP3s."""
    found: dict[str, Path] = {}
    for p in paths:
        p = Path(p).expanduser()
        if p.is_dir():
            walker = p.rglob("*") if recursive else p.glob("*")
            for f in walker:
                if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS and not f.name.startswith("._"):
                    found[str(f.resolve())] = f
        elif p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
            found[str(p.resolve())] = p
    return sorted(found.values(), key=lambda x: str(x).lower())


def _text(tags: ID3 | None, key: str) -> str:
    if tags is None:
        return ""
    frame = tags.get(key)
    if frame is None or not getattr(frame, "text", None):
        return ""
    return "; ".join(str(t) for t in frame.text if str(t).strip())


def _year_from_tags(tags: ID3 | None) -> str:
    raw = _text(tags, "TDRC") or _text(tags, "TYER") or _text(tags, "TORY") or _text(tags, "TDOR")
    m = re.search(r"\d{4}", raw)
    return m.group(0) if m else ""


def read_track(path: str | Path) -> TrackInfo:
    path = Path(path)
    info = TrackInfo(path=path)
    try:
        audio = MP3(path)
        info.duration = float(audio.info.length or 0)
    except HeaderNotFoundError:
        info.error = "Not a readable MP3"
        return info
    except Exception as e:  # noqa: BLE001
        info.error = f"Could not open: {e}"
        return info

    tags = audio.tags if isinstance(audio.tags, ID3) else None
    if tags is not None:
        info.id3_version = tags.version[1] if tags.version[1] in (3, 4) else 4
    info.artist = _text(tags, "TPE1") or _text(tags, "TPE2")
    info.title = _text(tags, "TIT2")
    info.album = _text(tags, "TALB")
    info.year = _year_from_tags(tags)
    info.genre = _text(tags, "TCON")
    return info


def backup_file(path: Path, backup_root: Path, source_root: Path | None = None) -> Path:
    """Copy a file into backup_root, mirroring its folder structure if possible."""
    backup_root.mkdir(parents=True, exist_ok=True)
    rel: Path
    if source_root is not None:
        try:
            rel = path.resolve().relative_to(source_root.resolve())
        except ValueError:
            rel = Path(path.name)
    else:
        rel = Path(path.name)
    dest = backup_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        stamp = datetime.now().strftime("%H%M%S")
        dest = dest.with_name(f"{dest.stem}.{stamp}{dest.suffix}")
    shutil.copy2(path, dest)
    return dest


def read_raw_year_genre(path: str | Path) -> tuple[list[str], list[str]]:
    """Exact year and genre text as stored (e.g. ['2011-05-03'], ['Hip Hop', 'Rap']), for undo."""
    raw = read_raw_fields(path)
    return raw["year"], raw["genre"]


def read_raw_fields(path: str | Path) -> dict[str, list[str]]:
    """Exact stored text of the four fields CrateTag can change, for the undo log."""
    out = {"year": [], "genre": [], "artist": [], "title": []}
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return out
    for key in ("TDRC", "TYER"):
        if key in tags and tags[key].text:
            out["year"] = [str(t) for t in tags[key].text]
            break
    for field, key in (("genre", "TCON"), ("artist", "TPE1"), ("title", "TIT2")):
        if key in tags:
            out[field] = [str(t) for t in tags[key].text]
    return out


@dataclass
class WriteJob:
    """What to change in one file. None leaves a field alone; '' or [] removes it."""
    path: Path
    year: str | list[str] | None = None
    genre: str | list[str] | None = None
    artist: str | list[str] | None = None
    title: str | list[str] | None = None

    def is_empty(self) -> bool:
        return self.year is None and self.genre is None and self.artist is None and self.title is None


def _as_list(v: str | list[str]) -> list[str]:
    if isinstance(v, list):
        return [x for x in v if str(x).strip()]
    v = v.strip()
    return [v] if v else []


def write_tags(path: str | Path, year: str | list[str] | None = None, genre: str | list[str] | None = None,
               artist: str | list[str] | None = None, title: str | list[str] | None = None,
               keep_mtime: bool = False) -> None:
    """Write any of year, genre, artist and title. None leaves a field untouched.

    An empty string (or empty list) removes that field. Lists are written exactly as given
    (used by undo to restore the original text, such as a full date or several genres).
    Every other frame, including Serato's GEOB data, is left as it was.
    """
    path = Path(path)
    if year is None and genre is None and artist is None and title is None:
        return

    stat = path.stat()
    try:
        tags = ID3(path)
        version = tags.version[1] if tags.version[1] in (3, 4) else 4
    except ID3NoHeaderError:
        tags = ID3()
        version = 3  # widest compatibility (Finder, Windows, older players)

    if year is not None:
        if isinstance(year, str):
            y = year.strip()
            if y and not re.fullmatch(r"\d{4}", y):
                raise ValueError(f"Year must be four digits, got {year!r}")
        years = _as_list(year)
        for key in ("TDRC", "TYER", "TDAT"):
            tags.delall(key)
        if years:
            # mutagen converts TDRC to TYER automatically when saving as v2.3
            tags.add(TDRC(encoding=3, text=years))

    for value, key, frame in ((genre, "TCON", TCON), (artist, "TPE1", TPE1), (title, "TIT2", TIT2)):
        if value is None:
            continue
        items = _as_list(value)
        tags.delall(key)
        if items:
            tags.add(frame(encoding=3, text=items))

    if version == 3:
        tags.update_to_v23()
        tags.save(path, v2_version=3)
    else:
        tags.update_to_v24()
        tags.save(path, v2_version=4)

    if keep_mtime:
        os.utime(path, (stat.st_atime, stat.st_mtime))


def write_year_genre(path: str | Path, year: str | list[str] | None = None,
                     genre: str | list[str] | None = None, keep_mtime: bool = False) -> None:
    """Write year and/or genre (kept for compatibility; see write_tags)."""
    write_tags(path, year=year, genre=genre, keep_mtime=keep_mtime)


# --------------------------------------------------------------------------- undo log

FIELDS = ("year", "genre", "artist", "title")
UNDO_FIELDS = ["path"] + [f"{k}_{f}" for f in FIELDS for k in ("wrote", "old", "new")] + ["time"]


class UndoLog:
    """A small CSV recording each file's original values before they are changed.

    A few hundred bytes per track instead of a full copy of the MP3. Rows are flushed as they
    are written, so the log is complete even if the app is closed mid-write.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        self._fh = open(path, "a", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._fh, fieldnames=UNDO_FIELDS)
        if new:
            self._w.writeheader()
            self._fh.flush()

    def record(self, job: WriteJob) -> None:
        old = read_raw_fields(job.path)
        row = {"path": str(Path(job.path).resolve()), "time": datetime.now().isoformat(timespec="seconds")}
        for f in FIELDS:
            v = getattr(job, f)
            row[f"wrote_{f}"] = "1" if v is not None else "0"
            row[f"old_{f}"] = json.dumps(old[f], ensure_ascii=False)
            row[f"new_{f}"] = "" if v is None else (v if isinstance(v, str) else "; ".join(v))
        self._w.writerow(row)
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


def read_undo_log(path: str | Path) -> list[WriteJob]:
    """Turn an undo log into restore jobs that put back each file's original values.

    If a file appears more than once, its earliest entry wins, which restores it to how it was
    before the first change recorded in this log. Logs from older versions (year/genre only) work too.
    """
    jobs: dict[str, WriteJob] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            p = row.get("path", "")
            if not p or p in jobs:
                continue
            job = WriteJob(Path(p))
            for f in FIELDS:
                if row.get(f"wrote_{f}") == "1":
                    setattr(job, f, json.loads(row.get(f"old_{f}") or "[]"))
            jobs[p] = job
    return list(jobs.values())
