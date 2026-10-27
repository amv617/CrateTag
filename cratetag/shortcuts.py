"""Keyboard shortcuts: every command that can have one, its default, and the user's overrides.

Shortcuts are stored in Qt's portable text form ("Ctrl+R"). On macOS Qt shows "Ctrl" as the
Command key (⌘) and "Meta" as the Control key, so "Ctrl+R" is ⌘R.
The user's changes live in Settings.shortcuts as {command id: "Ctrl+R"}; "" means no shortcut.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    id: str
    label: str
    group: str
    default: str = ""


COMMANDS: list[Command] = [
    # File
    Command("add_folder", "Add Folder…", "File", "Ctrl+O"),
    Command("add_files", "Add Files…", "File", "Ctrl+Shift+O"),
    Command("clear", "Clear the List", "File"),
    Command("write", "Write (highlighted, else checked)", "File", "Ctrl+S"),
    Command("write_checked", "Write Checked Tracks (ignore highlight)", "File"),
    Command("settings", "Settings…", "File", "Ctrl+,"),
    # Edit
    Command("highlight_all", "Highlight All Tracks", "Edit", "Ctrl+A"),
    Command("check_all", "Check All Tracks", "Edit"),
    Command("check_none", "Uncheck All Tracks", "Edit"),
    Command("check_selected", "Check Highlighted Tracks", "Edit"),
    Command("uncheck_selected", "Uncheck Highlighted Tracks", "Edit"),
    Command("search_tracks", "Search Tracks", "Edit", "Ctrl+F"),
    Command("search_crates", "Search Crates", "Edit", "Ctrl+Alt+F"),
    Command("remove", "Remove Highlighted from List (files stay)", "Edit", "Ctrl+Backspace"),
    # Tracks
    Command("analyze", "Analyze", "Tracks", "Ctrl+L"),
    Command("stop", "Stop", "Tracks", "Esc"),
    Command("reveal", "Reveal in Finder", "Tracks", "Ctrl+R"),
    Command("show_matches", "Show Matches…", "Tracks"),
    Command("edit_search", "Edit Search…", "Tracks"),
    Command("set_artist", "Set Artist…", "Tracks"),
    Command("set_title", "Set Title…", "Tracks"),
    Command("set_year", "Set Year…", "Tracks", "Ctrl+Y"),
    Command("set_genre", "Set Genre…", "Tracks", "Ctrl+G"),
    Command("undo_write", "Undo a Previous Write…", "Tracks"),
    # View / Help
    Command("toggle_crates", "Show or Hide Crates", "View", "Ctrl+Shift+C"),
    Command("browse_folders", "Browse Folders", "View", "Ctrl+Shift+F"),
    Command("palette", "Command Palette…", "View", "Ctrl+K"),
    Command("activity_log", "Activity Log…", "Help", "Ctrl+Alt+L"),
    Command("check_updates", "Check for Updates…", "Help"),
    Command("update", "Update from Downloads…", "Help"),
]

BY_ID = {c.id: c for c in COMMANDS}


def effective(overrides: dict | None) -> dict[str, str]:
    """The shortcut for every command: the user's choice if they made one, else the default."""
    overrides = overrides or {}
    out = {}
    for c in COMMANDS:
        v = overrides.get(c.id, c.default)
        out[c.id] = v if isinstance(v, str) else c.default
    return out


def overrides_from(chosen: dict[str, str]) -> dict[str, str]:
    """Keep only the shortcuts that differ from the defaults (so new defaults reach old settings)."""
    return {cid: v for cid, v in chosen.items() if cid in BY_ID and v != BY_ID[cid].default}


def conflicts(chosen: dict[str, str]) -> dict[str, list[str]]:
    """Shortcuts used by more than one command: {shortcut: [command ids]}."""
    seen: dict[str, list[str]] = {}
    for cid, v in chosen.items():
        if v:
            seen.setdefault(v, []).append(cid)
    return {k: ids for k, ids in seen.items() if len(ids) > 1}
