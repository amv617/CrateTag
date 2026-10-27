"""The CrateTag window."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (QAbstractTableModel, QEvent, QFileSystemWatcher, QItemSelectionModel, QModelIndex,
                            QObject, QPoint, QPointF, QRect, QRectF, QSize, QSortFilterProxyModel, Qt, QThread, QTimer, QUrl, Signal)
from PySide6.QtGui import (QAction, QBrush, QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter,
                           QPixmap,
                           QPainterPath, QPen)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QSplitter, QStyle, QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QKeySequenceEdit, QFrame, QStyleOptionButton, QToolTip, QStackedWidget, QToolButton,
                               QInputDialog,
                               QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QTableView, QTableWidget,
                               QTableWidgetItem, QToolBar, QVBoxLayout, QWidget)

from . import APP_NAME, __version__, activity, shortcuts
from . import theme as T
from .config import Settings
from .lookup import Candidate, Proposal, _pick_genre, find_fpcalc
from .tags import TrackInfo, WriteJob, read_undo_log
from .workers import LibraryWorker, LookupWorker, ScanWorker, WriteWorker

YEAR_RE = re.compile(r"^\d{4}$")


def _size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

_R_DISPLAY, _R_EDIT, _R_CHECK = int(Qt.DisplayRole), int(Qt.EditRole), int(Qt.CheckStateRole)
_R_FONT, _R_FG, _R_TIP = int(Qt.FontRole), int(Qt.ForegroundRole), int(Qt.ToolTipRole)
_CHECKED, _UNCHECKED = Qt.Checked, Qt.Unchecked

# Built once, reused for every cell (QApplication must exist first, so filled in by _init_styles)
_BOLD = _STRIKE = _GRAY = _GREEN = _AMBER = _RED = _ACCENT = None


def _init_styles() -> None:
    global _BOLD, _STRIKE, _GRAY, _GREEN, _AMBER, _RED, _ACCENT
    if _BOLD is not None:
        return
    _BOLD = QFont()
    _BOLD.setBold(True)
    _STRIKE = QFont()
    _STRIKE.setStrikeOut(True)
    _GRAY = QBrush(QColor(140, 140, 140))
    _GREEN = QBrush(QColor(40, 150, 70))
    _AMBER = QBrush(QColor(190, 140, 20))
    _RED = QBrush(QColor(200, 70, 60))
    _ACCENT = QBrush(QColor(64, 140, 255))  # unsaved hand edits


# --------------------------------------------------------------------------- model

@dataclass
class Row:
    info: TrackInfo
    proposal: Proposal | None = None
    new_year: str = ""
    new_genre: str = ""
    checked: bool = False
    status: str = "Not analyzed"
    year_edited: bool = False   # typed or picked by hand: keep when sources change
    genre_edited: bool = False
    new_artist: str | None = None  # pending hand edit; None = unchanged
    new_title: str | None = None
    hay: tuple | None = None       # cached search text (artist, title, file), rebuilt when they change

    @property
    def key(self) -> str:
        return str(self.info.path)

    def year_changes(self) -> bool:
        return bool(self.new_year) and self.new_year != self.info.year

    def genre_changes(self) -> bool:
        return bool(self.new_genre) and self.new_genre.strip() != self.info.genre.strip()

    def artist_changes(self) -> bool:
        return self.new_artist is not None and self.new_artist.strip() != self.info.artist.strip()

    def title_changes(self) -> bool:
        return self.new_title is not None and self.new_title.strip() != self.info.title.strip()

    def has_changes(self, mode: str = "both") -> bool:
        # Artist/title edits are always written; the Write mode only chooses between year and genre.
        if self.artist_changes() or self.title_changes():
            return True
        if mode == "year":
            return self.year_changes()
        if mode == "genre":
            return self.genre_changes()
        return self.year_changes() or self.genre_changes()

    def pending_year(self) -> bool:
        return self.year_edited and self.year_changes()

    def pending_genre(self) -> bool:
        return self.genre_edited and self.genre_changes()

    _ATTENTION = ("Write failed", "Error", "Needs", "No match", "Review", "No year", "No genre", "File not found",
                  "Not an MP3", "Can't read", "Unreadable")

    def stage(self, mode: str = "both") -> str:
        """Where this track is in the workflow: new, analyzed, ready, written or attention."""
        s = self.status
        if s in ("Written", "Restored"):
            return "written"
        if self.info.error or s.startswith(self._ATTENTION):
            return "attention"
        if self.has_changes(mode):
            return "ready"
        if self.proposal is not None:
            return "analyzed"
        return "new"


COLS = ["", "File", "Artist", "Title", "Year", "New Year", "Genre", "New Genre", "Match", "Source",
        "Searched As", "Status"]
DECK_LABELS = ["", "File", "Artist", "Song", "Year in File", "Year Found", "Genre in File", "Genre", "Match", "Source",
               "Searched As", "Status"]
(C_CHECK, C_FILE, C_ARTIST, C_TITLE, C_YEAR, C_NYEAR, C_GENRE, C_NGENRE, C_CONF, C_SRC,
 C_SEARCH, C_STATUS) = range(len(COLS))
EDIT_FIELDS = {C_ARTIST: "artist", C_TITLE: "title", C_YEAR: "year", C_NYEAR: "year",
               C_GENRE: "genre", C_NGENRE: "genre"}


class TrackModel(QAbstractTableModel):
    checks_changed = Signal()
    cell_edited = Signal(int, str, str)  # row, "year"/"genre", value

    def __init__(self):
        super().__init__()
        self.rows: list[Row] = []
        self._index: dict[str, int] = {}
        self.write_mode = "both"  # both | year | genre
        self.labels = list(COLS)  # column titles (Deck renames a few)
        self.genre_shows_file = False  # Deck: the Genre column falls back to the file's genre

    # basics
    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return len(COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.labels[section]
        if orientation == Qt.Horizontal and role == Qt.ToolTipRole:
            return {C_CHECK: "Checked rows are written", C_NYEAR: "Double-click to edit",
                    C_NGENRE: "Double-click to edit", C_CONF: "How closely the best match fits",
                    C_SEARCH: "What was searched. Double-click to change it and search again"}.get(section)
        return None

    # Flags never change per column, so build them once instead of on every call.
    _BASE_FLAGS = Qt.ItemIsEnabled | Qt.ItemIsSelectable
    _COL_FLAGS = None

    def flags(self, index):
        cf = TrackModel._COL_FLAGS
        if cf is None:
            base = TrackModel._BASE_FLAGS
            cf = TrackModel._COL_FLAGS = tuple(
                base | Qt.ItemIsUserCheckable if c == C_CHECK else
                base | Qt.ItemIsEditable if c in EDIT_FIELDS else base
                for c in range(len(COLS)))
        return cf[index.column()]

    _ROLES = frozenset(int(r) for r in (Qt.DisplayRole, Qt.EditRole, Qt.CheckStateRole, Qt.ToolTipRole,
                                        Qt.FontRole, Qt.ForegroundRole))

    def data(self, index, role=Qt.DisplayRole):
        # Called many times per visible cell while scrolling: bail out fast on roles we don't use
        # and reuse prebuilt fonts and colors instead of creating new ones each time.
        if role not in self._ROLES or not index.isValid():
            return None
        r = self.rows[index.row()]
        c = index.column()

        if role == _R_DISPLAY or role == _R_EDIT:
            if c == C_FILE:
                return r.info.path.name
            if c == C_ARTIST:
                return r.new_artist if r.new_artist is not None else r.info.artist
            if c == C_TITLE:
                return r.new_title if r.new_title is not None else r.info.title
            if c == C_YEAR:
                return r.new_year if r.pending_year() else r.info.year
            if c == C_NYEAR:
                return r.new_year
            if c == C_GENRE:
                return r.new_genre if r.pending_genre() else r.info.genre
            if c == C_NGENRE:
                return r.new_genre
            if c == C_STATUS:
                return r.status
            p = r.proposal
            if c == C_CONF:
                if role == _R_EDIT:
                    return p.confidence if p else -1
                return f"{p.confidence}%" if p and p.confidence else ""
            if c == C_SRC:
                return p.source if p else ""
            if c == C_SEARCH:
                ex = r.info.extra
                if ex and ex.get("search_title"):
                    a, t = ex.get("search_artist", ""), ex["search_title"]
                elif p and p.search_title:
                    a, t = p.search_artist, p.search_title
                else:
                    return ""
                return f"{a} · {t}" if a else t
            return None

        if role == _R_CHECK:
            if c == C_CHECK:
                return _CHECKED if r.checked else _UNCHECKED
            return None

        if role == _R_FONT:
            if (c == C_ARTIST and r.artist_changes()) or (c == C_TITLE and r.title_changes()) or \
                    (c == C_YEAR and r.pending_year()) or (c == C_GENRE and r.pending_genre()):
                return _BOLD
            if c == C_NYEAR:
                changed = r.year_changes()
                if self.write_mode == "genre":
                    return _STRIKE if changed else None
                return _BOLD if changed else None
            if c == C_NGENRE:
                changed = r.genre_changes()
                if self.write_mode == "year":
                    return _STRIKE if changed else None
                return _BOLD if changed else None
            return None

        if role == _R_FG:
            if (c == C_ARTIST and r.artist_changes()) or (c == C_TITLE and r.title_changes()) or \
                    (c == C_YEAR and r.pending_year()) or (c == C_GENRE and r.pending_genre()):
                return _ACCENT
            if c == C_NYEAR:
                return _GRAY if (self.write_mode == "genre" or not r.year_changes()) else None
            if c == C_NGENRE:
                return _GRAY if (self.write_mode == "year" or not r.genre_changes()) else None
            p = r.proposal
            if c == C_CONF and p and p.confidence:
                return _GREEN if p.confidence >= 85 else (_AMBER if p.confidence >= 70 else _RED)
            if c == C_STATUS and (r.info.error or (p and p.error) or r.status.startswith("Write failed")):
                return _RED
            return None

        if role == _R_TIP:
            if c == C_FILE:
                return str(r.info.path)
            was = {C_ARTIST: (r.artist_changes(), r.info.artist), C_TITLE: (r.title_changes(), r.info.title),
                   C_YEAR: (r.pending_year(), r.info.year), C_GENRE: (r.pending_genre(), r.info.genre)}.get(c)
            if was and was[0]:
                return f"Not saved yet. In the file now: {was[1] or '(blank)'}"
            if c in (C_STATUS, C_CONF, C_SRC, C_SEARCH):
                p = r.proposal
                tips = []
                if p:
                    if c == C_SRC and p.source:
                        tips.append(p.source)
                    if p.search_title:
                        tips.append(f"Searched: {p.search_artist or '?'} / {p.search_title}")
                    if p.note:
                        tips.append(p.note)
                    if p.error:
                        tips.append(p.error)
                if r.info.error:
                    tips.append(r.info.error)
                return "\n".join(tips) or None
        return None

    def setData(self, index, value, role=Qt.EditRole):  # noqa: N802
        if not index.isValid():
            return False
        r = self.rows[index.row()]
        c = index.column()
        if role == Qt.CheckStateRole and c == C_CHECK:
            r.checked = (Qt.CheckState(value) == Qt.Checked)
            self.dataChanged.emit(index, index)
            self.checks_changed.emit()
            return True
        if role == Qt.EditRole and c in EDIT_FIELDS:
            field = EDIT_FIELDS[c]
            v = str(value).strip()
            if field == "year" and v and not YEAR_RE.match(v):
                return False
            self.manual_set([index.row()], field, v)
            self.cell_edited.emit(index.row(), field, v)
            return True
        return False

    def manual_set(self, rows: list[int], field: str, value: str) -> None:
        """Set a year or genre by hand on one or more rows (analyzed or not)."""
        if len(rows) > 1:
            activity.info(f"Edit     {field} → {activity.q(value)} on {len(rows)} tracks")
        for i in rows:
            r = self.rows[i]
            before = {"year": r.info.year, "genre": r.info.genre, "artist": r.info.artist, "title": r.info.title}[field]
            activity.info(f"Edit     {r.info.path.name}: {activity.change(field, before, value)} (not saved yet)")
            if field == "year":
                r.new_year = value
                r.year_edited = True
                changed = r.year_changes()
            elif field == "genre":
                r.new_genre = value
                r.genre_edited = True
                changed = r.genre_changes()
            elif field == "artist":
                r.hay = None
                r.new_artist = value if value != r.info.artist else None
                changed = r.artist_changes()
            else:
                r.hay = None
                r.new_title = value if value != r.info.title else None
                changed = r.title_changes()
            changed = changed or r.artist_changes() or r.title_changes()
            r.checked = r.has_changes(self.write_mode)
            if changed:
                r.status = "Ready to write (edited)"
            elif r.proposal is None:
                r.status = "Already correct" if value else "Not analyzed"
            self._emit_row(i)
        self.checks_changed.emit()

    def known_genres(self) -> list[str]:
        seen = {}
        for r in self.rows:
            for g in (r.info.genre, r.new_genre):
                g = (g or "").strip()
                if g and g.lower() not in seen:
                    seen[g.lower()] = g
        return sorted(seen.values(), key=str.lower)

    # helpers
    def _emit_row(self, i: int) -> None:
        self.dataChanged.emit(self.index(i, 0), self.index(i, len(COLS) - 1))

    def _reindex(self) -> None:
        self._index = {r.key: i for i, r in enumerate(self.rows)}

    def add_tracks(self, tracks: list[TrackInfo]) -> int:
        new, seen = [], set()
        for t in tracks:
            k = str(t.path)
            if k not in self._index and k not in seen:
                seen.add(k)
                new.append(t)
        if not new:
            return 0
        start = len(self.rows)
        self.beginInsertRows(QModelIndex(), start, start + len(new) - 1)
        for n, t in enumerate(new):
            row = Row(info=t, status=t.error or "Not analyzed")
            row.hay = t.extra.pop("_hay", None) if t.extra else None  # prepared by the loader
            self.rows.append(row)
            self._index[str(t.path)] = start + n
        self.endInsertRows()
        return len(new)

    # Sorting happens here in one pass (fast even for 80,000+ tracks) instead of in the proxy.
    def sort(self, column: int, order=Qt.AscendingOrder) -> None:  # noqa: D401
        if not self.rows:
            return
        if column == C_CHECK:
            key = lambda r: r.checked  # noqa: E731
        elif column == C_CONF:
            key = lambda r: r.proposal.confidence if r.proposal else -1  # noqa: E731
        elif column == C_NGENRE and self.genre_shows_file:
            # Deck's Genre column shows the file's genre until a new one is found: sort by what's shown
            key = lambda r: (r.new_genre or r.info.genre).strip().lower()  # noqa: E731
        elif column in (C_YEAR, C_NYEAR, C_GENRE, C_NGENRE, C_ARTIST, C_TITLE, C_FILE, C_SRC, C_SEARCH, C_STATUS):
            idx = self.index
            key = lambda r, c=column: str(self.data(idx(self._index[r.key], c)) or "").lower()  # noqa: E731
        else:
            return
        self.layoutAboutToBeChanged.emit()
        old = self.rows
        try:
            keys = [key(r) for r in old]
        except Exception:  # noqa: BLE001
            self.layoutChanged.emit()
            return
        rev = order == Qt.DescendingOrder
        if keys and isinstance(keys[0], str):  # empty cells go to the bottom either way
            filled = [i for i in range(len(old)) if keys[i]]
            blank = [i for i in range(len(old)) if not keys[i]]
            order_idx = sorted(filled, key=keys.__getitem__, reverse=rev) + blank
        else:
            order_idx = sorted(range(len(old)), key=keys.__getitem__, reverse=rev)
        new_pos = [0] * len(old)
        for new_i, old_i in enumerate(order_idx):
            new_pos[old_i] = new_i
        self.rows = [old[i] for i in order_idx]
        self._reindex()
        persistent = self.persistentIndexList()
        self.changePersistentIndexList(
            persistent, [self.index(new_pos[p.row()], p.column()) if p.isValid() else p for p in persistent])
        self.layoutChanged.emit()

    def clear(self) -> None:
        self.beginResetModel()
        self.rows.clear()
        self._index.clear()
        self.endResetModel()
        self.checks_changed.emit()

    def remove(self, keys: set[str]) -> None:
        self.beginResetModel()
        self.rows = [r for r in self.rows if r.key not in keys]
        self.endResetModel()
        self._reindex()
        self.checks_changed.emit()

    def row_for(self, key: str) -> int | None:
        return self._index.get(key)

    def set_status(self, key: str, status: str) -> None:
        i = self.row_for(key)
        if i is not None:
            self.rows[i].status = status
            self._emit_row(i)

    def _status(self, r: Row, settings: Settings) -> str:
        prop = r.proposal
        if prop is None:
            return r.status
        if prop.error and not prop.year and not prop.genre:
            return "Error" if "No title" not in prop.error else "Needs a title"
        if not prop.year and not prop.genre and not r.year_edited and not r.genre_edited:
            return prop.note or "No match"
        mode = self.write_mode
        missing = []
        if mode in ("both", "year") and not r.new_year:
            missing.append("year")
        if mode in ("both", "genre") and not r.new_genre:
            missing.append("genre")
        gap = f"no {' or '.join(missing)} found" if missing else ""
        if not r.has_changes(mode):
            return gap[:1].upper() + gap[1:] if gap else "Already correct"
        base = "Review" if prop.confidence < settings.min_confidence else "Ready to write"
        return f"{base} ({gap})" if gap else base

    def apply_proposal(self, key: str, prop: Proposal, settings: Settings, keep_manual: bool = False) -> None:
        i = self.row_for(key)
        if i is None:
            return
        r = self.rows[i]
        r.proposal = prop
        fresh = getattr(prop, "fresh_info", None)
        if fresh is not None:  # listed from Serato's library: now we know the file's real tags
            fresh.extra = {k: v for k, v in r.info.extra.items() if k != "from_db" and not k.startswith("_pending")}
            r.info = fresh
            r.hay = None
            prop.fresh_info = None
            if fresh.error and not prop.year and not prop.genre:
                r.status = fresh.error
                r.checked = False
                self._emit_row(i)
                self.checks_changed.emit()
                return
        if not (keep_manual and r.year_edited):
            r.new_year = prop.year or ""
            r.year_edited = False
        if not (keep_manual and r.genre_edited):
            r.new_genre = prop.genre or ""
            r.genre_edited = False
        if settings.only_fill_blanks:
            if r.info.year:
                r.new_year = r.info.year
            if r.info.genre:
                r.new_genre = r.info.genre
        r.status = self._status(r, settings)
        r.checked = r.has_changes(self.write_mode) and prop.confidence >= settings.min_confidence
        self._emit_row(i)
        self.checks_changed.emit()

    def recheck(self, settings: Settings) -> None:
        """Write mode changed: refresh which looked-up rows are ready and checked."""
        for r in self.rows:
            if r.status.startswith("Write") or r.status == "Written":
                continue
            if r.year_edited or r.genre_edited:
                r.checked = r.has_changes(self.write_mode)
                r.status = "Ready to write (edited)" if r.checked else (
                    self._status(r, settings) if r.proposal else "Already correct")
                continue
            if r.proposal is None:
                continue
            r.status = self._status(r, settings)
            r.checked = r.has_changes(self.write_mode) and r.proposal.confidence >= settings.min_confidence
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(COLS) - 1))
        self.checks_changed.emit()

    def apply_written(self, key: str, ok: bool, msg: str, info: TrackInfo | None) -> None:
        i = self.row_for(key)
        if i is None:  # undo logs store resolved paths; match those too
            i = next((n for n, r in enumerate(self.rows) if str(r.info.path.resolve()) == key), None)
        if i is None:
            return
        r = self.rows[i]
        if ok and info is not None:
            info.extra = {k: v for k, v in r.info.extra.items() if k != "from_db"}  # keep custom search terms
            r.info = info
            r.hay = None
            r.checked = False
            r.new_artist = r.new_title = None
            if msg == "Restored":
                r.year_edited = r.genre_edited = False
                r.new_year = r.proposal.year if r.proposal else ""
                r.new_genre = r.proposal.genre if r.proposal else ""
        r.status = msg
        self._emit_row(i)
        self.checks_changed.emit()

    def checked_rows(self) -> list[Row]:
        return [r for r in self.rows if r.checked]

    def set_checked(self, rows: list[int], value: bool) -> None:
        for i in rows:
            self.rows[i].checked = value
            self._emit_row(i)
        self.checks_changed.emit()

    def set_all_checked(self, value: bool) -> None:
        for r in self.rows:
            r.checked = value
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(COLS) - 1))
        self.checks_changed.emit()

    def checked_indexes(self) -> list[int]:
        return [i for i, r in enumerate(self.rows) if r.checked]


class ResizableHeader(QHeaderView):
    """Column header with a wider, steadier grab zone for resizing columns.

    Qt's built-in resize handle is only a few pixels wide, and on macOS a header tooltip popping
    up resets the cursor to the arrow. This header handles resizing itself: the resize cursor
    shows within GRIP pixels of a divider and stays put, tooltips are skipped over dividers, and
    double-clicking a divider fits the column to its contents.
    """

    GRIP = 6
    check_clicked = Signal()  # the check-all box in the checkbox column's header

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setMouseTracking(True)
        self.setSectionsClickable(True)
        self.setHighlightSections(False)
        self._drag = None  # (logical section, press x, starting width)
        self.check_column = -1
        self.check_state = Qt.Unchecked  # Checked, Unchecked or PartiallyChecked; set by the window
        self.deck = False
        self._box_press = False

    # ---- check-all box
    def check_box_rect(self) -> QRect:
        col = self.check_column
        if col < 0 or self.isSectionHidden(col):
            return QRect()
        x = self.sectionViewportPosition(col)
        s = 15
        if self.deck:  # line up with the row checkboxes, which are centered in the column
            x += self.sectionSize(col) // 2 - s // 2
        else:
            x += 9
        return QRect(x, (self.height() - s) // 2, s, s)

    def set_check_state(self, state) -> None:
        if state != self.check_state:
            self.check_state = state
            self.viewport().update()

    def paintSection(self, painter, rect, logical):  # noqa: N802
        super().paintSection(painter, rect, logical)
        if logical != self.check_column:
            return
        box = self.check_box_rect()
        if box.isNull():
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        st = self.check_state
        if self.deck:
            b = QRectF(box)
            if st == Qt.Unchecked:
                painter.setPen(QPen(QColor("#464b5f"), 1.5))
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(b.adjusted(0.75, 0.75, -0.75, -0.75), 4, 4)
            else:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(T.VIO))
                painter.drawRoundedRect(b, 4, 4)
                pen = QPen(QColor("white"), 2)
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                painter.setPen(pen)
                if st == Qt.Checked:
                    path = QPainterPath(QPointF(b.left() + 3.8, b.center().y() + 0.3))
                    path.lineTo(b.left() + 6.4, b.bottom() - 4)
                    path.lineTo(b.right() - 3.5, b.top() + 4)
                    painter.setBrush(Qt.NoBrush)
                    painter.drawPath(path)
                else:
                    painter.drawLine(QPointF(b.left() + 4, b.center().y()), QPointF(b.right() - 4, b.center().y()))
        else:
            opt = QStyleOptionButton()
            opt.rect = box
            opt.state = QStyle.State_Enabled | (QStyle.State_On if st == Qt.Checked else
                                                QStyle.State_NoChange if st == Qt.PartiallyChecked else QStyle.State_Off)
            self.style().drawPrimitive(QStyle.PE_IndicatorCheckBox, opt, painter, self)
        painter.restore()

    def _prev_visible(self, logical: int) -> int:
        vi = self.visualIndex(logical) - 1
        while vi >= 0:
            li = self.logicalIndex(vi)
            if not self.isSectionHidden(li):
                return li
            vi -= 1
        return -1

    def _last_visible(self) -> int:
        for vi in range(self.count() - 1, -1, -1):
            li = self.logicalIndex(vi)
            if not self.isSectionHidden(li):
                return li
        return -1

    def section_at_edge(self, x: int) -> int:
        """The column whose right-hand divider is under x (within GRIP pixels), or -1."""
        li = self.logicalIndexAt(x)
        if li < 0:
            return -1
        left = self.sectionViewportPosition(li)
        right = left + self.sectionSize(li)
        if right - x <= self.GRIP:
            target = li
        elif x - left <= self.GRIP:
            target = self._prev_visible(li)
        else:
            return -1
        if target < 0 or self.sectionResizeMode(target) != QHeaderView.Interactive:
            return -1
        if self.stretchLastSection() and target == self._last_visible():
            return -1
        return target

    @staticmethod
    def _x(e) -> int:
        return int(e.position().x()) if hasattr(e, "position") else e.pos().x()

    def event(self, e):
        if e.type() == QEvent.ToolTip and (self._drag or self.section_at_edge(e.pos().x()) >= 0):
            e.ignore()
            return True  # no tooltip over a divider: it would reset the resize cursor on macOS
        if e.type() == QEvent.ToolTip and self.check_box_rect().adjusted(-4, -6, 4, 6).contains(e.pos()):
            QToolTip.showText(e.globalPos(), "Uncheck all tracks" if self.check_state == Qt.Checked
                              else "Check all tracks", self)
            return True
        return super().event(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.check_box_rect().adjusted(-4, -6, 4, 6).contains(
                QPoint(self._x(e), int(e.position().y()))):
            self._box_press = True  # the check-all box: don't sort
            e.accept()
            return
        if e.button() == Qt.LeftButton:
            sec = self.section_at_edge(self._x(e))
            if sec >= 0:
                self._drag = (sec, self._x(e), self.sectionSize(sec))
                self.setCursor(Qt.SplitHCursor)
                e.accept()
                return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag:
            sec, x0, w0 = self._drag
            self.resizeSection(sec, max(self.minimumSectionSize(), w0 + self._x(e) - x0))
            e.accept()
            return
        super().mouseMoveEvent(e)
        if e.buttons() == Qt.NoButton:
            if self.section_at_edge(self._x(e)) >= 0:
                self.setCursor(Qt.SplitHCursor)
            else:
                self.unsetCursor()

    def mouseReleaseEvent(self, e):
        if self._box_press and e.button() == Qt.LeftButton:
            self._box_press = False
            if self.check_box_rect().adjusted(-4, -6, 4, 6).contains(QPoint(self._x(e), int(e.position().y()))):
                self.check_clicked.emit()
            e.accept()
            return
        if self._drag and e.button() == Qt.LeftButton:
            self._drag = None
            if self.section_at_edge(self._x(e)) < 0:
                self.unsetCursor()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        sec = self.section_at_edge(self._x(e))
        if e.button() == Qt.LeftButton and sec >= 0:
            view = self.parent()
            if isinstance(view, QTableView):
                view.resizeColumnToContents(sec)
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def leaveEvent(self, e):
        if not self._drag:
            self.unsetCursor()
        super().leaveEvent(e)


class TrackView(QTableView):
    """Clicking a New Year/New Genre cell inside a multi-row highlight edits it without
    dropping the highlight, so the value can be applied to every highlighted track (like Serato)."""

    EDITABLE = tuple(EDIT_FIELDS)

    _bottom_space = 0
    _check_press = False

    def mouseReleaseEvent(self, e):  # noqa: N802
        if self._check_press and e.button() == Qt.LeftButton:
            self._check_press = False  # the press already toggled the box
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent_check(self, e) -> bool:
        idx = self.indexAt(e.position().toPoint())
        return e.button() == Qt.LeftButton and idx.isValid() and idx.column() == C_CHECK

    def set_bottom_space(self, px: int) -> None:
        """Keep rows out of the bottom px pixels (Deck's floating bar sits there)."""
        if px != self._bottom_space:
            self._bottom_space = px
            self.updateGeometries()

    def updateGeometries(self):  # noqa: N802
        # QTableView sets its own margins (for the header) here; add the bottom space back on top of them.
        super().updateGeometries()
        m = self.viewportMargins()
        if m.bottom() != self._bottom_space:
            self.setViewportMargins(m.left(), m.top(), m.right(), self._bottom_space)

    def _maybe_artist_line(self, idx, pos) -> None:
        """Deck shows the artist under the title: editing that lower line edits the artist."""
        d = self.itemDelegate()
        if hasattr(d, "start_artist_edit"):
            d._edit_artist = None
        if idx.isValid() and idx.column() == C_TITLE and hasattr(d, "start_artist_edit"):
            rect = self.visualRect(idx)
            if pos.y() > rect.center().y():
                d.start_artist_edit(idx)

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        if self.mouseDoubleClickEvent_check(e):
            # The first click already toggled the box; don't open anything or highlight the row.
            self._check_press = True
            e.accept()
            return
        pos = e.position().toPoint()
        self._maybe_artist_line(self.indexAt(pos), pos)
        super().mouseDoubleClickEvent(e)

    def mousePressEvent(self, e):  # noqa: N802
        idx = self.indexAt(e.position().toPoint())
        sm = self.selectionModel()
        if not idx.isValid() and e.button() == Qt.LeftButton:
            self.clearSelection()  # click empty space to drop the highlight, like Finder
            e.accept()
            return
        if e.button() == Qt.LeftButton and idx.isValid() and idx.column() == C_CHECK:
            # A checkbox click only checks or unchecks; it doesn't change which rows are highlighted
            # (highlighted rows win over checked ones for Analyze and Write).
            cur = idx.data(Qt.CheckStateRole)
            self.model().setData(idx, Qt.Unchecked if cur == Qt.Checked else Qt.Checked, Qt.CheckStateRole)
            self._check_press = True
            e.accept()
            return
        if (e.button() == Qt.LeftButton and e.modifiers() == Qt.NoModifier and idx.isValid()
                and idx.column() in self.EDITABLE and sm.isSelected(idx) and len(sm.selectedRows()) > 1
                and self.editTriggers() != QAbstractItemView.NoEditTriggers):
            sm.setCurrentIndex(idx, QItemSelectionModel.NoUpdate)
            self._maybe_artist_line(idx, e.position().toPoint())
            self.edit(idx)
            e.accept()
            return
        super().mousePressEvent(e)


def _search_hay(r) -> tuple:
    """(artist, title, everything) in search form: lowercase, no accents or punctuation."""
    from .parsing import normalize
    artist = r.new_artist if r.new_artist is not None else r.info.artist
    title = r.new_title if r.new_title is not None else r.info.title
    a, t = normalize(artist), normalize(title)
    return (a, t, f"{a} {t} {normalize(r.info.path.stem)}")


class SortProxy(QSortFilterProxyModel):
    """Sorting plus the track search: matches artist, title and/or file name, ignoring case and accents."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._words: list[str] = []
        self._field = "all"
        self._match_ids: set[int] = set()
        self._src = None
        self._stage: str | None = None  # Deck's stage cards: show only one stage

    def set_stage(self, stage: str | None) -> None:
        self._src = self.sourceModel()
        self._stage = stage
        self._refilter()

    def _refilter(self) -> None:
        if hasattr(self, "beginFilterChange"):  # Qt 6.10+
            self.beginFilterChange()
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            self.invalidateFilter()

    def _stage_ok(self, r) -> bool:
        st = self._stage
        if st is None:
            return True
        if st == "analyzed":
            return r.proposal is not None
        return r.stage(self._src.write_mode) == st

    def set_search(self, text: str, field: str = "all") -> None:
        from .parsing import normalize
        words = normalize(text).split()
        # Work out every match in one fast pass; Qt then just looks each row up.
        self._src = self.sourceModel()
        rows = self._src.rows if self._src is not None else []
        self._words, self._field = words, field
        if words:
            for r in rows:
                if r.hay is None:
                    r.hay = _search_hay(r)
            i = 0 if field == "artist" else 1 if field == "title" else 2
            if len(words) == 1:
                w = words[0]
                self._match_ids = {id(r) for r in rows if w in r.hay[i]}
            else:
                self._match_ids = {id(r) for r in rows if all(x in r.hay[i] for x in words)}
        else:
            self._match_ids = set()
        self._refilter()

    def _row_matches(self, r) -> bool:
        hay = r.hay
        if hay is None:
            hay = r.hay = _search_hay(r)
        text = hay[0] if self._field == "artist" else hay[1] if self._field == "title" else hay[2]
        return all(w in text for w in self._words)

    def is_filtering(self) -> bool:
        return bool(self._words) or self._stage is not None

    def sort(self, column, order=Qt.AscendingOrder):  # noqa: D401
        """Sorting is done by the model in one fast pass; the proxy only filters."""
        src = self.sourceModel()
        if src is not None and column >= 0:
            src.sort(column, order)

    def filterAcceptsRow(self, source_row, source_parent):  # noqa: N802
        if self._stage is not None and not self._stage_ok(self._src.rows[source_row]):
            return False
        if not self._words:
            return True
        r = self._src.rows[source_row]
        if r.hay is None:  # new or edited since the search started: check it now
            hit = self._row_matches(r)
            (self._match_ids.add if hit else self._match_ids.discard)(id(r))
            return hit
        return id(r) in self._match_ids

    def lessThan(self, left, right):  # noqa: N802
        if left.column() == C_CONF:
            return (left.data(Qt.EditRole) or -1) < (right.data(Qt.EditRole) or -1)
        if left.column() == C_CHECK:
            return left.data(Qt.CheckStateRole) == Qt.Unchecked and right.data(Qt.CheckStateRole) == Qt.Checked
        return str(left.data() or "").lower() < str(right.data() or "").lower()


# --------------------------------------------------------------------------- dialogs

class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} Settings")
        self.s = settings
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        general = QWidget()
        lay = QVBoxLayout(general)
        self.tabs.addTab(general, "General")

        # Sources
        src = QGroupBox("Lookup sources")
        f = QFormLayout(src)
        self.use_mb = QCheckBox("Use MusicBrainz (free, no account)")
        self.use_mb.setChecked(settings.use_musicbrainz)
        f.addRow(self.use_mb)
        self.contact = QLineEdit(settings.contact_email)
        self.contact.setPlaceholderText("Optional: your email, sent to MusicBrainz as a courtesy contact")
        f.addRow("Contact:", self.contact)
        self.use_dc = QCheckBox("Use Discogs (best for genres and styles)")
        self.use_dc.setChecked(settings.use_discogs)
        f.addRow(self.use_dc)
        dc_row = QHBoxLayout()
        self.dc_token = QLineEdit(settings.discogs_token)
        self.dc_token.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.dc_token.setPlaceholderText("Personal access token")
        dc_btn = QPushButton("Get token…")
        dc_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://www.discogs.com/settings/developers")))
        dc_row.addWidget(self.dc_token)
        dc_row.addWidget(dc_btn)
        f.addRow("Discogs token:", dc_row)

        self.use_dz = QCheckBox("Use Deezer (free, no account; strong on release years for recent music)")
        self.use_dz.setChecked(getattr(settings, "use_deezer", True))
        f.addRow(self.use_dz)
        self.use_it = QCheckBox("Use Apple iTunes as a backup (free, no account; only asked when the others "
                                "miss a year or genre)")
        self.use_it.setToolTip("Apple allows about 20 searches a minute, so CrateTag only asks it when needed.")
        self.use_it.setChecked(getattr(settings, "use_itunes", True))
        f.addRow(self.use_it)

        self.use_ac = QCheckBox("Identify songs by sound with AcoustID (for badly named files)")
        self.use_ac.setChecked(settings.use_acoustid)
        f.addRow(self.use_ac)
        ac_row = QHBoxLayout()
        self.ac_key = QLineEdit(settings.acoustid_key)
        self.ac_key.setPlaceholderText("Application API key")
        ac_btn = QPushButton("Get key…")
        ac_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://acoustid.org/new-application")))
        ac_row.addWidget(self.ac_key)
        ac_row.addWidget(ac_btn)
        f.addRow("AcoustID key:", ac_row)
        fp_row = QHBoxLayout()
        self.fpcalc = QLineEdit(settings.fpcalc_path)
        self.fpcalc.setPlaceholderText("Leave blank to find it automatically")
        fp_btn = QPushButton("Browse…")
        fp_btn.clicked.connect(self._pick_fpcalc)
        fp_row.addWidget(self.fpcalc)
        fp_row.addWidget(fp_btn)
        f.addRow("fpcalc:", fp_row)
        self.fp_status = QLabel()
        f.addRow("", self.fp_status)
        self.fpcalc.textChanged.connect(self._update_fp_status)
        self._update_fp_status()
        lay.addWidget(src)

        # Proposals
        prop = QGroupBox("What to propose")
        g = QFormLayout(prop)
        self.year_mode = QComboBox()
        self.year_mode.addItem("Original release year (earliest)", "earliest")
        self.year_mode.addItem("Year of the closest matching release", "best_match")
        self.year_mode.setCurrentIndex(max(0, self.year_mode.findData(settings.year_mode)))
        g.addRow("Year:", self.year_mode)
        g.addRow(QLabel("<i>Year source, genre source and what to write are in the main window.</i>"))
        self.ignore_words = QLineEdit(settings.ignore_words)
        self.ignore_words.setPlaceholderText("e.g. KidCutUp, MarkCutz, DJ Nasa, Deville")
        self.ignore_words.setToolTip("Comma-separated words or DJ names to remove from titles before searching. "
                                     "Intro/Edit/Clean/Dirty, key and BPM are already removed automatically.")
        g.addRow("Also ignore in titles:", self.ignore_words)
        self.max_genres = QSpinBox()
        self.max_genres.setRange(1, 5)
        self.max_genres.setValue(int(settings.max_genres or 1))
        g.addRow("Genres per track:", self.max_genres)
        self.min_conf = QSpinBox()
        self.min_conf.setRange(0, 100)
        self.min_conf.setSuffix("%")
        self.min_conf.setValue(int(settings.min_confidence))
        g.addRow("Auto-check at or above:", self.min_conf)
        lay.addWidget(prop)

        # Writing
        wr = QGroupBox("Writing")
        h = QFormLayout(wr)
        self.fill_blanks = QCheckBox("Only fill in empty fields (never replace an existing year or genre)")
        self.fill_blanks.setChecked(settings.only_fill_blanks)
        h.addRow(self.fill_blanks)
        self.backup_mode = QComboBox()
        self.backup_mode.addItem("Keep an undo log (recommended, a few KB per run)", "undo")
        self.backup_mode.addItem("Copy each file first (uses as much space as the files)", "copies")
        self.backup_mode.addItem("No backup", "none")
        self.backup_mode.setCurrentIndex(max(0, self.backup_mode.findData(settings.backup_mode)))
        self.backup_mode.setToolTip("The undo log records each file's original year and genre so "
                                    "Tracks > Undo a Previous Write can put them back.")
        h.addRow("Before writing:", self.backup_mode)
        bk_row = QHBoxLayout()
        self.backup_dir = QLineEdit(settings.backup_dir)
        bk_btn = QPushButton("Choose…")
        bk_btn.clicked.connect(self._pick_backup)
        bk_row.addWidget(self.backup_dir)
        bk_row.addWidget(bk_btn)
        h.addRow("Backups and undo logs:", bk_row)
        self.keep_mtime = QCheckBox("Keep each file's original modified date")
        self.keep_mtime.setToolTip("Useful if you sort by date added/modified in Finder")
        self.keep_mtime.setChecked(settings.keep_modified_date)
        h.addRow(self.keep_mtime)
        lay.addWidget(wr)

        up = QGroupBox("Updates")
        ul = QVBoxLayout(up)
        self.check_updates = QCheckBox("Check GitHub for a new version when CrateTag opens")
        self.check_updates.setChecked(getattr(settings, "check_updates", True))
        ul.addWidget(self.check_updates)
        lay.addWidget(up)

        self.keys = ShortcutsEditor(settings.shortcuts)
        self.tabs.addTab(self.keys, "Keyboard Shortcuts")

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self._save_btn = bb.button(QDialogButtonBox.Save)
        self.keys.changed.connect(lambda: self._save_btn.setEnabled(not self.keys.conflicts()))
        outer.addWidget(bb)
        self.resize(640, self.sizeHint().height())

    def show_tab(self, name: str):
        if name == "shortcuts":
            self.tabs.setCurrentWidget(self.keys)

    def _pick_fpcalc(self):
        p, _ = QFileDialog.getOpenFileName(self, "Locate fpcalc")
        if p:
            self.fpcalc.setText(p)

    def _pick_backup(self):
        d = QFileDialog.getExistingDirectory(self, "Backup folder", self.backup_dir.text())
        if d:
            self.backup_dir.setText(d)

    def _update_fp_status(self):
        found = find_fpcalc(self.fpcalc.text().strip())
        self.fp_status.setText(f"Found: {found}" if found else "Not found. Install with: brew install chromaprint")

    def apply(self) -> None:
        s = self.s
        s.use_musicbrainz = self.use_mb.isChecked()
        s.contact_email = self.contact.text().strip()
        s.use_discogs = self.use_dc.isChecked()
        s.use_deezer = self.use_dz.isChecked()
        s.use_itunes = self.use_it.isChecked()
        s.discogs_token = self.dc_token.text().strip()
        s.use_acoustid = self.use_ac.isChecked()
        s.acoustid_key = self.ac_key.text().strip()
        s.fpcalc_path = self.fpcalc.text().strip()
        s.year_mode = self.year_mode.currentData()
        s.max_genres = self.max_genres.value()
        s.ignore_words = self.ignore_words.text().strip()
        s.min_confidence = self.min_conf.value()
        s.only_fill_blanks = self.fill_blanks.isChecked()
        s.backup_mode = self.backup_mode.currentData()
        s.make_backups = s.backup_mode == "copies"
        s.backup_dir = self.backup_dir.text().strip() or s.backup_dir
        s.keep_modified_date = self.keep_mtime.isChecked()
        s.shortcuts = self.keys.overrides()
        s.check_updates = self.check_updates.isChecked()


def _native(seq: str) -> str:
    """How a shortcut looks to the user (⌘R on a Mac)."""
    return QKeySequence.fromString(seq, QKeySequence.PortableText).toString(QKeySequence.NativeText) if seq else ""


class ShortcutsEditor(QWidget):
    """Settings > Keyboard Shortcuts: click a shortcut, press the new keys."""

    changed = Signal()

    def __init__(self, overrides: dict, parent=None):
        super().__init__(parent)
        self.chosen = shortcuts.effective(overrides)
        lay = QVBoxLayout(self)
        tip = QLabel("Click a shortcut, then press the keys you want (for example ⌘R). "
                     "Press Delete or click ✕ to remove one.")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Search commands")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._filter)
        lay.addWidget(self.filter)
        self.table = QTableWidget(len(shortcuts.COMMANDS), 3)
        self.table.setHorizontalHeaderLabels(["Menu", "Command", "Shortcut"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setFocusPolicy(Qt.NoFocus)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.setColumnWidth(2, 190)
        self.edits: dict[str, QKeySequenceEdit] = {}
        for r, c in enumerate(shortcuts.COMMANDS):
            self.table.setItem(r, 0, QTableWidgetItem(c.group))
            item = QTableWidgetItem(c.label)
            if c.default:
                item.setToolTip(f"Default: {_native(c.default)}")
            self.table.setItem(r, 1, item)
            ed = QKeySequenceEdit(QKeySequence.fromString(self.chosen[c.id], QKeySequence.PortableText))
            ed.setMaximumSequenceLength(1)
            ed.setClearButtonEnabled(True)
            ed.keySequenceChanged.connect(lambda seq, cid=c.id: self._set(cid, seq))
            self.edits[c.id] = ed
            self.table.setCellWidget(r, 2, ed)
        lay.addWidget(self.table, 1)
        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #d0453a;")
        lay.addWidget(self.warning)
        row = QHBoxLayout()
        row.addStretch(1)
        reset = QPushButton("Reset All to Defaults")
        reset.clicked.connect(self.reset_defaults)
        row.addWidget(reset)
        lay.addLayout(row)
        self._refresh()

    def _set(self, cid: str, seq: QKeySequence):
        text = seq.toString(QKeySequence.PortableText)
        if text in ("Backspace", "Del"):  # Delete on its own clears the shortcut
            text = ""
            self.edits[cid].blockSignals(True)
            self.edits[cid].clear()
            self.edits[cid].blockSignals(False)
        self.chosen[cid] = text
        self._refresh()

    def set_shortcut(self, cid: str, text: str):
        """Used by tests and Reset: set one command's shortcut as if the user typed it."""
        self.edits[cid].setKeySequence(QKeySequence.fromString(text, QKeySequence.PortableText))
        self.chosen[cid] = text
        self._refresh()

    def reset_defaults(self):
        for c in shortcuts.COMMANDS:
            self.edits[c.id].blockSignals(True)
            self.edits[c.id].setKeySequence(QKeySequence.fromString(c.default, QKeySequence.PortableText))
            self.edits[c.id].blockSignals(False)
            self.chosen[c.id] = c.default
        self._refresh()

    def conflicts(self) -> dict[str, list[str]]:
        return shortcuts.conflicts(self.chosen)

    def overrides(self) -> dict[str, str]:
        return shortcuts.overrides_from(self.chosen)

    def _refresh(self):
        clash = self.conflicts()
        bad = {cid for ids in clash.values() for cid in ids}
        for r, c in enumerate(shortcuts.COMMANDS):
            item = self.table.item(r, 1)
            item.setForeground(QBrush(QColor("#d0453a")) if c.id in bad else QBrush())
        if clash:
            lines = [f"{_native(k)} is used by: " + ", ".join(shortcuts.BY_ID[i].label for i in ids)
                     for k, ids in clash.items()]
            self.warning.setText("Each shortcut can only do one thing. Change one of these to save:\n"
                                 + "\n".join(lines))
        else:
            self.warning.setText("")
        self.changed.emit()

    def _filter(self, text: str):
        t = text.strip().lower()
        for r, c in enumerate(shortcuts.COMMANDS):
            hay = f"{c.group} {c.label} {_native(self.chosen[c.id])}".lower()
            self.table.setRowHidden(r, bool(t) and t not in hay)


class ActivityLogDialog(QDialog):
    """Help > Activity Log: what CrateTag did, newest at the bottom, with a filter box."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} Activity Log")
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter (e.g. a file name, Write, FAILED, nothing to write)")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._render)
        top.addWidget(self.filter, 1)
        lay.addLayout(top)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        f = QFont("Menlo")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(11)
        self.text.setFont(f)
        lay.addWidget(self.text, 1)
        self.where = QLabel(f"Saved at {activity.log_path()}")
        self.where.setStyleSheet("color: gray;")
        self.where.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.where)
        btns = QHBoxLayout()
        for label, slot in (("Refresh", self.reload), ("Copy", self._copy), ("Show in Finder", self._reveal)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            btns.addWidget(b)
        btns.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        btns.addWidget(close)
        lay.addLayout(btns)
        self.resize(1100, 600)
        self.reload()

    def reload(self):
        self._lines = activity.tail(5000).splitlines()
        self._render()

    def _render(self, *_):
        words = self.filter.text().lower().split()
        lines = [l for l in self._lines if all(w in l.lower() for w in words)] if words else self._lines
        self.text.setPlainText("\n".join(lines) if lines else "(nothing logged yet)" if not words else "(no matches)")
        sb = self.text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _copy(self):
        QApplication.clipboard().setText(self.text.toPlainText())

    def _reveal(self):
        _reveal_path(activity.log_path())


class MatchesDialog(QDialog):
    """Show every candidate found for a track and let the user pick one."""

    def __init__(self, row: Row, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Matches for {row.info.path.name}")
        self.row = row
        self.settings = settings
        self.chosen: tuple[str, str] | None = None
        p = row.proposal or Proposal()
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"Searched for: <b>{p.search_artist or '?'}</b> / <b>{p.search_title or '?'}</b>"))

        self.table = QTableWidget(len(p.candidates), 7)
        self.table.setHorizontalHeaderLabels(["Match", "Source", "Artist", "Title", "Year", "Genre / Style", "Link"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for i, c in enumerate(p.candidates):
            gs = ", ".join(c.styles or c.genres)
            if c.styles and c.genres:
                gs = f"{', '.join(c.styles)} ({', '.join(c.genres)})"
            vals = [f"{int(c.score * 100)}%", c.source, c.artist, c.title or "(not verified)", c.year, gs, c.url]
            for j, v in enumerate(vals):
                self.table.setItem(i, j, QTableWidgetItem(v))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.cellDoubleClicked.connect(self._double)
        lay.addWidget(self.table)
        if not p.candidates:
            lay.addWidget(QLabel("No candidates were found. You can type the year and genre directly in the table."))

        btns = QHBoxLayout()
        open_btn = QPushButton("Open Link")
        open_btn.clicked.connect(self._open)
        use_btn = QPushButton("Use This Match")
        use_btn.setDefault(True)
        use_btn.clicked.connect(self._use)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        btns.addWidget(open_btn)
        btns.addStretch(1)
        btns.addWidget(close_btn)
        btns.addWidget(use_btn)
        lay.addLayout(btns)
        self.resize(980, 420)

    def _selected(self) -> Candidate | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows or not self.row.proposal:
            return None
        return self.row.proposal.candidates[rows[0].row()]

    def _open(self):
        c = self._selected()
        if c and c.url:
            QDesktopServices.openUrl(QUrl(c.url))

    def _double(self, r, _c):
        self.table.selectRow(r)
        self._use()

    def _use(self):
        c = self._selected()
        if not c:
            return
        from dataclasses import replace as _replace
        genre, _ = _pick_genre([c], _replace(self.settings, genre_source="auto"))
        self.chosen = (c.year, genre)
        self.accept()


# --------------------------------------------------------------------------- main window

class CratePanel(QWidget):
    """Sidebar listing Serato crates as one combined tree, like Serato's crate list. Read-only."""

    crate_chosen = Signal(object, bool, bool)  # CrateNode, include sub-crates, add to current list
    drive_changed = Signal(str)

    ALL = "all"

    def __init__(self, parent=None, drive: str = "all", deck: bool = False):
        super().__init__(parent)
        self._drive = drive or self.ALL
        self.deck = deck
        lay = QVBoxLayout(self)
        lay.setContentsMargins(*((12, 16, 10, 10) if deck else (6, 6, 0, 6)))
        lay.setSpacing(8 if deck else 6)
        top = QHBoxLayout()
        title = QLabel("CRATES" if deck else "<b>Serato Crates</b>")
        if deck:
            title.setObjectName("SideTitle")
        self.refresh_btn = QPushButton("Refresh")
        if deck:
            from .deck import icon as deck_icon
            self.refresh_btn.setText("")
            self.refresh_btn.setIcon(deck_icon("refresh", T.MUT, 15))
            self.refresh_btn.setObjectName("SideBtn")
            self.refresh_btn.setCursor(Qt.PointingHandCursor)
        self.refresh_btn.setToolTip("Look for Serato crates again (for example after connecting a drive)")
        self.refresh_btn.clicked.connect(self.refresh)
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(self.refresh_btn)
        lay.addLayout(top)

        self.drive_combo = QComboBox()
        self.drive_combo.setToolTip("Which drive's crates to show. All drives combines them the way Serato does.")
        self.drive_combo.currentIndexChanged.connect(self._drive_picked)
        lay.addWidget(self.drive_combo)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search crates")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_soon)
        lay.addWidget(self.search)
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(150)
        self._filter_timer.timeout.connect(self._apply_filter)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        if deck:  # name, then the track count right-aligned
            self.tree.setColumnCount(2)
            self.tree.header().setStretchLastSection(False)
            self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
            self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.setUniformRowHeights(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemClicked.connect(self._clicked)
        self.tree.itemActivated.connect(self._clicked)
        lay.addWidget(self.tree, 1)
        if deck:
            self.tree.setIndentation(16)
            self.tree.setIconSize(QSize(10, 10))
        self.note = QLabel()
        self.note.setWordWrap(True)
        if deck:
            self.note.setObjectName("SideNote")
        else:
            self.note.setStyleSheet("color: gray;")
        lay.addWidget(self.note)
        self.libraries: list = []
        self._find = None  # tests can swap in a fake library finder
        self._expanded_before_search: set | None = None
        # Refresh automatically when a drive is connected or removed
        self._watcher = QFileSystemWatcher(self)
        if os.path.isdir("/Volumes"):
            self._watcher.addPath("/Volumes")
        self._watcher.directoryChanged.connect(lambda _p: QTimer.singleShot(1500, self.refresh))

    # ---- drives
    def _fill_drive_combo(self):
        self.drive_combo.blockSignals(True)
        self.drive_combo.clear()
        if len(self.libraries) != 1:
            self.drive_combo.addItem("All drives" if self.deck else "All drives (combined, like Serato)", self.ALL)
        for lib in self.libraries:
            kind = "external drive" if lib.external else "this Mac"
            self.drive_combo.addItem(lib.label if self.deck else f"{lib.label}  ({kind}, {len(lib.crates)} crates)",
                                     str(lib.serato_dir))
            self.drive_combo.setItemData(self.drive_combo.count() - 1,
                                         f"{lib.label}: {kind}, {len(lib.crates)} crates", Qt.ToolTipRole)
        idx = self.drive_combo.findData(self._drive)
        self.drive_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.drive_combo.setEnabled(self.drive_combo.count() > 1)
        self.drive_combo.blockSignals(False)

    def _drive_picked(self, _i):
        d = self.drive_combo.currentData()
        if d and d != self._drive:
            self._drive = d
            self.drive_changed.emit(d)
            self._rebuild()

    def selected_libraries(self):
        if self._drive == self.ALL:
            return self.libraries
        chosen = [l for l in self.libraries if str(l.serato_dir) == self._drive]
        return chosen or self.libraries

    # ---- building the tree
    def refresh(self):
        from .serato import find_libraries
        find = self._find or find_libraries
        try:
            self.libraries = find()
        except Exception as e:  # noqa: BLE001
            self.libraries = []
            self.note.setText(f"Couldn't read Serato crates: {e}")
            return
        self._fill_drive_combo()
        self._rebuild()

    def _rebuild(self):
        from .serato import build_tree, node_track_count
        expanded = {it.data(0, Qt.UserRole + 1) for it in self._all_items() if it.isExpanded()}
        cur = self.tree.currentItem()
        current = cur.data(0, Qt.UserRole + 1) if cur else None
        first_build = self.tree.topLevelItemCount() == 0 and not expanded
        self.tree.clear()
        libs = self.selected_libraries()
        roots = build_tree(libs, order_libs=self.libraries)
        icon = self.style().standardIcon(QStyle.SP_DirIcon)
        smart_icon = self.style().standardIcon(QStyle.SP_FileDialogDetailedView)
        multi = len(self.libraries) > 1
        self._count = 0

        deck = self.deck

        def add(node, parent):
            n = node_track_count(node)
            self._count += 1
            if deck:
                icon_now = T.dot_icon(T.crate_color(node.parts[0]) if node.parts else T.DIM)
            if node.smart:
                it = QTreeWidgetItem([f"{node.name}  (Smart Crate)"])
                it.setIcon(0, T.dot_icon("#3a3e52") if deck else smart_icon)
                it.setFlags(Qt.ItemIsEnabled)
                it.setForeground(0, QBrush(QColor(140, 140, 140)))
                it.setData(0, Qt.UserRole + 1, f"smart:{node.key}")
                it.setToolTip(0, f"{node.full_name}\nSmart Crates are built from rules inside Serato, so they "
                                 "don't store a song list CrateTag can read.")
                (self.tree.addTopLevelItem(it) if parent is None else parent.addChild(it))
                for ch in node.children:
                    add(ch, it)
                return
            text = f"{node.name}  ({n})" if node.members else node.name
            if deck:
                text = node.name
            it = QTreeWidgetItem([text, f"{n:,}" if (deck and node.members) else ""])
            it.setIcon(0, icon_now if deck else icon)
            if deck:
                it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                it.setForeground(1, QBrush(QColor(T.DIM)))
            it.setData(0, Qt.UserRole, node)
            it.setData(0, Qt.UserRole + 1, f"crate:{node.key}")
            where = ""
            if node.members and multi:
                where = "\nStored on: " + ", ".join(node.drives)
            elif not node.members:
                where = "\nNo crate file of its own on " + ("these drives" if multi else "this drive") + \
                        " (it only groups its sub-crates)."
            it.setToolTip(0, f"{node.full_name}\n{n} tracks{where}\nClick to load. Right-click for more.")
            if not node.members:
                it.setForeground(0, QBrush(QColor(150, 150, 150)))
            if parent is None:
                self.tree.addTopLevelItem(it)
            else:
                parent.addChild(it)
            for ch in node.children:
                add(ch, it)

        self.tree.setUpdatesEnabled(False)
        self._add_all_songs(libs)
        for r in roots:
            add(r, None)
        smart = []  # Smart Crates now appear in place in the tree
        if smart:
            sm = QTreeWidgetItem(["Smart Crates (not supported yet)"])
            sm.setFlags(Qt.ItemIsEnabled)
            sm.setForeground(0, QBrush(QColor(140, 140, 140)))
            sm.setData(0, Qt.UserRole + 1, "smart")
            sm.setToolTip(0, "Smart Crates are built from rules inside Serato, so they don't store a song list "
                             "CrateTag can read. Load the regular crate the songs are in instead.")
            for _lbl, name in smart:
                c = QTreeWidgetItem([name])
                c.setFlags(Qt.NoItemFlags)
                c.setData(0, Qt.UserRole + 1, f"smart:{name}")
                sm.addChild(c)
            self.tree.addTopLevelItem(sm)
        for it in self._all_items():
            key = it.data(0, Qt.UserRole + 1)
            if key in expanded:
                it.setExpanded(True)
            if key and key == current:
                self.tree.setCurrentItem(it)
        self.tree.setUpdatesEnabled(True)
        self._expanded_before_search = None
        if self.search.text().strip():
            self._apply_filter()

        if not self.libraries:
            self.note.setText("No Serato library found. Connect your DJ drive, then click Refresh.")
        else:
            crates = sum(len(l.crates) for l in libs)
            self.note.setText(f"{crates} crate{'s' if crates != 1 else ''}. Crates are only read, never changed.")

    def _add_all_songs(self, libs):
        """'All Songs' at the top, like Serato's All: every MP3 in the library file(s)."""
        from .serato import ALL_SONGS_KEY, CrateNode, DB_FILE, database_mp3_count
        with_db = [l for l in libs if (l.serato_dir / DB_FILE).exists()]
        if not with_db:
            return
        node = CrateNode(parts=["All Songs"])
        node.order_key = ALL_SONGS_KEY
        node.all_songs = True
        n = sum(database_mp3_count(l) for l in with_db)
        it = QTreeWidgetItem(["All Songs", f"{n:,}"] if self.deck else [f"All Songs  ({n:,})"])
        it.setIcon(0, T.dot_icon(T.MUT) if self.deck else self.style().standardIcon(QStyle.SP_DriveHDIcon))
        if self.deck:
            it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            it.setForeground(1, QBrush(QColor(T.DIM)))
        f = it.font(0)
        f.setBold(True)
        it.setFont(0, f)
        it.setData(0, Qt.UserRole, _AllSongs(node))
        it.setData(0, Qt.UserRole + 1, ALL_SONGS_KEY)
        it.setToolTip(0, f"Every MP3 in your Serato library ({', '.join(l.label for l in with_db)}), "
                         "like Serato's All.\nVideos and other non-MP3 files aren't shown.")
        self.tree.addTopLevelItem(it)

    # ---- search
    def _filter_soon(self, _t):
        self._filter_timer.start()

    def _apply_filter(self):
        from .serato import ALL_SONGS_KEY
        text = self.search.text().strip().lower()
        items = self._all_items()
        if not text:
            for it in items:
                it.setHidden(False)
            if self._expanded_before_search is not None:
                for it in items:
                    it.setExpanded(it.data(0, Qt.UserRole + 1) in self._expanded_before_search)
                self._expanded_before_search = None
            cur = self.tree.currentItem()
            if cur:
                self.tree.scrollToItem(cur)
            self.note.setText(self.note.text().split("  ·  ")[0])
            return
        if self._expanded_before_search is None:
            self._expanded_before_search = {it.data(0, Qt.UserRole + 1) for it in items if it.isExpanded()}
        self.tree.setUpdatesEnabled(False)
        matches = 0

        def visit(it) -> bool:
            nonlocal matches
            node = it.data(0, Qt.UserRole)
            name = node.name if node is not None else it.text(0)
            hit = text in name.lower() or it.data(0, Qt.UserRole + 1) == ALL_SONGS_KEY  # always shown
            child_hit = False
            for i in range(it.childCount()):
                child_hit = visit(it.child(i)) or child_hit
            if hit:
                matches += 1
            show = hit or child_hit
            it.setHidden(not show)
            it.setExpanded(child_hit)
            return show

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))
        self.tree.setUpdatesEnabled(True)
        base = self.note.text().split("  ·  ")[0]
        self.note.setText(f"{base}  ·  {matches} match{'es' if matches != 1 else ''} for \"{self.search.text().strip()}\"")

    # ---- helpers
    def _all_items(self):
        out = []
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            it = stack.pop()
            out.append(it)
            stack.extend(it.child(i) for i in range(it.childCount()))
        return out

    def item_for(self, crate_key: str):
        for it in self._all_items():
            c = it.data(0, Qt.UserRole)
            if c is not None and c.key == crate_key:
                return it
        return None

    def _clicked(self, item, _col=0):
        crate = item.data(0, Qt.UserRole)
        if crate is not None:
            # A group with no crate file of its own: show its sub-crates' songs instead of nothing
            self.crate_chosen.emit(crate, not crate.members and bool(crate.children), False)

    def _menu(self, pos):
        item = self.tree.itemAt(pos)
        crate = item.data(0, Qt.UserRole) if item else None
        m = QMenu(self)
        if crate is not None:
            has_sub = bool(crate.children)
            m.addAction("Load Crate", lambda: self.crate_chosen.emit(crate, False, False))
            if has_sub:
                m.addAction("Load Crate with Sub-crates", lambda: self.crate_chosen.emit(crate, True, False))
            m.addSeparator()
            m.addAction("Add Crate to List", lambda: self.crate_chosen.emit(crate, False, True))
            if has_sub:
                m.addAction("Add Crate with Sub-crates to List", lambda: self.crate_chosen.emit(crate, True, True))
            if crate.members:
                m.addSeparator()
                for mem in crate.members:
                    label = "Reveal Crate File in Finder"
                    if len(self.libraries) > 1:
                        label += f" ({mem.library.label})"
                    m.addAction(label, lambda f=mem.file: _reveal_path(f))
            m.addSeparator()
        m.addAction("Expand All", self.tree.expandAll)
        m.addAction("Collapse All", self.tree.collapseAll)
        m.addAction("Refresh", self.refresh)
        m.exec(self.tree.viewport().mapToGlobal(pos))


class _AllSongs:
    """Stands in for a crate so 'All Songs' can travel the same path as a crate click."""

    def __init__(self, node):
        from .serato import ALL_SONGS_KEY
        self.key = ALL_SONGS_KEY
        self.parts = ["All Songs"]
        self.name = "All Songs"
        self.full_name = "All Songs"
        self.members: list = []
        self.children: list = []
        self.library = None
        self.drives: list = []
        self.smart = False


def _clean_env() -> dict:
    """This process's environment minus Qt/Python/library-path settings the packaged app sets for itself,
    so programs CrateTag starts (like the updater's build) don't pick up its copy of Qt."""
    drop = ("QT_", "QML", "DYLD_", "PYTHON", "_MEI", "_PYI")
    return {k: v for k, v in os.environ.items() if not k.startswith(drop)}


ASSETS = Path(__file__).resolve().parent / "assets"


def app_icon() -> QIcon:
    """The CT Monogram icon (also baked into CrateTag.app by the build)."""
    p = ASSETS / "icon.png"
    return QIcon(str(p)) if p.exists() else QIcon()


class _UpdateSignal(QObject):
    """Carries the GitHub update check's answer from its background thread to the window."""
    done = Signal(object, object, bool)  # latest version, error text, quiet
    downloaded = Signal(str, str)        # path of the downloaded .dmg, error text


def _terminal_script(cmd: str) -> str:
    """AppleScript that runs cmd in Terminal. If Terminal wasn't open, it opens with an empty window of its own;
    run the command in that window instead of opening a second one, so no blank window is left behind."""
    quoted = '"' + cmd.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return (
        'set wasRunning to application "Terminal" is running\n'
        'tell application "Terminal"\n'
        '  if wasRunning then\n'
        f'    do script {quoted}\n'
        '  else\n'
        '    activate\n'
        '    repeat 30 times\n'
        '      if (count of windows) > 0 then exit repeat\n'
        '      delay 0.1\n'
        '    end repeat\n'
        '    if (count of windows) > 0 then\n'
        f'      do script {quoted} in window 1\n'
        '    else\n'
        f'      do script {quoted}\n'
        '    end if\n'
        '  end if\n'
        '  activate\n'
        'end tell')


def _reveal_path(path: Path):
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", str(path)], check=False)
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings | None = None):
        super().__init__()
        self.settings = settings or Settings.load()
        activity.info(f"──── {APP_NAME} {__version__} started ────")
        self.setWindowTitle(f"{APP_NAME} v{__version__}")
        self.resize(1280, 760)
        self.setAcceptDrops(True)
        self.source_root: Path | None = None
        self._thread: QThread | None = None
        self._worker = None

        _init_styles()
        self.model = TrackModel()
        self.model.cell_edited.connect(self._on_cell_edited)
        self.proxy = SortProxy(self)
        self.proxy.setSourceModel(self.model)
        self.view = TrackView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.view.setAlternatingRowColors(True)
        # Smooth trackpad scrolling (pixel by pixel instead of jumping a whole row) and cheaper layout
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.verticalScrollBar().setSingleStep(12)
        self.view.setWordWrap(False)
        self.view.setTextElideMode(Qt.ElideRight)
        vh = self.view.verticalHeader()
        vh.setSectionResizeMode(QHeaderView.Fixed)
        vh.setDefaultSectionSize(max(22, self.view.fontMetrics().height() + 8))
        self.view.verticalHeader().setVisible(False)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.doubleClicked.connect(self._double_clicked)
        self.view.selectionModel().selectionChanged.connect(self._update_lookup_label)
        self.view.setHorizontalHeader(ResizableHeader(self.view))
        hh = self.view.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        hh.setMinimumSectionSize(24)
        hh.check_column = C_CHECK
        hh.deck = self.deck
        hh.check_clicked.connect(self._header_check_clicked)
        if self.deck:
            from .deck import DeckDelegate
            self.model.labels = list(DECK_LABELS)
            self.model.genre_shows_file = True
            self.view.setShowGrid(False)
            self.view.setAlternatingRowColors(False)
            self.view.setFrameShape(QFrame.NoFrame)
            self.view.setObjectName("Tracks")  # the row painter draws its own soft highlight
            self.view.setStyleSheet("QTableView#Tracks { selection-background-color: transparent; "
                                    "background: transparent; }")
            self.delegate = DeckDelegate(self.view, self.model, self.proxy, self.settings)
            self.view.setItemDelegate(self.delegate)
            vh.setDefaultSectionSize(DeckDelegate.ROW_H)
            hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._apply_default_columns()
        # Like Serato: right-click a column header to show or hide columns; drag headers to reorder.
        hh.setSectionsMovable(True)
        hh.setContextMenuPolicy(Qt.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._header_menu)
        self._restore_header()

        self.empty_hint = QLabel("Pick a Serato crate on the left, drop a folder of MP3s here, or click Add Folder.")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setWordWrap(True)
        if self.deck:
            self.empty_hint.setObjectName("EmptyHint")
        else:
            self.empty_hint.setStyleSheet("color: gray; font-size: 15px; padding: 30px;")

        self.track_search = QLineEdit()
        self.track_search.setPlaceholderText("Search tracks by artist or title  (⌘F)")
        self.track_search.setClearButtonEnabled(True)
        self.track_search.textChanged.connect(lambda _t: self._search_timer.start())
        self.track_search_field = QComboBox()
        for label, data in (("All", "all"), ("Artist", "artist"), ("Title", "title")):
            self.track_search_field.addItem(label, data)
        self.track_search_field.setToolTip("Search artist, title and file name, or just one of them")
        self.track_search_field.currentIndexChanged.connect(lambda _i: self._apply_track_search())
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)  # wait for a pause in typing (big libraries)
        self._search_timer.timeout.connect(self._apply_track_search)

        self.crates = CratePanel(drive=self.settings.crate_drive, deck=self.deck)
        self.crates.crate_chosen.connect(self.load_crate)
        self.crates.drive_changed.connect(self._crate_drive_changed)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        if not self.deck:
            right = QWidget()
            cl = QVBoxLayout(right)
            cl.setContentsMargins(6, 6, 6, 6)
            bar = QHBoxLayout()
            bar.addWidget(self.track_search, 1)
            bar.addWidget(self.track_search_field)
            cl.addLayout(bar)
            cl.addWidget(self.empty_hint)
            cl.addWidget(self.view, 1)
            self.side = self.crates
            self.splitter.addWidget(self.crates)
            self.splitter.addWidget(right)
            self.splitter.setStretchFactor(0, 0)
            self.splitter.setStretchFactor(1, 1)
            self.splitter.setSizes([260, 1000])
            self.setCentralWidget(self.splitter)
        self.current_crate = None
        self.current_folder: str | None = None
        self._loading_crate = None

        self._build_toolbar()
        if self.deck:
            self._build_deck()
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.setVisible(False)
        self.update_btn = QPushButton("Update")
        self.update_btn.setObjectName("PrimaryBtn" if self.deck else "")
        self.update_btn.setCursor(Qt.PointingHandCursor)
        self.update_btn.setToolTip("A newer CrateTag is on GitHub. Click to install it.")
        self.update_btn.clicked.connect(self.install_update)
        self.update_btn.setVisible(False)
        self.statusBar().addPermanentWidget(self.update_btn)
        self._update_signal = _UpdateSignal(self)
        self._update_signal.done.connect(self._update_checked)
        self._update_signal.downloaded.connect(self._dmg_downloaded)
        self.count_label = QLabel()
        self.count_label.setContentsMargins(0, 0, 18, 0)  # keep the counts off the window's edge
        self.statusBar().addPermanentWidget(self.count_label)
        self.statusBar().addPermanentWidget(self.progress)
        self.model.checks_changed.connect(self._refresh_counts)
        self.model.rowsInserted.connect(self._refresh_counts)
        self.model.modelReset.connect(self._refresh_counts)
        self._refresh_counts()
        self._set_busy(None)
        self.side.setVisible(bool(self.settings.show_crates))
        if self.deck:
            self._show_side_page(self.settings.side_page if self.settings.show_crates else None, save=False)
        if self.settings.splitter_sizes:
            try:
                self.splitter.setSizes([int(x) for x in self.settings.splitter_sizes])
            except (TypeError, ValueError):
                pass
        if self.settings.show_crates:
            QTimer.singleShot(0, self.crates.refresh)

        if not self.settings.discogs_token and self.settings.use_discogs:
            self.statusBar().showMessage("Tip: add a free Discogs token in Settings for much better genres.", 15000)

    # ---- UI construction
    def _build_toolbar(self):
        tb = None
        if not self.deck:  # Deck puts these buttons in the window itself (see _build_deck)
            tb = QToolBar("Main")
            tb.setMovable(False)
            tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
            self.addToolBar(tb)

        def sep():
            if tb is not None:
                tb.addSeparator()

        self.cmd: dict[str, QAction] = {}

        def act(text, slot, cid=None, tip=None, toolbar=True):
            """One QAction per command. Its shortcut comes from Settings (see apply_shortcuts)."""
            a = QAction(text, self)
            a.triggered.connect(slot)
            if tip:
                a.setToolTip(tip)
            if cid:
                self.cmd[cid] = a
                self.addAction(a)  # shortcut works even when the action only sits in a context menu
            if toolbar and tb is not None:
                tb.addAction(a)
            return a

        self.a_crates = act("Crates", self._toggle_crates, "toggle_crates", "Show or hide the Serato crates list")
        self.a_crates.setCheckable(True)
        self.a_crates.setChecked(bool(self.settings.show_crates))
        sep()
        self.a_folder = act("Add Folder…", self.add_folder, "add_folder")
        self.a_files = act("Add Files…", self.add_files, "add_files")
        self.a_clear = act("Clear", self.clear_all, "clear")
        sep()
        self.a_check_all = act("Select All", self.select_all, "check_all", "Check every track")
        self.a_check_none = act("Select None", self.select_none, "check_none", "Uncheck every track")
        sep()
        self.a_lookup = act("Analyze", self.lookup, "analyze",
                            "Find the year and genre for the highlighted tracks "
                            "(or checked tracks, or everything if nothing is chosen)")
        self.a_stop = act("Stop", self.stop, "stop")
        sep()
        self.a_write = act("Write Checked", self.write_checked, "write")

        def combo(label, items, current, slot, tip):
            sep()
            if tb is not None:
                tb.addWidget(QLabel(f" {label} "))
            cb = QComboBox()
            for text, data in items:
                cb.addItem(text, data)
            cb.setCurrentIndex(max(0, cb.findData(current)))
            cb.setToolTip(tip)
            cb.currentIndexChanged.connect(slot)
            if tb is not None:
                tb.addWidget(cb)
            return cb

        self.write_combo = combo("Write:", [("Year & Genre", "both"), ("Year only", "year"), ("Genre only", "genre")],
                                 self.settings.write_mode, self._write_mode_changed,
                                 "Which fields Write Checked changes")
        self.year_src_combo = combo("Year from:", [("Auto", "auto"), ("MusicBrainz", "musicbrainz"), ("Discogs", "discogs"),
                                                   ("Deezer", "deezer"), ("Apple iTunes", "itunes")],
                                    self.settings.year_source, self._sources_changed,
                                    "Auto: earliest year among the best matches from any source. A specific source leaves the year "
                                    "blank if that source found nothing.")
        self.genre_src_combo = combo("Genre from:", [("Auto", "auto"), ("Discogs style", "discogs_style"),
                                                     ("Discogs genre", "discogs_genre"), ("MusicBrainz", "musicbrainz"),
                                                     ("Deezer", "deezer"), ("Apple iTunes", "itunes")],
                                     self.settings.genre_source, self._sources_changed,
                                     "Auto: Discogs style, then Discogs genre, then MusicBrainz, then Deezer, then Apple iTunes. A specific source leaves the genre blank "
                                     "if that source found nothing.")
        self.model.write_mode = self.settings.write_mode
        sep()
        self.a_settings = act("Settings…", self.open_settings, "settings")
        self.a_settings.setMenuRole(QAction.PreferencesRole)  # CrateTag > Settings… on macOS

        def mk(text, slot, cid):
            return act(text, slot, cid, toolbar=False)

        # Menu bar mirrors the toolbar (macOS puts it at the top of the screen)
        mb = self.menuBar()
        fm = mb.addMenu("File")
        fm.addAction(self.a_folder)
        fm.addAction(self.a_files)
        fm.addAction(self.a_clear)
        fm.addSeparator()
        fm.addAction(self.a_write)
        fm.addAction(mk("Write Checked Tracks (ignore highlight)", self.write_only_checked, "write_checked"))
        fm.addAction(self.a_settings)
        em = mb.addMenu("Edit")
        em.addAction(mk("Select All", self.highlight_all, "highlight_all"))
        em.addAction(mk("Search Tracks", self.focus_track_search, "search_tracks"))
        em.addAction(mk("Search Crates", self.focus_crate_search, "search_crates"))
        em.addSeparator()
        em.addAction(mk("Check Selected", lambda: self._check_highlighted(True), "check_selected"))
        em.addAction(mk("Uncheck Selected", lambda: self._check_highlighted(False), "uncheck_selected"))
        em.addAction(self.a_check_all)
        em.addAction(self.a_check_none)
        em.addSeparator()
        em.addAction(mk("Remove from List", self.remove_selected, "remove"))
        vm = mb.addMenu("View")
        vm.addAction(self.a_crates)
        self.a_folders = mk("Browse Folders", self._toggle_folders, "browse_folders")
        self.a_folders.setCheckable(True)
        if self.deck:
            vm.addAction(self.a_folders)
        vm.addSeparator()
        vm.addAction(mk("Command Palette…", self.open_palette, "palette"))
        vm.addSeparator()
        classic = QAction("Classic Look (takes effect next launch)", self)
        classic.setCheckable(True)
        classic.setChecked(not self.deck)
        classic.toggled.connect(self._set_classic)
        vm.addAction(classic)
        tm = mb.addMenu("Tracks")
        tm.addAction(self.a_lookup)
        tm.addAction(self.a_stop)
        tm.addSeparator()
        tm.addAction(mk("Reveal in Finder", self.reveal_selected, "reveal"))
        tm.addAction(mk("Show Matches…", self.show_matches_selected, "show_matches"))
        tm.addAction(mk("Edit Search…", self.edit_search_selected, "edit_search"))
        tm.addSeparator()
        tm.addAction(mk("Set Artist…", lambda: self.bulk_set_dialog("artist"), "set_artist"))
        tm.addAction(mk("Set Title…", lambda: self.bulk_set_dialog("title"), "set_title"))
        tm.addAction(mk("Set Year…", lambda: self.bulk_set_dialog("year"), "set_year"))
        tm.addAction(mk("Set Genre…", lambda: self.bulk_set_dialog("genre"), "set_genre"))
        tm.addSeparator()
        tm.addAction(mk("Undo a Previous Write…", self.undo_previous_write, "undo_write"))
        hm = mb.addMenu("Help")
        about = QAction(f"About {APP_NAME}", self)
        about.setMenuRole(QAction.AboutRole)
        about.triggered.connect(self._about)
        hm.addAction(about)
        hm.addAction(mk("Activity Log…", self.show_activity_log, "activity_log"))
        hm.addSeparator()
        hm.addAction(mk("Keyboard Shortcuts…", lambda: self.open_settings(tab="shortcuts"), None))
        chk = mk("Check for Updates…", self.check_for_updates, "check_updates")
        chk.setMenuRole(QAction.ApplicationSpecificRole)  # lands in the CrateTag menu on macOS
        hm.addAction(chk)
        upd = mk("Update from Downloads…", self.update_from_downloads, "update")
        upd.setMenuRole(QAction.ApplicationSpecificRole)
        hm.addAction(upd)
        from . import updates as _updates
        if _updates.install_kind() == "dmg":
            upd.setVisible(False)  # installed from the .dmg: there's no CrateTag folder to update from
        self.apply_shortcuts()

    # ---- Deck layout
    def _build_deck(self):
        from .deck import FloatingBar, StageBar, rail_icon

        root = QWidget()
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        # Icon rail on the far left
        rail = QWidget()
        rail.setObjectName("Rail")
        rail.setFixedWidth(62)
        rl = QVBoxLayout(rail)
        rl.setContentsMargins(11, 14, 11, 14)
        rl.setSpacing(6)

        def rbtn(action, icon_name, tip):
            b = QToolButton()
            action.setIcon(rail_icon(icon_name))  # on the action, so toggling it doesn't drop the icon
            action.setIconVisibleInMenu(False)
            b.setDefaultAction(action)
            b.setIconSize(QSize(20, 20))
            b.setToolButtonStyle(Qt.ToolButtonIconOnly)
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            rl.addWidget(b, 0, Qt.AlignHCenter)
            return b

        self.rail_crates = rbtn(self.a_crates, "crates", "Serato crates")
        self.rail_folders = rbtn(self.a_folders, "folder", "Browse folders")
        rbtn(self.a_files, "files", "Add MP3 files…")
        rbtn(self.cmd["undo_write"], "undo", "Undo a previous write…")
        rbtn(self.cmd["activity_log"], "log", "Activity log")
        rl.addStretch(1)
        rbtn(self.a_settings, "gear", "Settings")
        h.addWidget(rail)

        # Left column: crates or folders
        self.side = QStackedWidget()
        self.side.setObjectName("SidePanel")
        self.side.setMinimumWidth(210)
        self.side.addWidget(self.crates)
        self.folders = None  # built the first time it's shown

        # Main area
        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(24, 16, 24, 0)
        ml.setSpacing(14)
        top = QHBoxLayout()
        top.setSpacing(14)
        self.title_lbl = QLabel("No tracks loaded")
        self.title_lbl.setObjectName("Title")
        self.subtitle_lbl = QLabel("")
        self.subtitle_lbl.setObjectName("Subtitle")
        top.addWidget(self.title_lbl)
        top.addWidget(self.subtitle_lbl, 0, Qt.AlignBottom)
        top.addStretch(1)
        self.track_search.setObjectName("Search")
        self.track_search.setPlaceholderText("Search tracks  ⌘F    ·    Commands  ⌘K")
        from .deck import icon as deck_icon
        self.track_search.addAction(deck_icon("search", T.DIM, 16), QLineEdit.LeadingPosition)
        top.addWidget(self.track_search)
        self.track_search_field.setFixedWidth(96)
        top.addWidget(self.track_search_field)
        ml.addLayout(top)

        self.stages = StageBar()
        self.stages.stage_chosen.connect(self._stage_chosen)
        ml.addWidget(self.stages)

        opts = self.opts_layout = QHBoxLayout()
        opts.setSpacing(8)

        def opt(label, cb):
            lab = QLabel(label)
            lab.setObjectName("OptLbl")
            opts.addWidget(lab)
            opts.addWidget(cb)

        def tbtn(action, name, text=None):
            b = QToolButton()
            b.setDefaultAction(action)
            b.setObjectName(name)
            b.setToolButtonStyle(Qt.ToolButtonTextOnly)
            b.setCursor(Qt.PointingHandCursor)
            if text:
                b.setText(text)
            opts.addWidget(b)
            return b

        # Year from / Genre from on the left. Analyze and Stop line up under the Ready to write and Written
        # cards (see _align_buttons). What to write and Write on the right. Clear lives in the File menu.
        self.opts_left = QWidget()
        left = QHBoxLayout(self.opts_left)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(6)
        for i, (label, cb) in enumerate((("Year from", self.year_src_combo), ("Genre from", self.genre_src_combo))):
            lab = QLabel(label)
            lab.setObjectName("OptLbl")
            if i:
                left.addSpacing(8)
            left.addWidget(lab)
            left.addWidget(cb)
        left.addStretch(1)
        opts.addWidget(self.opts_left)
        self.analyze_box = QWidget()  # Analyze sits under Ready to write; this box ends where Written starts
        ab = QHBoxLayout(self.analyze_box)
        ab.setContentsMargins(0, 0, 0, 0)
        self.analyze_btn = QToolButton()
        self.analyze_btn.setDefaultAction(self.a_lookup)
        self.analyze_btn.setObjectName("GhostBtn")
        self.analyze_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.analyze_btn.setCursor(Qt.PointingHandCursor)
        ab.addStretch(1)
        ab.addWidget(self.analyze_btn)
        ab.addSpacing(24)  # a little room between Analyze and Stop
        opts.addWidget(self.analyze_box)
        self.stop_btn = tbtn(self.a_stop, "GhostBtn")
        opts.addStretch(1)
        opt("Write", self.write_combo)
        tbtn(self.a_write, "PrimaryBtn")
        self.stages.cards["ready"].installEventFilter(self)
        self.stages.cards["written"].installEventFilter(self)
        ml.addLayout(opts)

        self.table_frame = QFrame()
        self.table_frame.setObjectName("TableFrame")
        tl = QVBoxLayout(self.table_frame)
        tl.setContentsMargins(1, 1, 1, 1)
        tl.addWidget(self.empty_hint)
        tl.addWidget(self.view, 1)
        ml.addWidget(self.table_frame, 1)
        ml.addSpacing(16)

        self.floating = FloatingBar(self.table_frame)
        self.floating.add(self.a_lookup, "Analyze")
        self.floating.add(self.cmd["set_year"], "Set Year")
        self.floating.add(self.cmd["set_genre"], "Set Genre")
        self.floating.add(self.cmd["reveal"], "Reveal")
        self.float_write = self.floating.add(self.a_write, "*", primary=True)
        self.table_frame.installEventFilter(self)

        self.splitter.addWidget(self.side)
        self.splitter.addWidget(main)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([250, 1100])
        h.addWidget(self.splitter, 1)
        self.setCentralWidget(root)
        self.resize(1440, 900)

        self._stage_timer = QTimer(self)
        self._stage_timer.setSingleShot(True)
        self._stage_timer.setInterval(120)
        self._stage_timer.timeout.connect(self._update_stages)
        self.model.dataChanged.connect(lambda *_: self._stage_timer.start())
        self.model.rowsRemoved.connect(lambda *_: self._stage_timer.start())

    def eventFilter(self, obj, e):  # noqa: N802
        if obj is getattr(self, "table_frame", None) and e.type() == QEvent.Resize:
            self.floating.place()
        elif getattr(self, "stages", None) is not None and obj in (self.stages.cards.get("ready"), self.stages.cards.get("written")) and \
                e.type() in (QEvent.Resize, QEvent.Move):
            self._align_buttons()
        return super().eventFilter(obj, e)

    STOP_NUDGE = 18  # Stop sits this far left of the Written card's edge

    def _align_buttons(self):
        """Start Analyze at the left edge of the Ready to write card above it."""
        cards = self.stages.cards
        x = cards["ready"].x()
        need = self.opts_left.layout().minimumSize().width()
        left_w = max(x, need)
        self.opts_left.setFixedWidth(left_w)
        # Stop under Written: Analyze's box reaches from Ready's left edge to Written's left edge
        gap = self.opts_layout.spacing()
        box_w = cards["written"].x() - left_w - gap - self.STOP_NUDGE
        self.analyze_box.setFixedWidth(max(box_w, self.analyze_btn.sizeHint().width() + 32))

    def _show_side_page(self, page: str | None, save: bool = True):
        """Deck: show the crates or the folder browser in the left column, or hide it (None)."""
        if page == "folders" and self.folders is None:
            from .deck import FolderPanel
            self.folders = FolderPanel(self.settings.last_folder)
            self.folders.folder_chosen.connect(self._load_folder)
            self.side.addWidget(self.folders)
        if page is not None:
            self.side.setCurrentWidget(self.folders if page == "folders" else self.crates)
            self.settings.side_page = page
        self.side.setVisible(page is not None)
        self.settings.show_crates = page is not None
        self.a_crates.setChecked(page == "crates")
        self.a_folders.setChecked(page == "folders")
        if page == "crates" and not self.crates.libraries:
            self.crates.refresh()
        if save:
            self.settings.save()

    def _toggle_folders(self, *_):
        if not self.deck:
            self.add_folder()
            return
        self._show_side_page("folders" if self.a_folders.isChecked() else None)

    def _load_folder(self, path: str, append: bool):
        if self._busy():
            return
        if not append:
            n = self._unsaved_count()
            if n and QMessageBox.question(
                    self, APP_NAME,
                    f"You have {n} track{'s' if n != 1 else ''} with changes that haven't been written yet.\n\n"
                    f"Load \"{Path(path).name}\" anyway? Those changes will be dropped.") != QMessageBox.Yes:
                return
            self.model.clear()
            self.current_crate = None
            self.source_root = None
            self.current_folder = path
            self._update_title()
        self.settings.last_folder = path
        self.settings.save()
        self.scan([path])

    def _stage_chosen(self, stage):
        self.proxy.set_stage(stage)
        self._refresh_counts()

    def _update_stages(self):
        if not getattr(self, "stages", None):
            return
        mode = self.model.write_mode
        counts = {"all": len(self.model.rows), "analyzed": 0, "ready": 0, "written": 0, "attention": 0}
        for r in self.model.rows:
            if r.proposal is not None:
                counts["analyzed"] += 1
            st = r.stage(mode)
            if st in counts and st != "analyzed":
                counts[st] += 1
        self.stages.set_counts(counts)

    def _update_floating(self):
        if not getattr(self, "floating", None):
            return
        n = len(self._selected_source_rows()) if self.model.rows else 0
        if n:
            self.floating.count.setText(f"<b>{n:,}</b> highlighted")
            self.floating.refresh_labels()
            self.floating.setVisible(True)
            self.floating.place()
            self.view.set_bottom_space(self.floating.reserved_height())
        else:
            self.floating.setVisible(False)
            self.view.set_bottom_space(0)

    def open_palette(self):
        from .deck import CommandPalette
        items = []
        seen = set()
        for c in shortcuts.COMMANDS:
            a = self.cmd.get(c.id)
            if a is None or c.id == "palette" or id(a) in seen:
                continue
            seen.add(id(a))
            items.append((c.label, _native(shortcuts.effective(self.settings.shortcuts).get(c.id, "")), a))
        dlg = CommandPalette(items, self)
        g = self.geometry()
        dlg.move(g.center().x() - dlg.width() // 2, g.top() + 110)
        if dlg.exec() == QDialog.Accepted and dlg.chosen is not None:
            dlg.chosen.trigger()

    def _set_classic(self, on: bool):
        self.settings.theme = "classic" if on else "deck"
        self.settings.save()
        activity.info(f"Setting  Look → {'Classic' if on else 'Deck'} (next launch)")
        QMessageBox.information(self, APP_NAME, "The new look appears the next time you open CrateTag.")

    def apply_shortcuts(self):
        """Put the shortcuts from Settings onto the actions (called at start and after Settings)."""
        eff = shortcuts.effective(self.settings.shortcuts)
        for cid, a in self.cmd.items():
            a.setShortcut(QKeySequence.fromString(eff.get(cid, ""), QKeySequence.PortableText))
        if getattr(self, "floating", None):
            self.floating.refresh_labels()

    # ---- commands that act on the highlighted tracks
    def _check_highlighted(self, on: bool):
        rows = self._selected_source_rows()
        if rows:
            self.model.set_checked(rows, on)

    def _one_highlighted(self) -> int | None:
        rows = self._selected_source_rows()
        if len(rows) != 1:
            self.statusBar().showMessage("Highlight one track first." if not rows
                                         else "Highlight just one track for this.", 4000)
            return None
        return rows[0]

    def show_matches_selected(self):
        if self._busy():
            return
        i = self._one_highlighted()
        if i is not None:
            self._show_matches(i)

    def edit_search_selected(self):
        if self._busy():
            return
        i = self._one_highlighted()
        if i is not None:
            self._edit_search(i)

    def remove_selected(self):
        if self._busy() or isinstance(QApplication.focusWidget(), QLineEdit):
            return  # don't remove tracks while typing in a search box
        rows = self._selected_source_rows()
        if not rows:
            return
        self.model.remove({self.model.rows[i].key for i in rows})
        n = len(rows)
        self.statusBar().showMessage(
            f"Removed {n} track{'s' if n != 1 else ''} from the list. The files weren't touched.", 5000)

    def reveal_selected(self):
        rows = self._selected_source_rows()
        if not rows:
            self.statusBar().showMessage("Highlight a track first.", 4000)
            return
        paths = [self.model.rows[i].info.path for i in rows]
        found = [p for p in paths if p.exists()]
        if not found:
            self.statusBar().showMessage("That file isn't there any more (was the drive unplugged?).", 6000)
            return
        if len(found) > 10:
            ans = QMessageBox.question(self, APP_NAME, f"Reveal all {len(found)} highlighted tracks in Finder?")
            if ans != QMessageBox.Yes:
                return
        self._reveal_many(found)
        missing = len(paths) - len(found)
        if missing:
            self.statusBar().showMessage(f"{missing} highlighted file{'s' if missing != 1 else ''} couldn't be found.",
                                         6000)

    def _reveal_many(self, paths: list[Path]):
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", *[str(p) for p in paths]], check=False)
        else:
            for d in dict.fromkeys(p.parent for p in paths):
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def show_activity_log(self):
        dlg = ActivityLogDialog(self)
        dlg.show()
        self._log_dialog = dlg  # keep a reference while it's open

    @staticmethod
    def _source_dir() -> Path | None:
        """The CrateTag folder this copy was built from (or is running from)."""
        try:
            from . import _build_info  # written by build_mac.sh
            d = Path(_build_info.SOURCE_DIR)
            if d.is_dir():
                return d
        except Exception:  # noqa: BLE001
            pass
        if not getattr(sys, "frozen", False):
            return Path(__file__).resolve().parent.parent
        return None

    def update_from_downloads(self):
        self._launch_updater([], "Update CrateTag from the newest CrateTag download in your Downloads folder?")

    def _launch_updater(self, args: list[str], question: str | None) -> bool:
        """Run "Update CrateTag.command" (with args) in Terminal. It quits CrateTag, updates, rebuilds and reopens."""
        src = self._source_dir()
        script = src / "Update CrateTag.command" if src else None
        if not script or not script.exists():
            QMessageBox.information(
                self, APP_NAME,
                "Couldn't find the CrateTag folder this app was built from.\n\n"
                "Open your CrateTag folder in Finder and double-click \"Update CrateTag.command\".")
            return False
        if self._busy():
            QMessageBox.information(self, APP_NAME, "Wait for the current job to finish (or click Stop) first.")
            return False
        if question:
            n = self._unsaved_count()
            extra = (f"\n\nYou have {n} track{'s' if n != 1 else ''} with changes that haven't been written yet; "
                     "they'll be lost.") if n else ""
            ans = QMessageBox.question(
                self, APP_NAME,
                question + "\n\nCrateTag will close, a Terminal window will show the progress, and the new version "
                "opens when it's done (about a minute)." + extra)
            if ans != QMessageBox.Yes:
                return False
        # Run the updater as a bash command inside Terminal. Opening the .command file directly
        # gets blocked by macOS Gatekeeper because it came from a download; bash running it does not.
        # The updater quits CrateTag itself once it has started, so we don't close here.
        activity.info(f"Update   starting the updater {' '.join(args)}".rstrip())
        try:
            if sys.platform == "darwin":
                cmd = " ".join(["bash", shlex.quote(str(script)), *[shlex.quote(a) for a in args]])
                apple = _terminal_script(cmd)
                proc = subprocess.Popen(["osascript", "-e", apple], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, text=True, env=_clean_env())
                self._watch_updater_launch(proc, script, 0)
            else:
                subprocess.Popen(["bash", str(script), *args], env=_clean_env())
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't start the updater:\n{e}")
            return False
        self.statusBar().showMessage(
            "Starting the updater in Terminal. If macOS asks to let CrateTag control Terminal, click Allow.", 20000)
        return True

    # ---- updates from GitHub
    def check_for_updates(self, *_, quiet: bool = False):
        """Ask GitHub for the newest version (in the background). quiet: say nothing unless there's an update."""
        import threading
        from . import updates
        if getattr(self, "_update_checking", False):
            return
        self._update_checking = True
        if not quiet:
            self.statusBar().showMessage("Checking GitHub for updates…", 8000)
        sig = self._update_signal

        kind = updates.install_kind()

        def run():
            try:
                if kind == "dmg":
                    rel = updates.latest_release()
                    self._release = rel
                    sig.done.emit(rel["version"], None, quiet)
                else:
                    sig.done.emit(updates.latest_version(), None, quiet)
            except OSError as e:
                sig.done.emit(None, str(e), quiet)

        threading.Thread(target=run, daemon=True).start()

    def _update_checked(self, latest, error, quiet):
        from . import updates
        self._update_checking = False
        if error:
            activity.warn(f"Update   check failed: {error}")
            if not quiet:
                QMessageBox.information(self, APP_NAME, f"{error}\n\nCheck your internet connection and try again.")
            return
        activity.info(f"Update   GitHub has {latest}; this is {__version__}")
        if updates.is_newer(latest):
            self._available_update = latest
            self.update_btn.setText(f"Update to {latest}")
            self.update_btn.setVisible(True)
            self.statusBar().showMessage(f"CrateTag {latest} is available. Click \"Update to {latest}\" at the "
                                         "bottom right to install it.", 30000)
            if not quiet:
                self.install_update()
        elif not quiet:
            QMessageBox.information(self, APP_NAME, f"You have the latest version, {__version__}.")

    def install_update(self):
        latest = getattr(self, "_available_update", None)
        if not latest:
            self.check_for_updates()
            return
        from . import updates
        if updates.install_kind() == "dmg":
            self._install_dmg_update(latest)
            return
        self._launch_updater(["--github"], f"Install CrateTag {latest} from GitHub? (You have {__version__}.)")

    # ---- updating a copy installed from the .dmg
    def _app_bundle(self) -> Path | None:
        exe = Path(sys.executable).resolve()
        for p in exe.parents:
            if p.suffix == ".app":
                return p
        return None

    def _install_dmg_update(self, latest: str):
        import tempfile
        import threading
        rel = getattr(self, "_release", None)
        app = self._app_bundle()
        if not rel or not rel.get("dmg_url") or app is None:
            QMessageBox.information(self, APP_NAME, "Download the newest CrateTag .dmg from "
                                    "github.com/amv617/CrateTag/releases and drag CrateTag to Applications.")
            return
        if self._busy():
            QMessageBox.information(self, APP_NAME, "Wait for the current job to finish (or click Stop) first.")
            return
        n = self._unsaved_count()
        extra = (f"\n\nYou have {n} track{'s' if n != 1 else ''} with changes that haven't been written yet; "
                 "they'll be lost.") if n else ""
        if QMessageBox.question(
                self, APP_NAME,
                f"Install CrateTag {latest}? (You have {__version__}.)\n\nCrateTag downloads it, closes, "
                "and reopens as the new version in a few seconds." + extra) != QMessageBox.Yes:
            return
        dest = Path(tempfile.mkdtemp()) / f"CrateTag-{latest}.dmg"
        self.update_btn.setEnabled(False)
        self.statusBar().showMessage(f"Downloading CrateTag {latest}…")
        sig = self._update_signal

        def run():
            try:
                import requests
                with requests.get(rel["dmg_url"], stream=True, timeout=30) as r:
                    r.raise_for_status()
                    with open(dest, "wb") as fh:
                        for chunk in r.iter_content(1 << 16):
                            fh.write(chunk)
                sig.downloaded.emit(str(dest), "")
            except Exception as e:  # noqa: BLE001
                sig.downloaded.emit("", str(e))

        threading.Thread(target=run, daemon=True).start()

    def _dmg_downloaded(self, path: str, error: str):
        from . import updates
        self.update_btn.setEnabled(True)
        if error or not path:
            activity.warn(f"Update   download failed: {error}")
            QMessageBox.warning(self, APP_NAME, f"Couldn't download the update:\n{error}")
            return
        app = self._app_bundle()
        script = Path(path).with_name("swap.sh")
        script.write_text(updates.SWAP_SCRIPT)
        activity.info(f"Update   downloaded {Path(path).name}; installing over {app}")
        try:
            subprocess.Popen(["/bin/bash", str(script), str(app), path, str(os.getpid())],
                             start_new_session=True, env=_clean_env(),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't start the update:\n{e}")
            return
        self._quitting_for_update = True
        QApplication.quit()

    def _watch_updater_launch(self, proc, script: Path, waited: int):
        """If macOS refuses to let CrateTag drive Terminal, say how to fix it instead of failing silently."""
        rc = proc.poll()
        if rc is None:
            if waited < 120:  # the permission prompt can sit there until it's answered
                QTimer.singleShot(1000, lambda: self._watch_updater_launch(proc, script, waited + 1))
            return
        if rc == 0:
            return
        err = (proc.stderr.read() if proc.stderr else "") or ""
        blocked = "-1743" in err or "not allowed" in err.lower() or "not authorized" in err.lower()
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Warning)
        box.setText("macOS didn't let CrateTag open Terminal for the update." if blocked
                    else "The updater couldn't be started.")
        box.setInformativeText(
            "To allow it: System Settings > Privacy & Security > Automation > CrateTag, "
            "and turn on Terminal. Then choose Update from Downloads again.\n\n"
            "Or run the update yourself in Terminal:\n"
            f"cd {shlex.quote(str(script.parent))}\n./\"Update CrateTag.command\"")
        settings_btn = box.addButton("Open Automation Settings", QMessageBox.ActionRole) if blocked else None
        box.addButton(QMessageBox.Ok)
        box.exec()
        if settings_btn is not None and box.clickedButton() is settings_btn:
            subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"])

    def _about(self):
        """About box with everything centered (QMessageBox left-aligns its text on macOS)."""
        dlg = QDialog(self)
        dlg.setWindowTitle(f"About {APP_NAME}")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(36, 28, 36, 20)
        lay.setSpacing(10)

        def label(html: str, size: int | None = None, muted: bool = False) -> QLabel:
            lab = QLabel(html)
            lab.setAlignment(Qt.AlignCenter)
            lab.setWordWrap(True)
            lab.setTextFormat(Qt.RichText)
            style = []
            if size:
                style.append(f"font-size: {size}px;")
            if muted:
                style.append("color: gray;")
            if style:
                lab.setStyleSheet(" ".join(style))
            lay.addWidget(lab)
            return lab

        logo_file = ASSETS / "icon.png"
        if logo_file.exists():
            logo = QLabel()
            pm = QPixmap(str(logo_file)).scaled(192, 192, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            pm.setDevicePixelRatio(2.0)  # 96 points, sharp on Retina
            logo.setPixmap(pm)
            logo.setAlignment(Qt.AlignCenter)
            lay.addWidget(logo)
        label(f"<b>{APP_NAME}</b>", 22)
        label(f"Version {__version__}", muted=True)
        lay.addSpacing(6)
        label("Looks up and edits artist, title, year and genre in MP3 tags.")
        label("Only those fields are ever changed. Serato cue points, loops, colors and "
              "beatgrids stored in the file are left untouched.", muted=True)
        lay.addSpacing(6)
        label("<b>Created by DJ CASE.</b>")
        label("@djcaseboston")
        lay.addSpacing(6)
        label("Data from MusicBrainz, Discogs, Deezer, Apple iTunes and AcoustID.", 11, muted=True)

        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.clicked.connect(dlg.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(ok)
        row.addStretch(1)
        lay.addSpacing(4)
        lay.addLayout(row)
        dlg.setFixedWidth(420)
        dlg.exec()

    # ---- state
    def _busy(self) -> bool:
        return self._thread is not None

    def _set_busy(self, label: str | None):
        busy = label is not None
        for a in (self.a_folder, self.a_files, self.a_clear, self.a_lookup, self.a_write, self.a_settings,
                  self.a_check_all, self.a_check_none):
            a.setEnabled(not busy)
        if hasattr(self, "crates"):
            self.crates.tree.setEnabled(not busy)
        if getattr(self, "folders", None) is not None:
            self.folders.setEnabled(not busy)
        if getattr(self, "floating", None):
            self.floating.refresh_labels()
        self.a_stop.setEnabled(busy)
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)
            self.statusBar().showMessage(label)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers if busy else
                                  QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)

    def _visible_rows(self) -> list[int]:
        """Rows shown right now (all rows unless a search is hiding some)."""
        if not self.proxy.is_filtering():
            return list(range(len(self.model.rows)))
        return sorted(self.proxy.mapToSource(self.proxy.index(r, 0)).row() for r in range(self.proxy.rowCount()))

    def _lookup_targets(self) -> list[int]:
        """Highlighted rows first, then checked rows, then every row that's showing."""
        return (self._selected_source_rows() or self.model.checked_indexes() or self._visible_rows())

    def _update_lookup_label(self, *args):
        if not hasattr(self, "a_lookup"):
            return
        n = sum(1 for i in self._lookup_targets() if not self.model.rows[i].info.error) if self.model.rows else 0
        self.a_lookup.setText(f"Analyze ({n})" if n else "Analyze")
        # Write button follows the highlight: "Write Selected (8)", or "Write Checked (2)" with nothing highlighted
        rows, kind = self._write_targets() if self.model.rows else ([], "checked")
        w = sum(1 for r in rows if self._writable(r))
        self.a_write.setText(f"Write {kind.title()} ({w})" if w else f"Write {kind.title()}")
        self.a_write.setToolTip("Write the changes for the highlighted tracks" if kind == "selected"
                                else "Write the changes for the checked tracks (highlight tracks to write those instead)")
        if not self._busy():
            self.a_write.setEnabled(w > 0)
        self._update_floating()

    # ---- manual year / genre for several tracks at once
    def _on_cell_edited(self, row: int, field: str, value: str):
        """Edited one cell while several rows are highlighted: offer to apply it to all of them."""
        sel = self._selected_source_rows()
        if row not in sel or len(sel) < 2:
            self._ensure_mode_includes(field)
            return
        others = [i for i in sel if i != row]
        QTimer.singleShot(0, lambda: self._confirm_bulk(others, field, value))

    def _confirm_bulk(self, rows: list[int], field: str, value: str):
        shown = value or "(blank)"
        ans = QMessageBox.question(
            self, APP_NAME,
            f"Set {field} to {shown} for all {len(rows) + 1} highlighted tracks?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if ans == QMessageBox.Yes:
            self.model.manual_set(rows, field, value)
        self._ensure_mode_includes(field)

    def bulk_set_dialog(self, field: str):
        if self._busy():
            return
        rows = self._selected_source_rows()
        if not rows:
            self.statusBar().showMessage("Highlight the tracks first.", 4000)
            return
        n = len(rows)
        title = f"Set {field.title()}"
        prompt = f"{field.title()} for {n} highlighted track{'s' if n != 1 else ''}:"
        first = self.model.rows[rows[0]]
        if field == "year":
            current = first.new_year or first.info.year
            while True:
                value, ok = QInputDialog.getText(self, title, prompt, QLineEdit.Normal, current)
                if not ok:
                    return
                value = value.strip()
                if not value or YEAR_RE.match(value):
                    break
                QMessageBox.warning(self, APP_NAME, "Enter a four-digit year, such as 2011.")
                current = value
        elif field in ("artist", "title"):
            pending = first.new_artist if field == "artist" else first.new_title
            current = pending if pending is not None else (first.info.artist if field == "artist" else first.info.title)
            value, ok = QInputDialog.getText(self, title, prompt, QLineEdit.Normal, current)
            if not ok:
                return
            value = value.strip()
        else:
            genres = self.model.known_genres()
            current = first.new_genre or first.info.genre
            if current and current not in genres:
                genres.insert(0, current)
            idx = genres.index(current) if current in genres else 0
            value, ok = QInputDialog.getItem(self, title, prompt + "\n(pick one or type a new genre)",
                                             genres or [""], idx, True)
            if not ok:
                return
            value = value.strip()
        if not value:
            return
        self.model.manual_set(rows, field, value)
        self._ensure_mode_includes(field)
        self.statusBar().showMessage(
            f"Set {field} to {value} on {n} track{'s' if n != 1 else ''}. Click Write to save.", 8000)

    def _ensure_mode_includes(self, field: str):
        """Typing a genre while in Year-only mode (or vice versa) switches to writing both."""
        mode = self.model.write_mode
        if (field == "year" and mode == "genre") or (field == "genre" and mode == "year"):
            self.write_combo.setCurrentIndex(self.write_combo.findData("both"))
            self.statusBar().showMessage("Write mode switched to Year & Genre so your edit gets saved.", 6000)

    def _write_mode_changed(self, *_):
        activity.info(f"Setting  Write → {self.write_combo.currentText()}")
        self.settings.write_mode = self.write_combo.currentData()
        self.model.write_mode = self.settings.write_mode
        self.settings.save()
        self.model.recheck(self.settings)

    def _sources_changed(self, *_):
        """Re-pick year/genre from the matches already found. No new lookups needed."""
        from .lookup import apply_choices
        activity.info(f"Setting  Year from → {self.year_src_combo.currentText()}, "
                      f"Genre from → {self.genre_src_combo.currentText()}")
        self.settings.year_source = self.year_src_combo.currentData()
        self.settings.genre_source = self.genre_src_combo.currentData()
        self.settings.save()
        n = 0
        for r in self.model.rows:
            if r.proposal is not None and r.proposal.candidates and r.status not in ("Written",):
                apply_choices(r.proposal, self.settings)
                self.model.apply_proposal(r.key, r.proposal, self.settings, keep_manual=True)
                n += 1
        if n:
            no_year = sum(1 for r in self.model.rows if r.proposal and not r.proposal.year)
            no_genre = sum(1 for r in self.model.rows if r.proposal and not r.proposal.genre)
            msg = f"Updated {n} tracks from the matches already found."
            gaps = []
            if no_year and self.settings.year_source != "auto":
                gaps.append(f"{no_year} have no year from {self.year_src_combo.currentText()}")
            if no_genre and self.settings.genre_source != "auto":
                gaps.append(f"{no_genre} have no genre from {self.genre_src_combo.currentText()}")
            if gaps:
                msg += " " + "; ".join(gaps) + ". Try Auto, or Edit Search on those tracks."
            self.statusBar().showMessage(msg, 12000)

    def highlight_all(self):
        """Highlight every track that's showing. Big libraries take a moment, so show a busy cursor."""
        big = self.proxy.rowCount() > 5000
        if big:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            self.statusBar().showMessage(f"Highlighting {self.proxy.rowCount():,} tracks…")
            QApplication.processEvents()
        try:
            self.view.selectAll()
        finally:
            if big:
                QApplication.restoreOverrideCursor()
                self.statusBar().clearMessage()

    def select_all(self):
        """Highlight and check every track that's showing (only the search results while searching)."""
        self.highlight_all()
        if self.proxy.is_filtering():
            self.model.set_checked(self._visible_rows(), True)
        else:
            self.model.set_all_checked(True)

    def select_none(self):
        self.view.clearSelection()
        self.model.set_all_checked(False)

    def _header_check_clicked(self):
        """The box in the checkbox column's header: check every track showing, or uncheck them all."""
        if self._busy() or not self.model.rows:
            return
        rows = self._visible_rows()
        all_on = bool(rows) and all(self.model.rows[i].checked for i in rows)
        if self.proxy.is_filtering():
            self.model.set_checked(rows, not all_on)
        else:
            self.model.set_all_checked(not all_on)

    def _update_header_check(self):
        rows = self.model.rows
        if not rows:
            state = Qt.Unchecked
        else:
            if self.proxy.is_filtering():
                vis = self._visible_rows()
                on = sum(1 for i in vis if rows[i].checked)
                total = len(vis)
            else:
                on = sum(1 for r in rows if r.checked)
                total = len(rows)
            state = Qt.Unchecked if on == 0 else Qt.Checked if on == total else Qt.PartiallyChecked
        self.view.horizontalHeader().set_check_state(state)

    def _refresh_counts(self, *args):
        n = len(self.model.rows)
        c = len(self.model.checked_rows())
        shown = self.proxy.rowCount() if self.proxy.is_filtering() else n
        self.count_label.setText((f"{n} tracks · {shown} shown · {c} checked" if shown != n
                                  else f"{n} tracks · {c} checked") if n else "")
        self.empty_hint.setVisible(n == 0)
        self.view.setVisible(n > 0)
        self._update_header_check()
        self._update_lookup_label()
        if getattr(self, "stages", None):
            self._stage_timer.start()
            self._update_header()

    def _on_progress(self, i, total):
        if total <= 0:  # unknown total: keep the busy indicator and show a running count
            self.progress.setRange(0, 0)
            self.statusBar().showMessage(f"Loaded {i:,} tracks…")
            return
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(i)

    def _start(self, worker, label: str, finished_signal, on_finished):
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        finished_signal.connect(on_finished)
        finished_signal.connect(thread.quit)
        thread.finished.connect(self._thread_done)
        self._thread, self._worker = thread, worker
        self._set_busy(label)
        thread.start()

    def _thread_done(self):
        if self._thread:
            self._thread.deleteLater()
        if self._worker:
            self._worker.deleteLater()
        self._thread = self._worker = None
        self._set_busy(None)
        self._refresh_counts()

    # ---- adding files
    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Choose a folder of MP3s", self.settings.last_folder)
        if d:
            self.settings.last_folder = d
            self.settings.save()
            self.scan([d])

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Choose MP3 files", self.settings.last_folder, "MP3 files (*.mp3)")
        if files:
            self.settings.last_folder = str(Path(files[0]).parent)
            self.settings.save()
            self.scan(files)

    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls() and not self._busy():
            e.acceptProposedAction()

    def dropEvent(self, e):  # noqa: N802
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.scan(paths)

    def scan(self, paths: list[str]):
        if self._busy():
            return
        roots = [Path(p) for p in paths]
        if self.source_root is None:
            first = roots[0]
            self.source_root = first if first.is_dir() else first.parent
        w = ScanWorker(paths)
        w.batch.connect(self.model.add_tracks)
        w.progress.connect(self._on_progress)
        self._start(w, "Reading tags…", w.finished, self._scan_done)

    def _library_done(self, total: int):
        activity.info(f"Load     All Songs: {total:,} MP3s")
        self._loading_crate = None
        skipped = getattr(getattr(self, "_library_worker", None), "skipped", 0)
        self._library_worker = None
        missing = sum(1 for r in self.model.rows if r.info.error.startswith("File not found"))
        from .serato import ALL_SONGS_KEY
        it = self.crates.item_for(ALL_SONGS_KEY)
        if it is not None:  # exact count now that duplicates across drives are merged
            if self.deck:
                it.setText(1, f"{total:,}")
            else:
                it.setText(0, f"All Songs  ({total:,})")
        msg = f"All Songs: {total:,} MP3s"
        extras = []
        if skipped:
            extras.append(f"{skipped:,} videos and other non-MP3 files not shown")
        if missing:
            extras.append(f"{missing:,} marked missing by Serato")
        if extras:
            msg += " (" + "; ".join(extras) + ")"
        self.statusBar().showMessage(msg + ". Search, highlight and Analyze in batches.", 20000)

    def _scan_done(self, total: int):
        crate = self._loading_crate
        activity.info(f"Load     {('crate ' + crate.full_name) if crate is not None else 'folder/files'}: {total} tracks")
        self._loading_crate = None
        if crate is not None:
            missing = sum(1 for r in self.model.rows if r.info.error.startswith("File not found"))
            other = sum(1 for r in self.model.rows if "MP3s only" in r.info.error)
            msg = f"Crate {crate.full_name}: {total} track{'s' if total != 1 else ''}"
            extras = []
            if missing:
                extras.append(f"{missing} not found")
            if other:
                extras.append(f"{other} not MP3")
            if extras:
                msg += " (" + ", ".join(extras) + ")"
            self.statusBar().showMessage(msg + ". Highlight tracks and click Analyze, or edit them directly.", 15000)
            return
        if total == 0:
            self.statusBar().showMessage("No MP3 files found there.", 6000)
        else:
            self.statusBar().showMessage(f"Loaded {total} files. Highlight tracks (or none for all) and click Analyze.", 8000)

    def clear_all(self):
        self.model.clear()
        self.source_root = None
        self.current_crate = None
        self.current_folder = None
        self._update_title()

    # ---- Serato crates
    def _apply_track_search(self):
        text = self.track_search.text()
        self.proxy.set_search(text, self.track_search_field.currentData())
        self._refresh_counts()
        if self.proxy.is_filtering():
            n = self.proxy.rowCount()
            self.statusBar().showMessage(
                f"{n} track{'s' if n != 1 else ''} match \"{text.strip()}\"." if n
                else f"No tracks match \"{text.strip()}\".", 6000)

    def focus_track_search(self):
        self.track_search.setFocus()
        self.track_search.selectAll()

    def focus_crate_search(self):
        if not self.a_crates.isChecked():
            self.a_crates.setChecked(True)
            self._toggle_crates()
        self.crates.search.setFocus()
        self.crates.search.selectAll()

    def _toggle_crates(self, *_):
        on = self.a_crates.isChecked()
        if self.deck:
            self._show_side_page("crates" if on else None)
            return
        self.crates.setVisible(on)
        self.settings.show_crates = on
        if on and not self.crates.libraries:
            self.crates.refresh()

    def _crate_drive_changed(self, drive: str):
        self.settings.crate_drive = drive
        self.settings.save()

    def _update_title(self):
        t = f"{APP_NAME} v{__version__}"
        if self.current_crate is not None:
            t += f"  ·  {self.current_crate.name}"
        self.setWindowTitle(t)
        self._update_header()

    def _update_header(self):
        if not getattr(self, "title_lbl", None):
            return
        n = len(self.model.rows)
        crate = self.current_crate
        folder = getattr(self, "current_folder", None)
        if crate is not None:
            title = crate.name
            path = " ▸ ".join(crate.parts) if len(getattr(crate, "parts", [])) > 1 else ""
        elif folder and n:
            title, path = Path(folder).name or folder, folder
        elif n:
            title, path = "Tracks", ""
        else:
            title, path = "No tracks loaded", ""
        bits = [b for b in (path, f"{n:,} track{'s' if n != 1 else ''}" if n else "") if b]
        self.title_lbl.setText(title)
        self.subtitle_lbl.setText("  ·  ".join(bits))

    def _unsaved_count(self) -> int:
        mode = self.model.write_mode
        return sum(1 for r in self.model.rows
                   if (r.year_edited or r.genre_edited or r.new_artist is not None or r.new_title is not None
                       or r.checked) and r.has_changes(mode))

    def load_crate(self, crate, include_subcrates: bool = False, append: bool = False):
        from .serato import ALL_SONGS_KEY, crate_tracks
        if self._busy():
            return
        is_all = crate.key == ALL_SONGS_KEY
        if not append:
            n = self._unsaved_count()
            if n:
                ans = QMessageBox.question(
                    self, APP_NAME,
                    f"You have {n} track{'s' if n != 1 else ''} with changes that haven't been written yet.\n\n"
                    f"Load \"{crate.name}\" anyway? Those changes will be dropped.")
                if ans != QMessageBox.Yes:
                    if self.current_crate is not None:
                        it = self.crates.item_for(self.current_crate.key)
                        if it:
                            self.crates.tree.setCurrentItem(it)
                    return
        paths = [] if is_all else crate_tracks(crate, include_subcrates)
        it = self.crates.item_for(crate.key)
        if it is not None:
            p = it.parent()
            while p is not None:
                p.setExpanded(True)
                p = p.parent()
            self.crates.tree.setCurrentItem(it)
        if not append:
            self.model.clear()
            self.current_crate = crate
            self.current_folder = None
            self.source_root = crate.library.root if getattr(crate, "library", None) else None
            self._update_title()
        if is_all:
            libs = self.crates.selected_libraries()
            self._loading_crate = crate
            w = LibraryWorker(libs)
            self._library_worker = w
            w.batch.connect(self.model.add_tracks)
            w.progress.connect(self._on_progress)
            where = ", ".join(l.label for l in libs) or "Serato"
            self._start(w, f"Loading every track in your Serato library ({where})…", w.finished, self._library_done)
            return
        if not paths:
            self.statusBar().showMessage(f"Crate {crate.full_name} is empty.", 6000)
            return
        self._loading_crate = crate
        w = ScanWorker([str(p) for p in paths], explicit=True)
        w.batch.connect(self.model.add_tracks)
        w.progress.connect(self._on_progress)
        self._start(w, f"Reading {len(paths)} tracks from {crate.name}…", w.finished, self._scan_done)

    # ---- lookup
    def _selected_source_rows(self) -> list[int]:
        """Highlighted rows as model row numbers. Works from the selection's ranges, so highlighting
        all of a huge library is instant."""
        sm = self.view.selectionModel()
        if sm is None or not sm.hasSelection():
            return []
        proxy_rows: set[int] = set()
        for rg in sm.selection():
            proxy_rows.update(range(rg.top(), rg.bottom() + 1))
        if not self.proxy.is_filtering():
            return sorted(proxy_rows)  # no search: the list shows the model's rows in order
        m = self.proxy.mapToSource
        idx = self.proxy.index
        return sorted(m(idx(r, 0)).row() for r in proxy_rows)

    def lookup(self):
        if self._busy() or not self.model.rows:
            return
        s = self.settings
        if not s.use_musicbrainz and not (s.use_discogs and s.discogs_token) and not s.use_acoustid \
                and not getattr(s, "use_deezer", True) and not getattr(s, "use_itunes", True):
            QMessageBox.warning(self, APP_NAME, "Turn on at least one lookup source in Settings.")
            return
        sel = self._lookup_targets()
        self._lookup_rows(sel)

    def _lookup_rows(self, sel: list[int]):
        if self._busy() or not sel:
            return
        s = self.settings
        from dataclasses import replace as _replace
        tracks = []
        for i in sel:
            r = self.model.rows[i]
            if r.info.error:
                continue
            # Search with your unsaved artist/title fixes, if any
            extra = dict(r.info.extra)
            if r.new_artist is not None:
                extra["_pending_artist"] = r.new_artist
            if r.new_title is not None:
                extra["_pending_title"] = r.new_title
            tracks.append(_replace(r.info, artist=r.new_artist if r.new_artist is not None else r.info.artist,
                                   title=r.new_title if r.new_title is not None else r.info.title, extra=extra))
        if not tracks:
            return
        if len(tracks) > 500:
            secs = len(tracks) * (2.5 if s.use_discogs and s.discogs_token else 1.5)
            hrs = secs / 3600
            when = f"about {hrs:.1f} hours" if hrs >= 1 else f"about {max(1, int(secs // 60))} minutes"
            ans = QMessageBox.question(
                self, APP_NAME,
                f"Analyze {len(tracks):,} tracks? That will take {when}, because MusicBrainz allows about one "
                "request per second.\n\nTip: highlight a smaller group (or search first) to do it in batches. "
                "You can click Stop at any time; finished tracks keep their results.")
            if ans != QMessageBox.Yes:
                return
        srcs = [n for n, on in (("MusicBrainz", s.use_musicbrainz), ("Discogs", s.use_discogs and bool(s.discogs_token)),
                                ("Deezer", getattr(s, "use_deezer", True)), ("iTunes", getattr(s, "use_itunes", True)),
                                ("AcoustID", s.use_acoustid)) if on]
        activity.info(f"Analyze  started: {len(tracks)} tracks; sources: {', '.join(srcs) or 'none'}; "
                      f"year from {s.year_source}, genre from {s.genre_source}")
        for t in tracks:
            self.model.set_status(str(t.path), "Queued")
        w = LookupWorker(self.settings, tracks)
        # Connect to bound methods (not lambdas) so updates run on the main thread.
        w.row_started.connect(self._on_row_started)
        w.row_done.connect(self._on_row_done)
        w.progress.connect(self._on_progress)
        secs = len(tracks) * (2 if s.use_discogs and s.discogs_token else 1) * 1.2
        eta = f" (about {int(secs // 60)} min)" if secs > 90 else ""
        self._start(w, f"Analyzing {len(tracks)} tracks{eta}…", w.finished, self._lookup_done)

    def _on_row_started(self, key: str):
        self.model.set_status(key, "Analyzing…")

    def _on_row_done(self, key: str, prop: Proposal):
        self.model.apply_proposal(key, prop, self.settings)

    def _lookup_done(self, cancelled: bool):
        for r in self.model.rows:
            if r.status in ("Queued", "Analyzing…"):
                r.status = "Not analyzed"
        self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(max(0, len(self.model.rows) - 1), len(COLS) - 1))
        c = len(self.model.checked_rows())
        msg = "Stopped." if cancelled else "Analysis finished."
        activity.info(f"Analyze  {'stopped' if cancelled else 'finished'}; {len(self.model.checked_rows())} tracks checked")
        self.statusBar().showMessage(f"{msg} {c} tracks checked and ready to write. Double-click a row to see all matches.", 12000)

    def stop(self):
        if self._worker is not None and hasattr(self._worker, "cancel"):
            self._worker.cancel()
            self.statusBar().showMessage("Stopping after the current track…")

    # ---- write
    def _writable(self, r: Row) -> bool:
        return r.has_changes(self.model.write_mode) and not r.info.error

    def _write_targets(self) -> tuple[list[Row], str]:
        """Highlighted tracks that have something to write; otherwise the checked tracks.

        Highlighting a track just to look at it shouldn't hide changes waiting on checked tracks."""
        sel = self._selected_source_rows()
        if sel:
            rows = [self.model.rows[i] for i in sel]
            if any(self._writable(r) for r in rows):
                return rows, "selected"
        return self.model.checked_rows(), "checked"

    def write_checked(self):
        """The Write button: writes the highlighted tracks, or the checked ones if none are highlighted."""
        rows, kind = self._write_targets()
        self._write_rows(rows, kind)

    def write_selected(self):
        sel = self._selected_source_rows()
        self._write_rows([self.model.rows[i] for i in sel], "selected")

    def write_only_checked(self):
        self._write_rows(self.model.checked_rows(), "checked")

    def _write_rows(self, rows: list[Row], kind: str = "checked"):
        if self._busy():
            return
        jobs = []
        mode = self.model.write_mode
        for r in rows:
            year = r.new_year if (r.year_changes() and mode in ("both", "year")) else None
            genre = r.new_genre if (r.genre_changes() and mode in ("both", "genre")) else None
            if self.settings.only_fill_blanks:  # applies to looked-up values, never to your own edits
                year = year if (not r.info.year or r.year_edited) else None
                genre = genre if (not r.info.genre or r.genre_edited) else None
            job = WriteJob(r.info.path, year=year, genre=genre,
                           artist=r.new_artist if r.artist_changes() else None,
                           title=r.new_title if r.title_changes() else None)
            if not job.is_empty():
                jobs.append(job)
        if not jobs:
            sel = len(self._selected_source_rows())
            activity.warn(f"Write    nothing to write: {len(rows)} {kind} track(s) had no changes for "
                          f"'{self.write_combo.currentText()}' (highlighted: {sel}, checked: {len(self.model.checked_rows())})")
            QMessageBox.information(
                self, APP_NAME,
                f"Nothing to write. The {kind} tracks have no changes for the current Write setting "
                f"({self.write_combo.currentText()}).")
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H.%M.%S")
        base = Path(self.settings.backup_dir).expanduser()
        bmode = self.settings.backup_mode
        backup_root = undo_log = None
        if bmode == "copies":
            need = sum(j.path.stat().st_size for j in jobs if j.path.exists())
            probe = base
            while not probe.exists() and probe.parent != probe:
                probe = probe.parent
            free = shutil.disk_usage(probe).free
            if need > free * 0.9:
                ans = QMessageBox.question(
                    self, APP_NAME,
                    f"Copying these files needs about {_size(need)}, but only {_size(free)} is free.\n\n"
                    "Use a small undo log instead? It records the old year and genre so they can be restored.",
                    QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Yes)
                if ans != QMessageBox.Yes:
                    return
                bmode = "undo"
            else:
                backup_root = base / stamp
        if bmode == "undo":
            undo_log = base / f"CrateTag undo {stamp}.csv"
        fields = []
        if any(j.artist is not None for j in jobs):
            fields.append("artist")
        if any(j.title is not None for j in jobs):
            fields.append("title")
        if any(j.year is not None for j in jobs):
            fields.append("year")
        if any(j.genre is not None for j in jobs):
            fields.append("genre")
        what = ", ".join(fields[:-1]) + (" and " if len(fields) > 1 else "") + fields[-1]
        msg = f"Write {what} to {len(jobs)} file{'s' if len(jobs) != 1 else ''}?"
        if backup_root:
            need = sum(j.path.stat().st_size for j in jobs if j.path.exists())
            msg += f"\n\nOriginals ({_size(need)}) will be copied to:\n{backup_root}"
        elif undo_log:
            msg += "\n\nThe old values are saved in a small undo log, so you can restore them with Tracks > Undo a Previous Write."
        else:
            msg += "\n\nBackups are turned off; this can't be undone."
        msg += "\n\nNo other fields change. Serato cue points and other data in the files are kept."
        if QMessageBox.question(self, APP_NAME, msg) != QMessageBox.Yes:
            activity.info(f"Write    cancelled at the confirmation ({len(jobs)} tracks)")
            return
        activity.info(f"Write    started: {len(jobs)} {kind} tracks; {what}; before writing: {bmode}")
        w = WriteWorker(self.settings, jobs, self.source_root, backup_root, undo_log)
        w.row_done.connect(self.model.apply_written)
        w.progress.connect(self._on_progress)
        self._start(w, f"Writing {len(jobs)} files…", w.finished, self._write_done)

    def _write_done(self, ok: int, fail: int, backup: str):
        text = f"Updated {ok} file{'s' if ok != 1 else ''}."
        if fail:
            text += f" {fail} failed (see Status column)."
        detail = ("\n\nIn Serato, select the updated tracks and choose the option to re-read ID3 tags "
                  "so the library shows the new values.")
        if backup and backup.endswith(".csv"):
            detail += f"\n\nUndo log: {Path(backup).name}\n(Tracks > Undo a Previous Write)"
        elif backup:
            detail += f"\n\nBackups: {backup}"
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setText(text)
        box.setInformativeText(detail.strip())
        if backup and os.path.exists(backup):
            reveal = box.addButton("Show in Finder", QMessageBox.ActionRole)
        else:
            reveal = None
        box.addButton(QMessageBox.Ok)
        box.exec()
        if reveal is not None and box.clickedButton() is reveal:
            self._reveal(Path(backup))
        self.statusBar().showMessage(text, 10000)

    def undo_previous_write(self):
        if self._busy():
            return
        base = Path(self.settings.backup_dir).expanduser()
        path, _ = QFileDialog.getOpenFileName(self, "Choose an undo log", str(base), "CrateTag undo logs (*.csv)")
        if not path:
            return
        try:
            jobs = read_undo_log(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, f"Couldn't read that undo log:\n{e}")
            return
        missing = [j.path for j in jobs if not j.path.exists()]
        jobs = [j for j in jobs if j.path.exists()]
        if not jobs:
            QMessageBox.information(self, APP_NAME, "None of the files in that log could be found.")
            return
        msg = f"Restore the original tags on {len(jobs)} file{'s' if len(jobs) != 1 else ''}?"
        if missing:
            msg += f"\n\n{len(missing)} file(s) in the log were moved or deleted and will be skipped."
        if QMessageBox.question(self, APP_NAME, msg) != QMessageBox.Yes:
            return
        activity.info(f"Restore  started from undo log {path}: {len(jobs)} files"
                      + (f" ({len(missing)} missing, skipped)" if missing else ""))
        w = WriteWorker(self.settings, jobs, None, done_label="Restored")
        w.row_done.connect(self.model.apply_written)
        w.progress.connect(self._on_progress)
        self._start(w, f"Restoring {len(jobs)} files…", w.finished, self._undo_done)

    def _undo_done(self, ok: int, fail: int, _where: str):
        text = f"Restored {ok} file{'s' if ok != 1 else ''}."
        if fail:
            text += f" {fail} failed."
        QMessageBox.information(self, APP_NAME, text + "\n\nIn Serato, rescan ID3 tags for those tracks.")
        self.statusBar().showMessage(text, 10000)

    # ---- misc interactions
    def open_settings(self, *_, tab: str | None = None):
        dlg = SettingsDialog(self.settings, self)
        if tab:
            dlg.show_tab(tab)
        before = shortcuts.effective(self.settings.shortcuts)
        if dlg.exec() == QDialog.Accepted:
            dlg.apply()
            self.settings.save()
            after = shortcuts.effective(self.settings.shortcuts)
            for cid, v in after.items():
                if v != before.get(cid):
                    activity.info(f"Shortcut changed: {shortcuts.BY_ID[cid].label} "
                                  f"{activity.q(_native(before.get(cid, '')))} → {activity.q(_native(v))}")
            self.apply_shortcuts()

    def _double_clicked(self, idx):
        src = self.proxy.mapToSource(idx)
        if src.column() in EDIT_FIELDS or src.column() == C_CHECK:
            return  # editing / checkbox handled by the view
        if src.column() == C_SEARCH:
            self._edit_search(src.row())
            return
        self._show_matches(src.row())

    def _show_matches(self, i: int):
        row = self.model.rows[i]
        if not row.proposal:
            self.statusBar().showMessage("Look this track up first.", 4000)
            return
        dlg = MatchesDialog(row, self.settings, self)
        if dlg.exec() == QDialog.Accepted and dlg.chosen:
            year, genre = dlg.chosen
            if year:
                self.model.setData(self.model.index(i, C_NYEAR), year)
            if genre:
                self.model.setData(self.model.index(i, C_NGENRE), genre)
            if self.model.rows[i].has_changes(self.model.write_mode):
                self.model.rows[i].checked = True
                self.model.rows[i].status = "Ready to write"
                self.model._emit_row(i)
                self.model.checks_changed.emit()

    def _edit_search(self, i: int):
        """Let the user say exactly what to search for, then look the track up again."""
        if self._busy():
            return
        row = self.model.rows[i]
        ex = row.info.extra
        p = row.proposal
        cur_a = ex.get("search_artist") if ex.get("search_title") else (p.search_artist if p else "")
        cur_t = ex.get("search_title") or (p.search_title if p else "")
        if not cur_t:
            from .parsing import search_terms
            cur_a, cur_t = search_terms(row.info.artist, row.info.title, row.info.path,
                                        [w.strip() for w in self.settings.ignore_words.split(",") if w.strip()])
        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Search")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(f"<b>{row.info.path.name}</b>"))
        form = QFormLayout()
        a_edit = QLineEdit(cur_a or "")
        t_edit = QLineEdit(cur_t or "")
        a_edit.setPlaceholderText("Artist (optional)")
        t_edit.setPlaceholderText("Song title only, e.g. LET 'EM KNOW")
        form.addRow("Artist:", a_edit)
        form.addRow("Title:", t_edit)
        lay.addLayout(form)
        hint = QLabel("Use just the main artist and the song name. Leave out edits, intros, key and BPM.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        lay.addWidget(hint)
        bb = QDialogButtonBox()
        go = bb.addButton("Search", QDialogButtonBox.AcceptRole)
        go.setDefault(True)
        reset = bb.addButton("Use Automatic", QDialogButtonBox.ResetRole)
        bb.addButton(QDialogButtonBox.Cancel)
        lay.addWidget(bb)
        choice = {"v": None}
        go.clicked.connect(lambda: (choice.update(v="go"), dlg.accept()))
        reset.clicked.connect(lambda: (choice.update(v="reset"), dlg.accept()))
        bb.rejected.connect(dlg.reject)
        dlg.resize(520, dlg.sizeHint().height())
        t_edit.setFocus()
        t_edit.selectAll()
        if dlg.exec() != QDialog.Accepted:
            return
        if choice["v"] == "reset" or not t_edit.text().strip():
            ex.pop("search_artist", None)
            ex.pop("search_title", None)
        else:
            ex["search_artist"] = a_edit.text().strip()
            ex["search_title"] = t_edit.text().strip()
        self.model._emit_row(i)
        self._lookup_rows([i])

    def _reveal(self, path: Path):
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", str(path)], check=False)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path if path.is_dir() else path.parent)))

    def _context_menu(self, pos):
        if self._busy():
            return
        rows = self._selected_source_rows()
        if not rows:
            return
        m = QMenu(self)
        m.addAction("Analyze Again" if any(self.model.rows[i].proposal for i in rows) else "Analyze",
                    lambda: self._lookup_rows(rows))
        if len(rows) == 1:
            m.addAction(self.cmd["edit_search"])
            m.addAction(self.cmd["show_matches"])
        m.addAction(self.cmd["reveal"])
        m.addSeparator()
        n = len(rows)
        w = sum(1 for i in rows if self._writable(self.model.rows[i]))
        a = m.addAction(f"Write {w} Selected Track{'s' if w != 1 else ''}", self.write_selected)
        a.setEnabled(w > 0)
        m.addSeparator()
        m.addAction(f"Set Artist for {n} Track{'s' if n != 1 else ''}…", lambda: self.bulk_set_dialog("artist"))
        m.addAction(f"Set Year for {n} Track{'s' if n != 1 else ''}…", lambda: self.bulk_set_dialog("year"))
        m.addAction(f"Set Genre for {n} Track{'s' if n != 1 else ''}…", lambda: self.bulk_set_dialog("genre"))
        m.addSeparator()
        m.addAction("Check", lambda: self.model.set_checked(rows, True))
        m.addAction("Uncheck", lambda: self.model.set_checked(rows, False))
        m.addSeparator()
        m.addAction(self.cmd["remove"])
        m.exec(self.view.viewport().mapToGlobal(pos))

    # ---- column layout
    DEFAULT_WIDTHS = {C_CHECK: 52, C_FILE: 260, C_ARTIST: 150, C_TITLE: 180, C_YEAR: 50, C_NYEAR: 70,
                      C_GENRE: 110, C_NGENRE: 130, C_CONF: 60, C_SRC: 230, C_SEARCH: 200}

    DECK_WIDTHS = {C_CHECK: 60, C_FILE: 240, C_ARTIST: 150, C_TITLE: 220, C_YEAR: 86, C_NYEAR: 104,
                   C_GENRE: 120, C_NGENRE: 150, C_CONF: 66, C_SRC: 132, C_SEARCH: 200}
    DECK_HIDDEN = (C_FILE, C_GENRE, C_SEARCH)

    @property
    def deck(self) -> bool:
        return self.settings.theme != "classic"

    def _apply_default_widths(self):
        for col, w in (self.DECK_WIDTHS if self.deck else self.DEFAULT_WIDTHS).items():
            self.view.setColumnWidth(col, w)

    def _apply_default_columns(self):
        hh = self.view.horizontalHeader()
        for col in range(len(COLS)):
            hh.showSection(col)
            hh.moveSection(hh.visualIndex(col), col)
        if self.deck:
            for col in self.DECK_HIDDEN:
                hh.hideSection(col)
        self._apply_default_widths()

    def _cols_signature(self) -> str:
        return ("deck3|" if self.deck else "check2|") + "|".join(COLS)

    @property
    def _always_shown(self) -> int:
        return C_TITLE if self.deck else C_FILE

    def _restore_header(self):
        st = self.settings.header_state
        if not st or self.settings.header_cols != self._cols_signature():
            return  # no saved layout, or the columns changed in an update
        try:
            from PySide6.QtCore import QByteArray
            self.view.horizontalHeader().restoreState(QByteArray.fromBase64(st.encode()))
        except Exception:  # noqa: BLE001
            pass
        if self.view.horizontalHeader().isSectionHidden(self._always_shown):
            self.view.horizontalHeader().showSection(self._always_shown)

    def _save_header(self):
        try:
            data = self.view.horizontalHeader().saveState().toBase64().data().decode()
        except Exception:  # noqa: BLE001
            return
        self.settings.header_state = data
        self.settings.header_cols = self._cols_signature()

    def _header_menu(self, pos):
        hh = self.view.horizontalHeader()
        m = QMenu(self)
        names = {C_CHECK: "Checkbox"}
        for col in range(len(COLS)):
            if col == self._always_shown:
                continue  # always shown
            a = m.addAction(names.get(col, self.model.labels[col]))
            a.setCheckable(True)
            a.setChecked(not hh.isSectionHidden(col))
            a.toggled.connect(lambda on, c=col: self._set_column_visible(c, on))
        m.addSeparator()
        m.addAction("Show All Columns", self._show_all_columns)
        m.addAction("Reset Column Layout", self._reset_columns)
        m.exec(hh.mapToGlobal(pos))

    def _set_column_visible(self, col: int, visible: bool):
        hh = self.view.horizontalHeader()
        if visible:
            hh.showSection(col)
            if hh.sectionSize(col) < 20:
                widths = self.DECK_WIDTHS if self.deck else self.DEFAULT_WIDTHS
                self.view.setColumnWidth(col, widths.get(col, 100))
        else:
            hh.hideSection(col)
        self._save_header()
        self.settings.save()

    def _show_all_columns(self):
        for col in range(len(COLS)):
            self._set_column_visible(col, True)

    def _reset_columns(self):
        self._apply_default_columns()
        self._save_header()
        self.settings.save()

    def closeEvent(self, e):  # noqa: N802
        self._save_header()
        self.settings.splitter_sizes = self.splitter.sizes()
        if self._busy():
            self.stop()
            if self._thread:
                self._thread.quit()
                self._thread.wait(3000)
        self.settings.save()
        super().closeEvent(e)


def main() -> int:
    if sys.platform == "darwin":
        os.environ.setdefault("QT_MAC_WANTS_LAYER", "1")
    activity.install_crash_logging()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())  # Dock icon when running from source; the .app has its own
    settings = Settings.load()
    if settings.theme != "classic":
        T.apply_palette(app)
    w = MainWindow(settings)
    w.show()
    if settings.check_updates:
        QTimer.singleShot(4000, lambda: w.check_for_updates(quiet=True))
    return app.exec()
