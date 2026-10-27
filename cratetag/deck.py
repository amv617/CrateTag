"""Deck look: the painted track rows, stage cards, floating action bar, folder browser and command palette."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QDir, QEvent, QModelIndex, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPainterPath, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileSystemModel, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton, QSizePolicy,
                               QStyle, QStyledItemDelegate, QToolButton, QTreeView, QVBoxLayout, QWidget)

from . import theme as T

# --------------------------------------------------------------------------- icons

_ICONS = {
    "crates": '<path d="M3 8h18v12H3z"/><path d="M5 8V5h14v3"/><path d="M9 12h6"/>',
    "folder": '<path d="M3 6.5h6.5l2 2.2H21V19H3z"/>',
    "files": '<path d="M6 3h8l4 4v14H6z"/><path d="M12 10v7M8.5 13.5h7"/>',
    "undo": '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
    "log": '<path d="M4 6h16M4 12h16M4 18h10"/>',
    "gear": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83'
            'l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4'
            'a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0'
            '-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83'
            'l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51'
            ' 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0'
            ' 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    "search": '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/>',
    "refresh": '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
}


def icon(name: str, color: str = T.DIM, size: int = 20) -> QIcon:
    from PySide6.QtSvg import QSvgRenderer
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
           f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{_ICONS[name]}</svg>')
    r = QSvgRenderer(QByteArray(svg.encode()))
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    r.render(p)
    p.end()
    pm.setDevicePixelRatio(2)
    return QIcon(pm)


def rail_icon(name: str) -> QIcon:
    """Gray normally, violet when the rail button is switched on."""
    ic = QIcon()
    for color, state in ((T.DIM, QIcon.Off), (T.VIO, QIcon.On)):
        ic.addPixmap(icon(name, color, 20).pixmap(20, 20), QIcon.Normal, state)
    return ic


# --------------------------------------------------------------------------- stages

STAGES = [("all", "In list"), ("analyzed", "Analyzed"), ("ready", "Ready to write"), ("written", "Written"),
          ("attention", "Need attention")]
STAGE_COLORS = {"all": "#3a3e52", "analyzed": T.TEAL, "ready": T.VIO, "written": T.TEAL, "attention": T.PINK}


class StageCard(QFrame):
    clicked = Signal(str)

    def __init__(self, key: str, label: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.setObjectName("Stage")
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("on", False)
        self.setMinimumHeight(74)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 10, 14, 12)
        lay.setSpacing(0)
        top = QHBoxLayout()
        self.num = QLabel("0")
        self.num.setObjectName("StageNum")
        self.tag = QLabel("")
        self.tag.setObjectName("StageTag")
        top.addWidget(self.num)
        top.addStretch(1)
        top.addWidget(self.tag, 0, Qt.AlignTop)
        lay.addLayout(top)
        self.lbl = QLabel(label)
        self.lbl.setObjectName("StageLbl")
        lay.addWidget(self.lbl)
        self.fraction = 1.0  # how much of the bottom bar is filled
        if key in ("ready", "attention"):
            self.num.setStyleSheet(f"color: {STAGE_COLORS[key]};")

    def set_value(self, n: int, tag: str = "", fraction: float = 1.0):
        self.num.setText(f"{n:,}")
        self.tag.setText(tag)
        self.fraction = fraction
        self.update()

    def set_on(self, on: bool):
        self.setProperty("on", on)
        self.style().unpolish(self)
        self.style().polish(self)

    def mouseReleaseEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(self.key)
        super().mouseReleaseEvent(e)

    def paintEvent(self, e):  # noqa: N802
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(1, 0, -1, -1)
        y = r.bottom() - 3
        path = QPainterPath()
        path.addRoundedRect(QRectF(r), 12, 12)
        p.setClipPath(path)
        p.fillRect(QRect(r.left(), y, r.width(), 4), QColor("#2a2d3c"))
        p.fillRect(QRect(r.left(), y, int(r.width() * max(0.0, min(1.0, self.fraction))), 4),
                   QColor(STAGE_COLORS[self.key]))
        p.end()


class StageBar(QWidget):
    """Five counts across the top. Click one to show only those tracks; click it again to show all."""

    stage_chosen = Signal(object)  # stage key, or None for everything

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        self.cards: dict[str, StageCard] = {}
        tips = {"all": "Every track in the list. Click to show them all.",
                "analyzed": "Tracks CrateTag has looked up. Click to show only those.",
                "ready": "Tracks with changes waiting to be written. Click to show only those.",
                "written": "Tracks written since they were loaded. Click to show only those.",
                "attention": "No match, errors, low-confidence matches or a missing year or genre. "
                             "Click to show only those."}
        for key, label in STAGES:
            c = StageCard(key, label)
            c.setToolTip(tips[key])
            c.clicked.connect(self._clicked)
            lay.addWidget(c, 1)
            self.cards[key] = c
        self.current: str | None = None
        self.cards["all"].set_on(True)

    def _clicked(self, key: str):
        self.set_current(None if key == "all" or key == self.current else key)
        self.stage_chosen.emit(self.current)

    def set_current(self, key: str | None):
        self.current = key
        for k, c in self.cards.items():
            c.set_on(k == (key or "all"))

    def set_counts(self, counts: dict[str, int]):
        n = counts.get("all", 0)
        a = counts.get("analyzed", 0)
        self.cards["all"].set_value(n)
        self.cards["analyzed"].set_value(a, f"{round(100 * a / n)}%" if n else "", a / n if n else 0)
        self.cards["ready"].set_value(counts.get("ready", 0))
        w = counts.get("written", 0)
        self.cards["written"].set_value(w, "undo available" if w else "")
        self.cards["attention"].set_value(counts.get("attention", 0))


# --------------------------------------------------------------------------- floating bar

class FloatingBar(QFrame):
    """Shows over the bottom of the track list while tracks are highlighted."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Floating")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 7, 7, 7)
        lay.setSpacing(4)
        self.count = QLabel()
        lay.addWidget(self.count)
        sep = QFrame()
        sep.setObjectName("FloatSep")
        sep.setFixedSize(1, 24)
        lay.addSpacing(10)
        lay.addWidget(sep)
        lay.addSpacing(6)
        self._lay = lay
        self.buttons: list[tuple[QPushButton, object, str]] = []
        self.setVisible(False)

    def add(self, action, text: str, primary: bool = False) -> QPushButton:
        b = QPushButton()
        b.setObjectName("FloatGo" if primary else "FloatBtn")
        b.setCursor(Qt.PointingHandCursor)
        b.setFocusPolicy(Qt.NoFocus)
        b.clicked.connect(action.trigger)
        self._lay.addWidget(b)
        self.buttons.append((b, action, text))
        self.refresh_labels()
        return b

    def refresh_labels(self):
        for b, action, text in self.buttons:
            sc = action.shortcut().toString(QKeySequence.NativeText)
            label = action.text() if text == "*" else text
            b.setText(f"{label}   {sc}" if sc else label)
            b.setEnabled(action.isEnabled())

    def place(self):
        p = self.parentWidget()
        if p is None:
            return
        self.adjustSize()
        w, h = self.sizeHint().width(), self.sizeHint().height()
        self.setGeometry((p.width() - w) // 2, p.height() - h - self.BOTTOM, w, h)
        self.raise_()

    BOTTOM = 12  # gap between the bar and the bottom of the track list frame

    def reserved_height(self) -> int:
        """Space the track list leaves free at the bottom so no row sits under the bar."""
        return self.sizeHint().height() + self.BOTTOM * 2


# --------------------------------------------------------------------------- track rows

class DeckDelegate(QStyledItemDelegate):
    """Paints the track list: two-line track, old → new year, genre chips, match ring, source badges."""

    ROW_H = 46

    def __init__(self, view, model, proxy, settings):
        super().__init__(view)
        from . import gui as g
        self.g = g
        self.view, self.model, self.proxy, self.settings = view, model, proxy, settings
        self.c = {k: QColor(v) for k, v in dict(text=T.TEXT, mut=T.MUT, dim=T.DIM, vio=T.VIO, teal=T.TEAL,
                                                   pink=T.PINK, amber=T.AMBER, blue=T.BLUE, line=T.ROWLINE,
                                                   ring="#2a2d3c", p1=T.P1, box="#464b5f").items()}
        self.sel_bg = QColor(139, 123, 255, 30)
        base = QFont(view.font())
        base.setPixelSize(13)
        self.f = base
        self.fb = QFont(base)
        self.fb.setWeight(QFont.DemiBold)
        self.fs = QFont(base)
        self.fs.setPixelSize(12)
        self.fstrike = QFont(base)
        self.fstrike.setStrikeOut(True)
        self.fchip = QFont(base)
        self.fchip.setPixelSize(12)
        self.fchip.setWeight(QFont.DemiBold)
        self.fbadge = QFont(base)
        self.fbadge.setPixelSize(10)
        self.fbadge.setWeight(QFont.Bold)
        self.fbadge.setLetterSpacing(QFont.AbsoluteSpacing, 0.4)
        self.fring = QFont(base)
        self.fring.setPixelSize(10)
        self.fring.setWeight(QFont.Bold)
        self._edit_artist: QModelIndex | None = None  # title cell being edited as the artist line

    # ---- helpers
    def row(self, index):
        return self.model.rows[self.proxy.mapToSource(index).row()]

    def sizeHint(self, option, index):  # noqa: N802
        s = super().sizeHint(option, index)
        return QSize(s.width(), self.ROW_H)

    def _first_visible(self) -> int:
        hh = self.view.horizontalHeader()
        for vi in range(hh.count()):
            li = hh.logicalIndex(vi)
            if not hh.isSectionHidden(li):
                return li
        return -1

    def _text(self, p, rect, text, color, font=None, align=Qt.AlignVCenter | Qt.AlignLeft):
        p.setFont(font or self.f)
        p.setPen(color)
        fm = QFontMetrics(p.font())
        p.drawText(rect, align, fm.elidedText(text, Qt.ElideRight, rect.width()))
        return fm.horizontalAdvance(text)

    def _pill(self, p, x, cy, text, color: QColor, font, pad=9, h=22, radius=6, alpha=36, strike=False, maxw=None):
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(text)
        w = tw + pad * 2
        if maxw is not None and w > maxw:
            w = maxw
        r = QRectF(x, cy - h / 2, w, h)
        bg = QColor(color)
        bg.setAlpha(alpha)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(r, radius, radius)
        p.setFont(font)
        p.setPen(color)
        p.drawText(r.adjusted(pad, 0, -pad + 1, 0), Qt.AlignVCenter | Qt.AlignLeft,
                   fm.elidedText(text, Qt.ElideRight, int(w - pad * 2 + 1)))
        return w

    # ---- painting
    def paint(self, p, option, index):
        g = self.g
        r = self.row(index)
        col = index.column()
        rect = option.rect
        selected = bool(option.state & QStyle.State_Selected)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        if selected:
            p.fillRect(rect, self.sel_bg)
        p.setPen(self.c["line"])
        p.drawLine(rect.bottomLeft(), rect.bottomRight())
        if selected and col == self._first_visible():
            p.fillRect(QRect(rect.left(), rect.top(), 3, rect.height()), self.c["vio"])
        inner = rect.adjusted(12, 0, -10, 0)
        cy = rect.center().y() + 0.5
        mode = self.model.write_mode

        if col == g.C_CHECK:
            s = 15
            box = QRectF(rect.center().x() - s / 2 + 1, cy - s / 2, s, s)
            if r.checked:
                p.setPen(Qt.NoPen)
                p.setBrush(self.c["vio"])
                p.drawRoundedRect(box, 4, 4)
                pen = QPen(QColor("white"), 2)
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                p.setPen(pen)
                path = QPainterPath(QPointF(box.left() + 3.8, box.center().y() + 0.3))
                path.lineTo(box.left() + 6.4, box.bottom() - 4)
                path.lineTo(box.right() - 3.5, box.top() + 4)
                p.setBrush(Qt.NoBrush)
                p.drawPath(path)
            else:
                p.setPen(QPen(self.c["box"], 1.5))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(box.adjusted(0.75, 0.75, -0.75, -0.75), 4, 4)

        elif col == g.C_TITLE:
            title = r.new_title if r.new_title is not None else r.info.title
            artist = r.new_artist if r.new_artist is not None else r.info.artist
            if not title:
                title = r.info.path.stem
            title_color = self.c["blue"] if r.title_changes() else self.c["text"]
            if not self.view.isColumnHidden(g.C_ARTIST):  # Artist has its own column: just the song
                self._text(p, inner, title, title_color, self.fb)
            else:
                top = QRect(inner.left(), rect.top() + 5, inner.width(), rect.height() // 2 - 3)
                bot = QRect(inner.left(), rect.top() + rect.height() // 2 + 1, inner.width(), rect.height() // 2 - 6)
                self._text(p, top, title, title_color, self.fb, Qt.AlignBottom | Qt.AlignLeft)
                self._text(p, bot, artist or "Unknown artist",
                           self.c["blue"] if r.artist_changes() else (self.c["mut"] if artist else self.c["dim"]),
                           self.fs, Qt.AlignTop | Qt.AlignLeft)

        elif col == g.C_NYEAR:
            # Only what analysis found (or you typed). The file's own year is in Year in File.
            new = r.new_year
            if new:
                if mode == "genre":
                    self._text(p, inner, new, self.c["dim"], self.fstrike)
                else:
                    self._text(p, inner, new, self.c["blue"] if r.year_edited else self.c["teal"], self.fb)
            else:
                self._text(p, inner, "—" if r.proposal is not None else "", self.c["dim"])

        elif col == g.C_NGENRE:
            cur, new = r.info.genre.strip(), r.new_genre.strip()
            x = inner.left()
            right = inner.right()
            if new and new != cur:
                if cur:
                    x += self._text(p, QRect(x, rect.top(), max(0, right - x), rect.height()), cur, self.c["dim"],
                                    self.fstrike) + 7
                shown, dim = new, mode == "year"
            else:
                shown, dim = cur or new, False
            if not shown:
                self._text(p, inner, "—", self.c["dim"])
            else:
                sep = (self.settings.genre_separator or ";").strip() or ";"
                for part in [s.strip() for s in shown.split(sep) if s.strip()]:
                    if x >= right - 20:
                        break
                    color = QColor(T.genre_color(part))
                    if r.genre_edited and new and new != cur:
                        color = self.c["blue"]
                    if dim:
                        color.setAlpha(110)
                    x += self._pill(p, x, cy, part, color, self.fchip, maxw=right - x) + 6

        elif col == g.C_CONF:
            pr = r.proposal
            if pr and pr.confidence:
                v = pr.confidence
                color = self.c["teal"] if v >= 85 else (self.c["amber"] if v >= 70 else self.c["pink"])
                d = 28
                ring = QRectF(inner.left(), cy - d / 2, d, d)
                p.setPen(Qt.NoPen)
                p.setBrush(self.c["ring"])
                p.drawEllipse(ring)
                p.setBrush(color)
                p.drawPie(ring, 90 * 16, -int(360 * 16 * min(v, 100) / 100))
                p.setBrush(self.c["p1"] if not selected else QColor("#1d1b30"))
                p.drawEllipse(ring.adjusted(3, 3, -3, -3))
                p.setFont(self.fring)
                p.setPen(self.c["text"])
                p.drawText(ring, Qt.AlignCenter, str(v))
            else:
                self._text(p, inner, "—", self.c["dim"])

        elif col == g.C_SRC:
            names = []
            if r.year_edited or r.genre_edited or r.artist_changes() or r.title_changes():
                names.append("Edited")
            src = r.proposal.source if r.proposal else ""
            for key, label in (("discogs", "Discogs"), ("musicbrainz", "MusicBrainz"), ("deezer", "Deezer"),
                               ("itunes", "iTunes"), ("acoustid", "AcoustID")):
                if key in src.lower() and label not in names:
                    names.append(label)
            if not names:
                self._text(p, inner, "—", self.c["dim"])
            x = inner.left()
            for n in names:
                if x >= inner.right() - 20:
                    break
                color = QColor(T.SOURCE_COLORS[n.lower()])
                x += self._pill(p, x, cy, n.upper(), color, self.fbadge, pad=7, h=20, radius=5, alpha=34,
                                maxw=inner.right() - x) + 5

        elif col == g.C_STATUS:
            st = r.stage(mode)
            color = {"ready": self.c["vio"], "attention": self.c["pink"], "analyzed": self.c["teal"],
                     "written": self.c["dim"]}.get(st, self.c["dim"])
            if st == "ready" and (r.year_edited or r.genre_edited or r.artist_changes() or r.title_changes()):
                color = self.c["blue"]
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawEllipse(QRectF(inner.left(), cy - 4, 8, 8))
            self._text(p, inner.adjusted(16, 0, 0, 0), r.status,
                       self.c["pink"] if st == "attention" and r.status.startswith(("Write failed", "Error"))
                       else self.c["mut"])

        else:  # File, Artist, Year/Genre in file, Searched As: plain text
            text = index.data() or ""
            color = self.c["mut"] if col in (g.C_FILE, g.C_SEARCH) else self.c["text"]
            if (col == g.C_ARTIST and r.artist_changes()) or (col == g.C_YEAR and r.pending_year()) or \
                    (col == g.C_GENRE and r.pending_genre()):
                color = self.c["blue"]
            self._text(p, inner, str(text) if text else "—", color if text else self.c["dim"])
        p.restore()

    # ---- checkbox clicks and editing
    def editorEvent(self, event, model, option, index):  # noqa: N802
        if index.column() == self.g.C_CHECK and event.type() in (QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick):
            if event.button() == Qt.LeftButton:
                if event.type() == QEvent.MouseButtonRelease:
                    cur = index.data(Qt.CheckStateRole)
                    model.setData(index, Qt.Unchecked if cur == Qt.Checked else Qt.Checked, Qt.CheckStateRole)
                return True
        return super().editorEvent(event, model, option, index)

    def start_artist_edit(self, index):
        """The next editor opened on this title cell edits the artist line instead
        (only when the artist is shown under the title, i.e. the Artist column is hidden)."""
        if self.view.isColumnHidden(self.g.C_ARTIST):
            self._edit_artist = QModelIndex(index)

    def createEditor(self, parent, option, index):  # noqa: N802
        ed = QLineEdit(parent)
        ed.setFrame(False)
        ed.setStyleSheet(f"background:{T.P2}; border:1px solid {T.VIO}; border-radius:6px; padding:2px 6px;")
        return ed

    def _editing_artist(self, index) -> bool:
        return self._edit_artist is not None and self._edit_artist.isValid() and self._edit_artist == index

    def setEditorData(self, editor, index):  # noqa: N802
        r = self.row(index)
        if self._editing_artist(index):
            editor.setText(r.new_artist if r.new_artist is not None else r.info.artist)
        elif index.column() == self.g.C_NYEAR:
            editor.setText(r.new_year or r.info.year)
        elif index.column() == self.g.C_NGENRE:
            editor.setText(r.new_genre or r.info.genre)
        else:
            editor.setText(str(index.data(Qt.EditRole) or ""))
        editor.selectAll()

    def setModelData(self, editor, model, index):  # noqa: N802
        text = editor.text()
        if self._editing_artist(index):
            self._edit_artist = None
            model.setData(index.sibling(index.row(), self.g.C_ARTIST), text)
            return
        r = self.row(index)
        if index.column() == self.g.C_NYEAR and not r.new_year and text.strip() == r.info.year:
            return  # opened and closed without a change
        if index.column() == self.g.C_NGENRE and not r.new_genre and text.strip() == r.info.genre.strip():
            return
        model.setData(index, text)

    def updateEditorGeometry(self, editor, option, index):  # noqa: N802
        rect = option.rect.adjusted(6, 7, -6, -7)
        if index.column() == self.g.C_TITLE:
            half = option.rect.height() // 2
            if self._editing_artist(index):
                rect = QRect(option.rect.left() + 6, option.rect.top() + half, option.rect.width() - 12, half - 3)
            else:
                rect = QRect(option.rect.left() + 6, option.rect.top() + 3, option.rect.width() - 12, half - 2)
        editor.setGeometry(rect)

    def destroyEditor(self, editor, index):  # noqa: N802
        if self._editing_artist(index):
            self._edit_artist = None
        super().destroyEditor(editor, index)


# --------------------------------------------------------------------------- folder browser

class FolderPanel(QWidget):
    """Browse folders on this Mac and your drives. Double-click a folder to load its MP3s."""

    folder_chosen = Signal(str, bool)  # path, add to the current list

    def __init__(self, start: str = "", parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 16, 10, 10)
        lay.setSpacing(8)
        head = QHBoxLayout()
        t = QLabel("FOLDERS")
        t.setObjectName("SideTitle")
        head.addWidget(t)
        head.addStretch(1)
        lay.addLayout(head)
        self.place = QComboBox()
        self.place.setToolTip("Where to browse")
        self.place.currentIndexChanged.connect(self._place_picked)
        lay.addWidget(self.place)
        self.fs = QFileSystemModel(self)
        self.fs.setFilter(QDir.AllDirs | QDir.NoDotAndDotDot | QDir.Drives)
        self.fs.setReadOnly(True)
        self.fs.setRootPath("")
        self.tree = QTreeView()
        self.tree.setModel(self.fs)
        self.tree.setHeaderHidden(True)
        for c in range(1, 4):
            self.tree.hideColumn(c)
        self.tree.setAnimated(False)
        self.tree.setIndentation(14)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.doubleClicked.connect(lambda idx: self._load(idx, False))
        self.tree.activated.connect(lambda idx: None)
        lay.addWidget(self.tree, 1)
        self.load_btn = QPushButton("Load This Folder")
        self.load_btn.setObjectName("GhostBtn")
        self.load_btn.clicked.connect(lambda: self._load(self.tree.currentIndex(), False))
        lay.addWidget(self.load_btn)
        note = QLabel("Double-click a folder to load its MP3s, including its subfolders.")
        note.setObjectName("SideNote")
        note.setWordWrap(True)
        lay.addWidget(note)
        self._fill_places(start)

    def _fill_places(self, start: str):
        home = Path.home()
        places = []
        music = home / "Music"
        if music.is_dir():
            places.append(("Music", str(music)))
        places.append(("Home", str(home)))
        vol = Path("/Volumes")
        if vol.is_dir():
            try:
                for d in sorted(vol.iterdir()):
                    if d.is_dir() and not d.name.startswith("."):
                        places.append((d.name, str(d)))
            except OSError:
                pass
        self.place.blockSignals(True)
        self.place.clear()
        for label, path in places:
            self.place.addItem(label, path)
        self.place.blockSignals(False)
        idx = 0
        if start:
            for i, (_l, path) in enumerate(places):
                if start.startswith(path) and len(path) > len(places[idx][1]):
                    idx = i
        self.place.setCurrentIndex(idx)
        self._place_picked(idx)
        if start and Path(start).is_dir():
            si = self.fs.index(start)
            if si.isValid():
                self.tree.setCurrentIndex(si)
                self.tree.scrollTo(si)

    def _place_picked(self, _i):
        path = self.place.currentData()
        if path:
            self.tree.setRootIndex(self.fs.index(path))

    def current_path(self) -> str:
        idx = self.tree.currentIndex()
        return self.fs.filePath(idx) if idx.isValid() else (self.place.currentData() or "")

    def _load(self, idx, append: bool):
        path = self.fs.filePath(idx) if idx.isValid() else self.place.currentData()
        if path:
            self.folder_chosen.emit(path, append)

    def _menu(self, pos):
        idx = self.tree.indexAt(pos)
        if not idx.isValid():
            return
        path = self.fs.filePath(idx)
        m = QMenu(self)
        m.addAction("Load Folder", lambda: self.folder_chosen.emit(path, False))
        m.addAction("Add Folder to List", lambda: self.folder_chosen.emit(path, True))
        m.addSeparator()
        from .gui import _reveal_path
        m.addAction("Reveal in Finder", lambda: _reveal_path(Path(path)))
        m.exec(self.tree.viewport().mapToGlobal(pos))


# --------------------------------------------------------------------------- command palette

class CommandPalette(QDialog):
    """⌘K: type part of a command's name and press Return to run it."""

    def __init__(self, commands: list[tuple[str, str, object]], parent=None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        box = QFrame()
        box.setObjectName("Palette")
        outer.addWidget(box)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(10, 10, 10, 10)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Type a command…")
        self.edit.textChanged.connect(self._filter)
        self.edit.installEventFilter(self)
        lay.addWidget(self.edit)
        self.list = QListWidget()
        self.list.itemActivated.connect(self._run)
        self.list.setMinimumHeight(320)
        lay.addWidget(self.list)
        self.commands = commands
        self.chosen = None
        self.resize(560, 420)
        self._filter("")

    def _filter(self, text: str):
        t = text.strip().lower()
        self.list.clear()
        for label, keys, action in self.commands:
            if t and not all(w in label.lower() for w in t.split()):
                continue
            it = QListWidgetItem(f"{label}" + (f"      {keys}" if keys else ""))
            it.setData(Qt.UserRole, action)
            if not action.isEnabled():
                it.setFlags(Qt.NoItemFlags)
            self.list.addItem(it)
        for i in range(self.list.count()):
            if self.list.item(i).flags() & Qt.ItemIsEnabled:
                self.list.setCurrentRow(i)
                break

    def eventFilter(self, obj, e):  # noqa: N802
        if obj is self.edit and e.type() == QEvent.KeyPress:
            if e.key() in (Qt.Key_Down, Qt.Key_Up):
                step = 1 if e.key() == Qt.Key_Down else -1
                row = self.list.currentRow()
                n = self.list.count()
                for _ in range(n):
                    row = (row + step) % n if n else 0
                    if n and self.list.item(row).flags() & Qt.ItemIsEnabled:
                        self.list.setCurrentRow(row)
                        break
                return True
            if e.key() in (Qt.Key_Return, Qt.Key_Enter):
                it = self.list.currentItem()
                if it:
                    self._run(it)
                return True
        return super().eventFilter(obj, e)

    def _run(self, item):
        if not item.flags() & Qt.ItemIsEnabled:
            return
        self.chosen = item.data(Qt.UserRole)
        self.accept()
