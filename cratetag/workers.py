"""Background jobs so the window stays responsive during scans, lookups and writes."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from . import activity
from .config import Settings
from .lookup import Engine, LookupError_, Proposal
from .tags import TrackInfo, UndoLog, WriteJob, backup_file, find_mp3s, read_track, write_tags


class ScanWorker(QObject):
    batch = Signal(list)          # list[TrackInfo]
    progress = Signal(int, int)
    finished = Signal(int)

    def __init__(self, paths: list[str], explicit: bool = False):
        """explicit=True: `paths` is an exact track list (from a crate), kept in order,
        including files that are missing or aren't MP3s so they can be flagged."""
        super().__init__()
        self.paths = paths
        self.explicit = explicit
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    @Slot()
    def run(self) -> None:
        files = [Path(p) for p in self.paths] if self.explicit else find_mp3s(self.paths)
        total = len(files)
        buf: list[TrackInfo] = []
        for i, f in enumerate(files, 1):
            if self._cancel:
                break
            if self.explicit and not f.exists():
                buf.append(TrackInfo(path=f, error="File not found (is the drive connected?)"))
            elif self.explicit and f.suffix.lower() != ".mp3":
                buf.append(TrackInfo(path=f, error=f"{f.suffix.lstrip('.').upper() or 'This'} file: CrateTag edits MP3s only"))
            else:
                buf.append(read_track(f))
            if len(buf) >= 50:
                self.batch.emit(buf)
                buf = []
            self.progress.emit(i, total)
        if buf:
            self.batch.emit(buf)
        self.finished.emit(total)


class LibraryWorker(QObject):
    """Load every track in Serato's library file(s) without opening the MP3s (fast for big libraries).

    Values come from Serato's own copy of each track's tags. CrateTag re-reads the real file
    before analyzing it, so a stale copy can't hide a needed change."""

    batch = Signal(list)
    progress = Signal(int, int)
    finished = Signal(int)

    def __init__(self, libraries: list):
        super().__init__()
        self.libraries = libraries
        self.skipped = 0       # non-MP3 files (videos, WAV, AIFF...)
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    @Slot()
    def run(self) -> None:
        from .parsing import normalize
        from .serato import read_database
        seen: set[str] = set()
        buf: list[TrackInfo] = []
        total = 0
        for lib in self.libraries:
            for t in read_database(lib):
                if self._cancel:
                    break
                if t.ftype != "mp3" and t.path.suffix.lower() != ".mp3":
                    self.skipped += 1
                    continue
                k = str(t.path)
                if k in seen:
                    continue
                seen.add(k)
                a, ti = normalize(t.artist), normalize(t.title)
                info = TrackInfo(path=t.path, artist=t.artist, title=t.title, album=t.album, year=t.year,
                                 genre=t.genre, duration=t.seconds,
                                 extra={"from_db": True, "_hay": (a, ti, f"{a} {ti} {normalize(t.path.stem)}")})
                if t.missing:
                    info.error = "File not found (Serato marks it missing)"
                buf.append(info)
                total += 1
                if len(buf) >= 2000:
                    self.batch.emit(buf)
                    buf = []
                    self.progress.emit(total, 0)
        if buf:
            self.batch.emit(buf)
        self.finished.emit(total)


class LookupWorker(QObject):
    row_started = Signal(str)               # path
    row_done = Signal(str, object)          # path, Proposal
    progress = Signal(int, int)
    finished = Signal(bool)                 # cancelled?

    def __init__(self, settings: Settings, tracks: list[TrackInfo]):
        super().__init__()
        self.settings = settings
        self.tracks = tracks
        self.engine: Engine | None = None
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True
        if self.engine:
            self.engine.cancel()

    @Slot()
    def run(self) -> None:
        cancelled = False
        try:
            self.engine = Engine(self.settings)
        except Exception as e:  # noqa: BLE001
            for t in self.tracks:
                self.row_done.emit(str(t.path), Proposal(error=f"Setup failed: {e}"))
            self.finished.emit(False)
            return
        total = len(self.tracks)
        for i, info in enumerate(self.tracks, 1):
            if self._cancel:
                cancelled = True
                break
            self.row_started.emit(str(info.path))
            fresh = None
            if info.extra.get("from_db"):
                # Listed from Serato's library: read the real file first so the comparison is accurate
                fresh = read_track(info.path)
                if fresh.error:
                    activity.warn(f"Analyze  {info.path.name}: {fresh.error}  [{info.path}]")
                    self.row_done.emit(str(info.path), Proposal(error=fresh.error, fresh_info=fresh))
                    self.progress.emit(i, total)
                    continue
                pend_a, pend_t = info.extra.get("_pending_artist"), info.extra.get("_pending_title")
                info = replace(fresh, extra=info.extra,
                               artist=pend_a if pend_a is not None else fresh.artist,
                               title=pend_t if pend_t is not None else fresh.title)
            try:
                prop = self.engine.lookup(info)
            except LookupError_ as e:
                if str(e) == "Cancelled":
                    cancelled = True
                    break
                prop = Proposal(error=str(e))
            except Exception as e:  # noqa: BLE001
                prop = Proposal(error=f"Unexpected error: {e}")
            prop.fresh_info = fresh
            if prop.error and not prop.year and not prop.genre:
                activity.warn(f"Analyze  {info.path.name}: {prop.error or prop.note}  "
                              f"(searched {activity.q(prop.search_artist)} / {activity.q(prop.search_title)})")
            else:
                activity.info(f"Analyze  {info.path.name}: year {activity.q(prop.year)}, genre {activity.q(prop.genre)}, "
                              f"match {prop.confidence}%, {prop.source or 'no source'}"
                              f"{' — ' + prop.note if prop.note else ''}  "
                              f"(searched {activity.q(prop.search_artist)} / {activity.q(prop.search_title)})")
            self.row_done.emit(str(info.path), prop)
            self.progress.emit(i, total)
        self.finished.emit(cancelled)


class WriteWorker(QObject):
    row_done = Signal(str, bool, str, object)   # path, ok, message, refreshed TrackInfo | None
    progress = Signal(int, int)
    finished = Signal(int, int, str)            # ok count, fail count, backup folder or undo log

    def __init__(self, settings: Settings, jobs: list[WriteJob],
                 source_root: Path | None, backup_root: Path | None = None,
                 undo_log: Path | None = None, done_label: str = "Written"):
        super().__init__()
        self.settings = settings
        self.jobs = jobs
        self.source_root = source_root
        self.backup_root = backup_root
        self.undo_log = undo_log
        self.done_label = done_label

    @Slot()
    def run(self) -> None:
        ok = fail = 0
        total = len(self.jobs)
        log = None
        try:
            if self.undo_log is not None:
                log = UndoLog(self.undo_log)
            verb = "Restore" if self.done_label == "Restored" else "Write"
            for i, job in enumerate(self.jobs, 1):
                path = job.path
                try:
                    from .tags import read_raw_fields
                    old = read_raw_fields(path)
                    if self.backup_root is not None:
                        backup_file(path, self.backup_root, self.source_root)
                    if log is not None:
                        log.record(job)
                    write_tags(path, year=job.year, genre=job.genre, artist=job.artist, title=job.title,
                               keep_mtime=self.settings.keep_modified_date)
                    refreshed = read_track(path)
                    changes = [activity.change(f, old[f], getattr(job, f)) for f in ("artist", "title", "year", "genre")
                               if getattr(job, f) is not None]
                    activity.info(f"{verb} OK     {path.name}: " + ", ".join(changes) + f"  [{path}]")
                    self.row_done.emit(str(path), True, self.done_label, refreshed)
                    ok += 1
                except Exception as e:  # noqa: BLE001
                    activity.error(f"{verb} FAILED {path.name}: {e}  [{path}]")
                    self.row_done.emit(str(path), False, f"Write failed: {e}", None)
                    fail += 1
                self.progress.emit(i, total)
        finally:
            if log is not None:
                log.close()
        where = self.backup_root or self.undo_log or ""
        activity.info(f"{'Restore' if self.done_label == 'Restored' else 'Write'} finished: {ok} OK, {fail} failed"
                      + (f"; backup/undo log: {where}" if where else "; no backup"))
        self.finished.emit(ok, fail, str(where))
