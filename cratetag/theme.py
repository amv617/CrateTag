"""The Deck look: a dark palette, one stylesheet, and the colors used for genres, crates and sources."""

from __future__ import annotations

import zlib

from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtCore import Qt

BG = "#0d0e14"       # window
P1 = "#14151d"       # panels, table
P2 = "#1b1d27"       # inputs, cards
LINE = "#262938"
ROWLINE = "#1d1f2a"
TEXT = "#ecedf3"
MUT = "#8d90a3"
DIM = "#555a6e"
VIO = "#8b7bff"
TEAL = "#2dd4bf"
PINK = "#ff6b9a"
AMBER = "#fbbf24"
BLUE = "#60a5fa"

# Genres people use most get fixed colors; anything else gets a steady color from its name.
GENRE_COLORS = {
    "hip-hop": "#fbbf24", "hip hop": "#fbbf24", "rap": "#fbbf24", "r&b": "#c084fc", "rnb": "#c084fc",
    "reggaeton": "#ff6b9a", "latin": "#f472b6", "bachata": "#f472b6", "salsa": "#fb7185", "merengue": "#fb7185",
    "punta": "#fb923c", "world": "#2dd4bf", "dancehall": "#a3e635", "reggae": "#4ade80", "soca": "#facc15",
    "pop": "#60a5fa", "latin pop": "#f9a8d4", "dance": "#38bdf8", "house": "#22d3ee", "electronic": "#22d3ee",
    "edm": "#22d3ee", "funk / soul": "#f59e0b", "soul": "#f59e0b", "funk": "#f59e0b", "rock": "#f87171",
    "disco": "#e879f9", "freestyle": "#a78bfa", "jazz": "#93c5fd", "country": "#d6a96a", "afrobeats": "#86efac",
}
_SPREAD = ["#8b7bff", "#2dd4bf", "#ff6b9a", "#fbbf24", "#60a5fa", "#a3e635", "#fb923c", "#c084fc", "#22d3ee",
           "#f472b6", "#4ade80", "#facc15"]
SOURCE_COLORS = {"discogs": "#fbbf24", "musicbrainz": "#a99dff", "deezer": "#2dd4bf", "acoustid": "#f472b6",
                 "itunes": "#fb7185",
                 "edited": "#60a5fa"}


def _steady(name: str) -> str:
    return _SPREAD[zlib.crc32(name.strip().lower().encode()) % len(_SPREAD)]


def genre_color(name: str) -> str:
    return GENRE_COLORS.get(name.strip().lower()) or _steady(name)


def crate_color(top_level_name: str) -> str:
    return _steady(top_level_name)


_dot_cache: dict[str, QIcon] = {}


def dot_icon(color: str, size: int = 10) -> QIcon:
    """A small rounded square in a crate's color (cached)."""
    ic = _dot_cache.get(color)
    if ic is None:
        pm = QPixmap(size * 2, size * 2)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(3, 3, size * 2 - 6, size * 2 - 6, 4, 4)
        p.end()
        pm.setDevicePixelRatio(2)
        ic = _dot_cache[color] = QIcon(pm)
    return ic


def apply_palette(app) -> None:
    """Dark palette for every window and dialog (works with the Fusion style)."""
    app.setStyle("Fusion")
    pal = QPalette()
    c = QColor
    pal.setColor(QPalette.Window, c(BG))
    pal.setColor(QPalette.WindowText, c(TEXT))
    pal.setColor(QPalette.Base, c(P1))
    pal.setColor(QPalette.AlternateBase, c("#171821"))
    pal.setColor(QPalette.Text, c(TEXT))
    pal.setColor(QPalette.PlaceholderText, c(DIM))
    pal.setColor(QPalette.Button, c(P2))
    pal.setColor(QPalette.ButtonText, c(TEXT))
    pal.setColor(QPalette.ToolTipBase, c("#1f2130"))
    pal.setColor(QPalette.ToolTipText, c(TEXT))
    pal.setColor(QPalette.Highlight, c("#3b3473"))
    pal.setColor(QPalette.HighlightedText, c("#ffffff"))
    pal.setColor(QPalette.Link, c(VIO))
    pal.setColor(QPalette.BrightText, c(PINK))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, c(DIM))
    app.setPalette(pal)
    app.setStyleSheet(STYLESHEET)


STYLESHEET = f"""
QWidget {{ font-size: 13px; }}
QMainWindow, QDialog {{ background: {BG}; }}
QToolTip {{ background: #1f2130; color: {TEXT}; border: 1px solid #33374a; padding: 5px 7px; border-radius: 6px; }}
QMenuBar {{ background: {BG}; color: {TEXT}; }}
QMenu {{ background: #1b1d27; color: {TEXT}; border: 1px solid #33374a; border-radius: 8px; padding: 5px; }}
QMenu::item {{ padding: 5px 22px 5px 14px; border-radius: 5px; }}
QMenu::item:selected {{ background: {VIO}; color: white; }}
QMenu::item:disabled {{ color: {DIM}; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 4px 6px; }}
QStatusBar {{ background: {BG}; color: {MUT}; border-top: 1px solid {LINE}; }}
QStatusBar QLabel {{ color: {MUT}; }}
QLineEdit, QSpinBox, QPlainTextEdit, QKeySequenceEdit {{ background: {P2}; color: {TEXT}; border: 1px solid {LINE};
    border-radius: 8px; padding: 5px 9px; selection-background-color: {VIO}; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}
QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border-color: {VIO}; }}
QComboBox {{ background: {P1}; color: {TEXT}; border: 1px solid {LINE}; border-radius: 14px; padding: 4px 26px 4px 12px;
    min-height: 18px; }}
QComboBox:hover {{ border-color: #3a3e52; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: #1b1d27; color: {TEXT}; border: 1px solid #33374a;
    selection-background-color: {VIO}; outline: 0; padding: 4px; }}
QPushButton {{ background: {P2}; color: {TEXT}; border: 1px solid {LINE}; border-radius: 8px; padding: 6px 14px; }}
QPushButton:hover {{ border-color: #3a3e52; background: #20222e; }}
QPushButton:default {{ background: {VIO}; border-color: {VIO}; color: white; font-weight: 600; }}
QPushButton:disabled {{ color: {DIM}; }}
QCheckBox, QRadioButton, QLabel {{ color: {TEXT}; }}
QGroupBox {{ border: 1px solid {LINE}; border-radius: 10px; margin-top: 14px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {MUT}; font-weight: 600; }}
QTabWidget::pane {{ border: 1px solid {LINE}; border-radius: 10px; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {MUT}; padding: 7px 14px; border: none; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {VIO}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2e3142; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #3b3f55; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #2e3142; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QHeaderView::section {{ background: {P1}; color: {DIM}; border: none; border-bottom: 1px solid {LINE};
    padding: 0 10px; height: 34px; font-size: 11px; font-weight: 600; }}
QTableView, QTableWidget, QTreeView, QTreeWidget, QListWidget {{ background: {P1}; color: {TEXT}; border: none;
    gridline-color: {ROWLINE}; selection-background-color: #2a2550; outline: 0; }}
QTreeView::item, QTreeWidget::item {{ padding: 4px 2px; border-radius: 6px; }}
QTreeView::item:selected, QTreeWidget::item:selected {{ background: {P2}; color: white; }}
QTreeView::item:hover, QTreeWidget::item:hover {{ background: #191b25; }}
QListWidget::item {{ padding: 7px 10px; border-radius: 7px; }}
QListWidget::item:selected {{ background: {VIO}; color: white; }}
QProgressBar {{ background: #2a2d3c; border: none; border-radius: 3px; max-height: 6px; color: transparent; }}
QProgressBar::chunk {{ background: {TEAL}; border-radius: 3px; }}
QSplitter::handle {{ background: {LINE}; }}
QSplitter::handle:horizontal {{ width: 1px; }}

/* Deck pieces */
#Rail {{ background: #0a0b10; border-right: 1px solid {LINE}; }}
#Rail QToolButton {{ background: transparent; border: none; border-radius: 10px; padding: 9px; color: {DIM}; }}
#Rail QToolButton:hover {{ background: #161823; }}
#Rail QToolButton:checked {{ background: rgba(139,123,255,0.16); }}
#SidePanel {{ background: {P1}; }}
#SideTitle {{ color: {DIM}; font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
#SideNote {{ color: {DIM}; font-size: 11px; }}
#SideBtn {{ background: transparent; border: none; color: {MUT}; padding: 2px 6px; }}
#SideBtn:hover {{ color: {TEXT}; }}
#Title {{ font-size: 21px; font-weight: 700; color: {TEXT}; }}
#Subtitle {{ color: {MUT}; font-size: 13px; }}
#Search {{ background: {P2}; border-radius: 10px; padding: 7px 12px; min-width: 300px; }}
#Stage {{ background: {P1}; border: 1px solid {LINE}; border-radius: 12px; }}
#Stage:hover {{ border-color: #3a3e52; }}
#Stage[on="true"] {{ border-color: {VIO}; background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #1d1b33, stop:1 {P1}); }}
#StageNum {{ font-size: 25px; font-weight: 700; }}
#StageLbl {{ color: {MUT}; font-weight: 500; }}
#StageTag {{ color: {DIM}; font-size: 11px; }}
#OptLbl {{ color: {MUT}; }}
#TextBtn {{ background: transparent; border: none; color: {MUT}; padding: 6px 8px; border-radius: 8px; }}
#TextBtn:hover {{ color: {TEXT}; background: {P2}; }}
#TextBtn:disabled {{ color: #3a3e52; }}
#GhostBtn {{ background: {P2}; border: 1px solid {LINE}; color: {TEXT}; padding: 6px 14px; border-radius: 10px; }}
#GhostBtn:hover {{ border-color: #3a3e52; }}
#GhostBtn:disabled {{ color: {DIM}; }}
#PrimaryBtn {{ background: {VIO}; border: none; color: white; font-weight: 700; padding: 7px 16px; border-radius: 10px; }}
#PrimaryBtn:hover {{ background: #9a8cff; }}
#PrimaryBtn:disabled {{ background: #2a2747; color: #6f6a9c; }}
#TableFrame {{ background: {P1}; border: 1px solid {LINE}; border-radius: 12px; }}
#Floating {{ background: #1f2130; border: 1px solid #33374a; border-radius: 16px; }}
#Floating QLabel {{ color: {TEXT}; }}
#FloatBtn {{ background: transparent; border: none; color: #d6d8e3; padding: 9px 12px; border-radius: 10px; }}
#FloatBtn:hover {{ background: #2a2d3e; }}
#FloatGo {{ background: {VIO}; border: none; color: white; font-weight: 700; padding: 9px 14px; border-radius: 10px; }}
#FloatGo:hover {{ background: #9a8cff; }}
#FloatGo:disabled {{ background: #2a2747; color: #6f6a9c; }}
#FloatSep {{ background: #33374a; }}
#EmptyHint {{ color: {DIM}; font-size: 15px; padding: 40px; }}
#Palette {{ background: #1b1d27; border: 1px solid #33374a; border-radius: 14px; }}
#Palette QLineEdit {{ font-size: 15px; padding: 10px 12px; border-radius: 10px; }}
"""
