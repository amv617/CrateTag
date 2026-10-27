"""End-to-end smoke test of the window: scan, look up (mocked), write. Runs headless."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import cratetag.workers as workers  # noqa: E402
from cratetag.config import Settings  # noqa: E402
from cratetag.lookup import Engine  # noqa: E402
from cratetag.tags import read_track  # noqa: E402

from test_cratetag import ROUTES, FakeHttp, add_serato_style_tags, make_mp3  # noqa: E402


def lbl(item) -> str:
    """A crate's label as "Name  (count)" in either look (Deck puts the count in its own column)."""
    if item.columnCount() > 1 and item.text(1):
        return f"{item.text(0)}  ({item.text(1)})"
    return item.text(0)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_idle(win, timeout=10000):
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: (not win._busy()) and loop.quit())
    timer.start()
    QTimer.singleShot(timeout, loop.quit)
    loop.exec()
    timer.stop()
    assert not win._busy(), "background job did not finish"


def test_full_flow(app, tmp_path, monkeypatch):
    from cratetag.gui import MainWindow

    lib = tmp_path / "lib"
    lib.mkdir()
    a = make_mp3(lib / "Aventura - Obsesion.mp3")
    add_serato_style_tags(a, 3)
    b = make_mp3(lib / "Track 07.mp3")

    monkeypatch.setattr(workers, "Engine", lambda s: Engine(s, http=FakeHttp(ROUTES)))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(Settings, "save", lambda self: None)

    s = Settings(discogs_token="t", backup_dir=str(tmp_path / "bk"), backup_mode="copies")
    win = MainWindow(s)
    win.show()

    win.scan([str(lib)])
    wait_idle(win)
    assert len(win.model.rows) == 2

    win.lookup()
    wait_idle(win)
    rows = {r.info.path.name: r for r in win.model.rows}
    good = rows["Aventura - Obsesion.mp3"]
    assert good.new_year == "2002" and good.new_genre == "Bachata"
    assert good.checked and good.status == "Ready to write"
    assert rows["Track 07.mp3"].status == "Needs a title"
    assert not rows["Track 07.mp3"].checked

    win.write_checked()
    wait_idle(win)
    info = read_track(a)
    assert (info.year, info.genre) == ("2002", "Bachata")
    assert good.status == "Written" or win.model.rows[win.model.row_for(str(a))].status == "Written"
    assert read_track(b).year == ""
    backups = list((tmp_path / "bk").rglob("*.mp3"))
    assert len(backups) == 1 and read_track(backups[0]).year == "1999"

    # Manual edit path: type a year into the untitled row and write it
    i = win.model.row_for(str(b))
    from cratetag.gui import C_NGENRE, C_NYEAR
    assert win.model.setData(win.model.index(i, C_NYEAR), "1995")
    assert not win.model.setData(win.model.index(i, C_NYEAR), "95")  # rejected
    win.model.setData(win.model.index(i, C_NGENRE), "Punta")
    assert win.model.rows[i].checked
    win.write_checked()
    wait_idle(win)
    assert (read_track(b).year, read_track(b).genre) == ("1995", "Punta")
    win.close()


def test_select_all_none_and_lookup_uses_checked(app, tmp_path, monkeypatch):
    from cratetag.gui import MainWindow

    lib = tmp_path / "lib2"
    lib.mkdir()
    for n in ["Aventura - Obsesion.mp3", "Aventura - Obsesion (Clean).mp3", "Track 07.mp3"]:
        make_mp3(lib / n)
    monkeypatch.setattr(workers, "Engine", lambda s: Engine(s, http=FakeHttp(ROUTES)))
    monkeypatch.setattr(Settings, "save", lambda self: None)
    win = MainWindow(Settings(discogs_token="t"))
    win.scan([str(lib)])
    wait_idle(win)

    win.a_check_all.trigger()
    assert all(r.checked for r in win.model.rows)
    assert win.a_lookup.text() == "Analyze (3)"
    win.a_check_none.trigger()
    assert not any(r.checked for r in win.model.rows)
    assert not win.view.selectionModel().hasSelection()

    # Highlight only one row: Look Up should touch only that one, even with nothing checked
    target = win.model.row_for(str(lib / "Aventura - Obsesion.mp3"))
    win.view.selectRow(win.proxy.mapFromSource(win.model.index(target, 0)).row())
    assert win.a_lookup.text() == "Analyze (1)"
    win.lookup()
    wait_idle(win)
    looked = [r.info.path.name for r in win.model.rows if r.proposal is not None]
    assert looked == ["Aventura - Obsesion.mp3"]
    from cratetag.gui import C_SEARCH
    assert "Obsesion" in win.model.data(win.model.index(target, C_SEARCH))
    win.close()


def test_write_mode_and_source_switch(app, tmp_path, monkeypatch):
    from cratetag.gui import MainWindow

    lib = tmp_path / "lib3"
    lib.mkdir()
    a = make_mp3(lib / "Aventura - Obsesion.mp3")
    add_serato_style_tags(a, 4)  # year 1999, genre Latin
    monkeypatch.setattr(workers, "Engine", lambda s: Engine(s, http=FakeHttp(ROUTES)))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(Settings, "save", lambda self: None)
    win = MainWindow(Settings(discogs_token="t", backup_mode="none"))
    win.scan([str(lib)])
    wait_idle(win)
    win.lookup()
    wait_idle(win)
    row = win.model.rows[0]
    assert (row.new_year, row.new_genre) == ("2002", "Bachata")

    # Switching genre source re-picks instantly from stored matches
    win.genre_src_combo.setCurrentIndex(win.genre_src_combo.findData("discogs_genre"))
    assert row.new_genre == "Latin"
    assert row.new_year == "2002"
    # Year-only mode: genre now matches the file, row still checked for its year change
    win.write_combo.setCurrentIndex(win.write_combo.findData("year"))
    assert row.checked
    win.genre_src_combo.setCurrentIndex(win.genre_src_combo.findData("auto"))
    win.write_checked()
    wait_idle(win)
    info = read_track(a)
    assert info.year == "2002" and info.genre == "Latin"  # genre untouched in year-only mode

    # A hand-typed year survives a source change
    win.lookup()
    wait_idle(win)
    from cratetag.gui import C_NYEAR
    win.model.setData(win.model.index(0, C_NYEAR), "2003")
    win.year_src_combo.setCurrentIndex(win.year_src_combo.findData("musicbrainz"))
    assert win.model.rows[0].new_year == "2003"
    win.close()


def test_status_says_no_genre_instead_of_already_correct(app, tmp_path, monkeypatch):
    from cratetag.gui import MainWindow
    from cratetag.lookup import Candidate, Proposal
    lib = tmp_path / "lib5"
    lib.mkdir()
    a = make_mp3(lib / "Dolly Babe - Pink Blush Remix.mp3")
    from cratetag.tags import write_year_genre
    write_year_genre(a, year="2026")
    monkeypatch.setattr(Settings, "save", lambda self: None)
    win = MainWindow(Settings(genre_source="auto"))
    win.scan([str(lib)])
    wait_idle(win)
    prop = Proposal(year="2026", confidence=100,
                    candidates=[Candidate("MusicBrainz", "Dolly Babe", "Pink Blush Remix", original_year="2026", score=1.0)])
    win.model.apply_proposal(str(a), prop, win.settings)
    assert win.model.rows[0].status == "No genre found"
    win.write_combo.setCurrentIndex(win.write_combo.findData("year"))
    assert win.model.rows[0].status == "Already correct"
    win.close()


def test_bulk_edit_like_serato(app, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QInputDialog
    from cratetag.gui import C_NGENRE, C_NYEAR, MainWindow

    lib = tmp_path / "lib6"
    lib.mkdir()
    files = [make_mp3(lib / f"Artist - Song {i}.mp3") for i in range(4)]
    monkeypatch.setattr(Settings, "save", lambda self: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    win = MainWindow(Settings(backup_mode="none", write_mode="genre"))
    win.resize(1200, 400)
    win.show()
    win.scan([str(lib)])
    wait_idle(win)

    # Highlight 3 of 4 rows, click a highlighted New Year cell: highlight survives and editor opens
    for r in range(3):
        from PySide6.QtCore import QItemSelectionModel as SM
        win.view.selectionModel().select(win.proxy.index(r, 0), SM.Select | SM.Rows)
    idx = win.proxy.index(1, C_NYEAR)
    QTest.mouseClick(win.view.viewport(), Qt.LeftButton, Qt.NoModifier, win.view.visualRect(idx).center())
    assert len(win._selected_source_rows()) == 3

    # Commit 2099 on that cell: confirmation applies it to all 3 (deferred), and switches write mode
    win.view.closePersistentEditor(idx)
    win.proxy.setData(idx, "2099")
    QTest.qWait(50)
    years = sorted(r.new_year for r in win.model.rows)
    assert years.count("2099") == 3 and "" in years
    assert win.model.write_mode == "both"
    assert sum(r.checked for r in win.model.rows) == 3

    # Set Genre… for the highlighted rows
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("Freestyle", True))
    win.bulk_set_dialog("genre")
    assert sum(r.new_genre == "Freestyle" for r in win.model.rows) == 3

    win.write_checked()
    wait_idle(win)
    written = [read_track(f) for f in files]
    assert sum(t.year == "2099" and t.genre == "Freestyle" for t in written) == 3
    assert sum(t.year == "" for t in written) == 1
    win.close()


def test_undo_log_and_restore(app, tmp_path, monkeypatch):
    from cratetag.gui import MainWindow
    from cratetag.tags import read_raw_year_genre, write_year_genre
    from PySide6.QtWidgets import QFileDialog

    lib = tmp_path / "lib7"
    lib.mkdir()
    a = make_mp3(lib / "Aventura - Obsesion.mp3")
    add_serato_style_tags(a, 4)
    write_year_genre(a, year=["2002-04-30"], genre=["Latin", "Bachata"])  # full date + two genres
    b = make_mp3(lib / "Artist - Song.mp3")  # no tags at all
    monkeypatch.setattr(Settings, "save", lambda self: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: 0)
    bk = tmp_path / "bk"
    win = MainWindow(Settings(backup_dir=str(bk), backup_mode="undo"))
    win.scan([str(lib)])
    wait_idle(win)
    win.select_all()
    win.model.manual_set(list(range(2)), "year", "2099")
    win.model.manual_set(list(range(2)), "genre", "Freestyle")
    win.write_checked()
    wait_idle(win)
    assert read_track(a).year == "2099" and read_track(b).genre == "Freestyle"
    logs = list(bk.glob("CrateTag undo *.csv"))
    assert len(logs) == 1 and logs[0].stat().st_size < 2000
    assert not any(p.suffix == ".mp3" for p in bk.rglob("*"))  # no file copies

    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(logs[0]), ""))
    win.undo_previous_write()
    wait_idle(win)
    assert read_raw_year_genre(a) == (["2002-04-30"], ["Latin", "Bachata"])
    assert read_raw_year_genre(b) == ([], [])
    # Serato data survived both writes
    from mutagen.id3 import ID3
    assert ID3(a)["GEOB:Serato Markers2"].data.startswith(b"\x01\x01CUE")
    assert {r.status for r in win.model.rows} == {"Restored"}
    win.close()


def test_copies_mode_falls_back_when_disk_is_full(app, tmp_path, monkeypatch):
    import shutil as _sh
    from collections import namedtuple
    from cratetag.gui import MainWindow
    lib = tmp_path / "lib8"
    lib.mkdir()
    a = make_mp3(lib / "Artist - Song.mp3")
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(_sh, "disk_usage", lambda p: Usage(10, 10, 1))
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(Settings, "save", lambda self: None)
    bk = tmp_path / "bk"
    win = MainWindow(Settings(backup_dir=str(bk), backup_mode="copies"))
    win.scan([str(lib)])
    wait_idle(win)
    win.select_all()
    win.model.manual_set([0], "year", "2001")
    win.write_checked()
    wait_idle(win)
    assert "only" in asked[0] and "free" in asked[0]
    assert read_track(a).year == "2001"
    assert list(bk.glob("CrateTag undo *.csv")) and not list(bk.rglob("*.mp3"))
    win.close()


def test_edit_artist_title_year_genre_in_place(app, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtCore import QItemSelectionModel as SM
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QFileDialog
    from cratetag.gui import C_ARTIST, C_GENRE, C_TITLE, C_YEAR, MainWindow
    from cratetag.tags import read_raw_fields

    lib = tmp_path / "lib9"
    lib.mkdir()
    files = [make_mp3(lib / f"Track {i}.mp3") for i in range(3)]
    add_serato_style_tags(files[0], 3)  # artist Aventura, title Obsesion, 1999, Latin
    monkeypatch.setattr(Settings, "save", lambda self: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: 0)
    bk = tmp_path / "bk"
    win = MainWindow(Settings(backup_dir=str(bk), backup_mode="undo", write_mode="year"))
    win.resize(1400, 400)
    win.show()
    win.scan([str(lib)])
    wait_idle(win)
    m = win.model
    i0 = m.row_for(str(files[0]))

    # Single-row edits straight in the Artist / Title / Year / Genre columns
    assert m.flags(m.index(i0, C_ARTIST)) & Qt.ItemIsEditable
    m.setData(m.index(i0, C_ARTIST), "Aventura feat. Judy Santos")
    m.setData(m.index(i0, C_TITLE), "Obsesión")
    m.setData(m.index(i0, C_YEAR), "2002")
    assert m.data(m.index(i0, C_ARTIST)) == "Aventura feat. Judy Santos"
    assert m.data(m.index(i0, C_YEAR)) == "2002"            # shows the pending value
    assert m.data(m.index(i0, C_YEAR), Qt.FontRole).bold()
    assert "1999" in m.data(m.index(i0, C_YEAR), Qt.ToolTipRole)
    assert m.rows[i0].checked

    # Bulk: highlight all three, click a highlighted Genre cell, commit -> applies to all
    for r in range(3):
        win.view.selectionModel().select(win.proxy.index(r, 0), SM.Select | SM.Rows)
    gidx = win.proxy.index(1, C_GENRE)
    QTest.mouseClick(win.view.viewport(), Qt.LeftButton, Qt.NoModifier, win.view.visualRect(gidx).center())
    assert len(win._selected_source_rows()) == 3
    win.proxy.setData(gidx, "Bachata")
    QTest.qWait(50)
    assert all(r.new_genre == "Bachata" for r in m.rows)
    assert m.write_mode == "both"  # switched from Year only so the genre edit is saved

    win.write_checked()
    wait_idle(win)
    raw = read_raw_fields(files[0])
    assert raw == {"year": ["2002"], "genre": ["Bachata"], "artist": ["Aventura feat. Judy Santos"],
                   "title": ["Obsesión"]}
    assert read_track(files[2]).genre == "Bachata"
    from mutagen.id3 import ID3
    assert "GEOB:Serato Markers2" in ID3(files[0])
    assert m.rows[i0].new_artist is None and not m.rows[i0].checked

    # Undo puts artist and title back too
    log = next(bk.glob("CrateTag undo *.csv"))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(log), ""))
    win.undo_previous_write()
    wait_idle(win)
    assert read_raw_fields(files[0]) == {"year": ["1999"], "genre": ["Latin"], "artist": ["Aventura"],
                                         "title": ["Obsesion"]}
    assert read_raw_fields(files[2])["genre"] == []
    win.close()


def test_hide_columns_and_layout_is_remembered(app, tmp_path, monkeypatch):
    from cratetag.gui import C_NGENRE, C_NYEAR, C_SEARCH, C_FILE, MainWindow
    saved = {}
    monkeypatch.setattr(Settings, "save", lambda self: saved.update(vars(self)))
    s = Settings()
    win = MainWindow(s)
    win._set_column_visible(C_NYEAR, False)
    win._set_column_visible(C_SEARCH, False)
    win.view.horizontalHeader().moveSection(win.view.horizontalHeader().visualIndex(C_NGENRE), 2)
    win.close()
    assert s.header_state

    win2 = MainWindow(s)  # same settings, new window
    hh = win2.view.horizontalHeader()
    assert hh.isSectionHidden(C_NYEAR) and hh.isSectionHidden(C_SEARCH)
    assert hh.visualIndex(C_NGENRE) == 2
    assert not hh.isSectionHidden(win2._always_shown)  # Track in Deck, File in Classic
    win2._reset_columns()
    assert not hh.isSectionHidden(C_NYEAR) and hh.visualIndex(C_NGENRE) == C_NGENRE
    win2.close()


def test_crates_sidebar_load_edit_write(app, tmp_path, monkeypatch):
    from functools import partial
    from cratetag.gui import C_GENRE, MainWindow
    from cratetag.serato import find_libraries
    from test_cratetag import _fake_serato
    home, vols, (a, b, c) = _fake_serato(tmp_path)
    monkeypatch.setattr(Settings, "save", lambda self: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *x, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    win = MainWindow(Settings(backup_mode="none"))
    win.crates._find = partial(find_libraries, home, vols)
    win.crates.refresh()
    tree = win.crates.tree
    # Combined view: crates from both drives in one tree, like Serato
    tops = [lbl(tree.topLevelItem(i)) for i in range(tree.topLevelItemCount())]
    assert tops == ["#CASE CRATES#  (1)", "Internal  (1)", "test  (1)"]

    item = win.crates.item_for("#CASE CRATES#%%092626 WEDDING")
    assert lbl(item) == "092626 WEDDING  (4)"
    win.crates._clicked(item)
    wait_idle(win)
    names = [r.info.path.name for r in win.model.rows]
    assert names == ["Whitney Houston - Its Not Right.mp3", "Gone.mp3", "Song.m4a", "Aventura - Obsesion.mp3"]
    status = {r.info.path.name: r.status for r in win.model.rows}
    assert status["Gone.mp3"].startswith("File not found")
    assert "MP3s only" in status["Song.m4a"]
    assert "092626 WEDDING" in win.windowTitle()

    # Edit and write a track that lives on the "external drive"
    i = win.model.row_for(str(b))
    win.model.setData(win.model.index(i, C_GENRE), "R&B")
    win.write_checked()
    wait_idle(win)
    assert read_track(b).genre == "R&B"

    # Unsaved edit -> loading another crate asks first (answered No: stays put)
    win.model.setData(win.model.index(i, C_GENRE), "Pop")
    monkeypatch.setattr(QMessageBox, "question", lambda *x, **k: QMessageBox.No)
    win.crates._clicked(win.crates.item_for("test"))
    assert len(win.model.rows) == 4
    # Add a crate to the current list instead of replacing it
    win.load_crate(win.crates.item_for("test").data(0, 256), append=True)
    wait_idle(win)
    assert len(win.model.rows) == 5
    win.close()


def test_crate_nesting_drive_filter_and_search(app, tmp_path, monkeypatch):
    """Your MHS06 case: parent crate stored on the Mac, sub-crates on the external drive."""
    import os
    from functools import partial
    from cratetag.gui import MainWindow
    from cratetag.serato import find_libraries, write_crate
    home, vols = tmp_path / "home", tmp_path / "Volumes"
    (home / "Music").mkdir(parents=True)
    ext = vols / "Macintosh HD"
    (ext / "Music").mkdir(parents=True)
    os.symlink("/", vols / "OS")
    isub, esub = home / "Music/_Serato_/Subcrates", ext / "_Serato_/Subcrates"
    write_crate(isub / "MHS06.crate", [])
    write_crate(esub / "MHS06%%CASE'S LIST.crate", ["Music/a.mp3"] * 4)
    write_crate(esub / "MHS06%%MSH06 - OPENING.crate", ["Music/b.mp3"])
    write_crate(esub / "102326 INTRO.crate", ["Music/c.mp3"])
    write_crate(esub / "#CASE CRATES#%%PREPS%%Week 1.crate", ["Music/d.mp3"])  # no parent files at all
    (home / "Music/_Serato_/neworder.pref").write_bytes(
        "[crate]MHS06\n[crate]MHS06%%CASE'S LIST\n[crate]MHS06%%MSH06 - OPENING\n[crate]102326 INTRO\n"
        "[crate]#CASE CRATES#\n".encode("utf-16"))
    saved = {}
    monkeypatch.setattr(Settings, "save", lambda self: saved.update(drive=self.crate_drive))
    win = MainWindow(Settings())
    p = win.crates
    p._find = partial(find_libraries, home, vols)
    p.refresh()

    def tops():
        return [lbl(p.tree.topLevelItem(i)) for i in range(p.tree.topLevelItemCount())]

    assert tops() == ["MHS06  (0)", "102326 INTRO  (1)", "#CASE CRATES#"]
    mhs = p.tree.topLevelItem(0)
    assert [lbl(mhs.child(i)) for i in range(mhs.childCount())] == ["CASE'S LIST  (4)", "MSH06 - OPENING  (1)"]
    assert "Stored on: Macintosh HD" in mhs.child(0).toolTip(0)
    week = p.item_for("#CASE CRATES#%%PREPS%%Week 1")
    assert lbl(week.parent()) == "PREPS" and lbl(week.parent().parent()) == "#CASE CRATES#"

    # Drive picker: external only keeps the nesting and Serato's order
    combo = p.drive_combo
    assert combo.itemData(0) == "all" and combo.count() == 3
    combo.setCurrentIndex(combo.findData(str(ext / "_Serato_")))
    assert tops() == ["MHS06", "102326 INTRO  (1)", "#CASE CRATES#"]
    assert saved["drive"] == str(ext / "_Serato_")
    combo.setCurrentIndex(combo.findData(str(home / "Music/_Serato_")))
    assert tops() == ["MHS06  (0)"]
    combo.setCurrentIndex(0)

    # Search: shows matches and their parents, hides the rest; clearing restores
    p.search.setText("case")
    p._apply_filter()
    visible = [lbl(it) for it in p._all_items() if not it.isHidden()]
    assert set(visible) == {"MHS06  (0)", "CASE'S LIST  (4)", "#CASE CRATES#"}
    assert p.item_for("MHS06").isExpanded()
    assert "2 matches" in p.note.text()
    p.search.setText("")
    p._apply_filter()
    assert all(not it.isHidden() for it in p._all_items())

    # Clicking a group that has no crate file loads its sub-crates' songs
    loaded = []
    p.crate_chosen.disconnect()
    p.crate_chosen.connect(lambda node, sub, add: loaded.append((node.key, sub)))
    p._clicked(p.item_for("#CASE CRATES#%%PREPS"))
    assert loaded == [("#CASE CRATES#%%PREPS", True)]
    win.close()


def test_column_divider_cursor_resize_and_no_tooltip(app, monkeypatch):
    from PySide6.QtCore import QEvent, QPoint, Qt
    from PySide6.QtGui import QHelpEvent
    from PySide6.QtTest import QTest
    from cratetag.gui import C_ARTIST, C_CONF, C_FILE, C_NYEAR, ResizableHeader, MainWindow
    monkeypatch.setattr(Settings, "save", lambda self: None)
    win = MainWindow(Settings(show_crates=False, theme="classic"))  # File column is a Classic default
    win.resize(1400, 400)
    win.show()
    from pathlib import Path
    from cratetag.tags import TrackInfo
    win.model.add_tracks([TrackInfo(path=Path("/x/A very long file name for fitting the column.mp3"))])
    QTest.qWait(20)
    hh = win.view.horizontalHeader()
    assert isinstance(hh, ResizableHeader)
    edge = hh.sectionViewportPosition(C_FILE) + hh.sectionSize(C_FILE)
    y = hh.height() // 2

    # Wider grab zone: a few pixels either side of the divider still counts
    for dx in (-5, -1, 0, 3, 5):
        assert hh.section_at_edge(edge + dx) == C_FILE
    mid = hh.sectionViewportPosition(C_ARTIST) + hh.sectionSize(C_ARTIST) // 2
    assert hh.section_at_edge(mid) == -1

    # Hovering shows and keeps the resize cursor
    QTest.mouseMove(hh.viewport(), QPoint(edge - 2, y))
    assert hh.cursor().shape() == Qt.SplitHCursor
    QTest.mouseMove(hh.viewport(), QPoint(mid, y))
    assert hh.cursor().shape() == Qt.ArrowCursor

    # No tooltip over a divider (New Year's header has one in the middle)
    ny_edge = hh.sectionViewportPosition(C_NYEAR)
    ev = QHelpEvent(QEvent.ToolTip, QPoint(ny_edge + 1, y), hh.mapToGlobal(QPoint(ny_edge + 1, y)))
    assert hh.event(ev) is True and not ev.isAccepted()

    # Drag resizes; double-click fits the column to its contents
    w0 = hh.sectionSize(C_FILE)
    QTest.mousePress(hh.viewport(), Qt.LeftButton, Qt.NoModifier, QPoint(edge - 2, y))
    QTest.mouseMove(hh.viewport(), QPoint(edge + 58, y))
    QTest.mouseRelease(hh.viewport(), Qt.LeftButton, Qt.NoModifier, QPoint(edge + 58, y))
    assert hh.sectionSize(C_FILE) == w0 + 60
    c_edge = hh.sectionViewportPosition(C_CONF) + hh.sectionSize(C_CONF)
    hh.resizeSection(C_FILE, 40)
    edge = hh.sectionViewportPosition(C_FILE) + hh.sectionSize(C_FILE)
    QTest.mouseDClick(hh.viewport(), Qt.LeftButton, Qt.NoModifier, QPoint(edge - 1, y))
    assert hh.sectionSize(C_FILE) > 200
    win.close()


def test_write_highlighted_without_checking(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QItemSelectionModel as SM
    from cratetag.gui import MainWindow
    lib = tmp_path / "lib10"
    lib.mkdir()
    files = [make_mp3(lib / f"Artist - Song {i}.mp3") for i in range(4)]
    monkeypatch.setattr(Settings, "save", lambda self: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    win = MainWindow(Settings(backup_mode="none", show_crates=False))
    win.scan([str(lib)])
    wait_idle(win)
    assert win.a_lookup.text() == "Analyze (4)"
    m = win.model
    m.manual_set([0, 1, 2, 3], "year", "1999")   # pretend analysis found a year for all four
    m.set_all_checked(False)                     # ...and nothing is checked
    assert win.a_write.text() == "Write Checked" and not win.a_write.isEnabled()

    # Highlight rows 1 and 2: the button writes exactly those, no checkboxes needed
    win.view.clearSelection()
    for r in (1, 2):
        win.view.selectionModel().select(win.proxy.index(r, 0), SM.Select | SM.Rows)
    assert win.a_write.text() == "Write Selected (2)" and win.a_write.isEnabled()
    keys = {win.model.rows[win.proxy.mapToSource(win.proxy.index(r, 0)).row()].info.path for r in (1, 2)}
    win.write_checked()
    wait_idle(win)
    assert {f for f in files if read_track(f).year == "1999"} == keys
    assert win.a_write.text() == "Write Checked"  # nothing left in the highlight: falls back to checked

    # Click empty space below the rows: highlight clears, back to checked rows
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    win.resize(1000, 900)  # tall enough for empty space under the rows (the floating bar keeps its own strip)
    win.show()
    QTest.qWait(10)
    QTest.mouseClick(win.view.viewport(), Qt.LeftButton, Qt.NoModifier, QPoint(50, win.view.viewport().height() - 5))
    assert not win.view.selectionModel().hasSelection()
    left = [i for i, r in enumerate(m.rows) if r.year_changes()]
    m.set_checked(left[:1], True)
    assert win.a_write.text() == "Write Checked (1)"
    win.write_checked()
    wait_idle(win)
    assert sum(read_track(f).year == "1999" for f in files) == 3
    win.close()


def test_track_search_filters_and_scopes_actions(app, monkeypatch):
    from pathlib import Path
    from cratetag.gui import C_ARTIST, MainWindow
    from cratetag.tags import TrackInfo
    monkeypatch.setattr(Settings, "save", lambda self: None)
    win = MainWindow(Settings(show_crates=False))
    win.model.add_tracks([
        TrackInfo(path=Path("/m/Aventura - Obsesion.mp3"), artist="Aventura", title="Obsesión"),
        TrackInfo(path=Path("/m/Blink-182 - All The Small Things.mp3"), artist="Blink-182", title="All The Small Things"),
        TrackInfo(path=Path("/m/Romeo Santos - Propuesta Indecente.mp3"), artist="Romeo Santos", title="Propuesta Indecente"),
        TrackInfo(path=Path("/m/Track 07 Aventura live.mp3"), artist="", title=""),
    ])

    def shown():
        return sorted(win.model.rows[win.proxy.mapToSource(win.proxy.index(r, 0)).row()].info.path.name
                      for r in range(win.proxy.rowCount()))

    win.track_search.setText("obsesion")          # accents and case ignored
    win._apply_track_search()
    assert shown() == ["Aventura - Obsesion.mp3"]
    win.track_search.setText("aventura")          # All: artist, title or file name
    win._apply_track_search()
    assert shown() == ["Aventura - Obsesion.mp3", "Track 07 Aventura live.mp3"]
    assert "4 tracks · 2 shown" in win.count_label.text()
    win.track_search_field.setCurrentIndex(win.track_search_field.findData("artist"))
    assert shown() == ["Aventura - Obsesion.mp3"]
    win.track_search_field.setCurrentIndex(win.track_search_field.findData("title"))
    win.track_search.setText("small things")      # several words, any order
    win._apply_track_search()
    assert shown() == ["Blink-182 - All The Small Things.mp3"]

    # Select All and Analyze cover only what's showing
    win.track_search_field.setCurrentIndex(win.track_search_field.findData("all"))
    win.track_search.setText("santos")
    win._apply_track_search()
    win.view.clearSelection()
    assert win._lookup_targets() == [2]
    assert win.a_lookup.text() == "Analyze (1)"
    win.select_all()
    assert [r.checked for r in win.model.rows] == [False, False, True, False]

    # A pending artist edit is searchable
    win.track_search.setText("")
    win._apply_track_search()
    win.model.setData(win.model.index(1, C_ARTIST), "blink-182 feat. Tom")
    win.track_search.setText("tom")
    win._apply_track_search()
    assert shown() == ["Blink-182 - All The Small Things.mp3"]
    win.close()


def test_all_songs_from_serato_library(app, tmp_path, monkeypatch):
    import os
    from functools import partial
    from PySide6.QtCore import Qt
    from cratetag.gui import C_ARTIST, C_YEAR, MainWindow
    from cratetag.lookup import Proposal
    from cratetag.serato import ALL_SONGS_KEY, find_libraries, write_database
    home, vols = tmp_path / "home", tmp_path / "Volumes"
    drive = vols / "DJ DRIVE"
    (drive / "Music").mkdir(parents=True)
    (home / "Music").mkdir(parents=True)
    os.symlink("/", vols / "Macintosh HD")
    a = make_mp3(drive / "Music" / "Aventura - Obsesion.mp3")
    add_serato_style_tags(a, 3)  # the real file says 1999 / Latin
    b = make_mp3(drive / "Music" / "Blink-182 - Small Things.mp3")
    write_database(drive / "_Serato_" / "database V2", [
        {"ttyp": "mp3", "pfil": "Music/Aventura - Obsesion.mp3", "tsng": "Obsesion", "tart": "Aventura",
         "ttyr": "1990", "tgen": "Bachata", "tlen": "04:12.00"},          # Serato's copy is out of date
        {"ttyp": "mp3", "pfil": "Music/Blink-182 - Small Things.mp3", "tsng": "All The Small Things",
         "tart": "Blink-182", "ttyr": "1999"},
        {"ttyp": "mp3", "pfil": "Music/Gone.mp3", "tsng": "Gone", "tart": "Nobody", "bmis": True},
        {"ttyp": "quicktime", "pfil": "Videos/Clip.mov", "tsng": "Clip"},
    ])
    internal_song = make_mp3(home / "Music" / "Internal Song.mp3")
    write_database(home / "Music" / "_Serato_" / "database V2", [
        {"ttyp": "mp3", "pfil": str(internal_song).lstrip("/"), "tsng": "Internal Song", "tart": "Me"},
        {"ttyp": "mp3", "pfil": str(drive / "Music" / "Blink-182 - Small Things.mp3").lstrip("/"),
         "tsng": "dup", "tart": "dup"},  # same file listed by both libraries: shown once
    ])
    monkeypatch.setattr(Settings, "save", lambda self: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *x, **k: QMessageBox.Yes)
    win = MainWindow(Settings())
    p = win.crates
    p._find = partial(find_libraries, home, vols)
    p.refresh()
    top = p.tree.topLevelItem(0)
    assert lbl(top) == "All Songs  (5)"           # quick count: each drive's list, before merging duplicates
    p._clicked(top)
    wait_idle(win)
    assert lbl(p.tree.topLevelItem(0)) == "All Songs  (4)"  # exact once loaded
    names = sorted(r.info.path.name for r in win.model.rows)
    assert names == ["Aventura - Obsesion.mp3", "Blink-182 - Small Things.mp3", "Gone.mp3", "Internal Song.mp3"]
    msg = win.statusBar().currentMessage()
    assert "4 MP3s" in msg and "1 video" in msg and "1 marked missing" in msg
    rows = {r.info.path.name: r for r in win.model.rows}
    assert rows["Gone.mp3"].status.startswith("File not found")
    assert rows["Aventura - Obsesion.mp3"].info.year == "1990"   # shown from Serato's copy, no file opened

    # Search works on the library rows straight away
    win.track_search.setText("small")
    win._apply_track_search()
    assert win.proxy.rowCount() == 1
    win.track_search.setText("")
    win._apply_track_search()

    # Fast model sort keeps the highlight on the same track
    i = win.model.row_for(str(b))
    win.view.selectRow(win.proxy.mapFromSource(win.model.index(i, 0)).row())
    win.view.sortByColumn(C_ARTIST, Qt.DescendingOrder)
    sel = win._selected_source_rows()
    assert len(sel) == 1 and win.model.rows[sel[0]].info.path == b
    assert [r.info.artist for r in win.model.rows][:2] == ["Nobody", "Me"]

    # Analyzing re-reads the real file: 1999 from the file, so nothing to change for the year
    import cratetag.workers as workers
    class FakeEngine:
        def __init__(self, s): pass
        def cancel(self): pass
        def lookup(self, info):
            assert info.year == "1999"  # the engine saw the file's real tags, not Serato's copy
            return Proposal(year="1999", genre="Latin", confidence=95, source="Test")
    monkeypatch.setattr(workers, "Engine", FakeEngine)
    i = win.model.row_for(str(a))
    win._lookup_rows([i])
    wait_idle(win)
    r = win.model.rows[win.model.row_for(str(a))]
    assert r.info.year == "1999" and r.info.genre == "Latin" and not r.info.extra.get("from_db")
    assert r.status == "Already correct"
    win.close()


def test_analyze_many_tracks_asks_first(app, monkeypatch):
    from pathlib import Path
    from cratetag.gui import MainWindow
    from cratetag.tags import TrackInfo
    asked = []
    monkeypatch.setattr(Settings, "save", lambda self: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.No)
    win = MainWindow(Settings(show_crates=False))
    win.model.add_tracks([TrackInfo(path=Path(f"/m/{i}.mp3"), artist="A", title=f"S{i}") for i in range(600)])
    win.lookup()
    assert asked and "Analyze 600 tracks" in asked[0] and not win._busy()
    win.close()


def test_highlighting_a_row_does_not_hide_checked_edits(app, tmp_path, monkeypatch):
    """Your Trap Dickey case: edits on two checked tracks, a different track highlighted."""
    from PySide6.QtCore import QItemSelectionModel as SM
    from cratetag.gui import C_ARTIST, C_TITLE, MainWindow
    lib = tmp_path / "lib11"
    lib.mkdir()
    files = [make_mp3(lib / n) for n in ("Trap Dickey - Down South (Dirty).mp3",
                                          "Trap Dickey - Down South (Clean).mp3", "JAY-Z - Empire.mp3")]
    monkeypatch.setattr(Settings, "save", lambda self: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    win = MainWindow(Settings(backup_mode="none", show_crates=False))
    win.scan([str(lib)])
    wait_idle(win)
    m = win.model
    for f in files[:2]:
        i = m.row_for(str(f))
        m.setData(m.index(i, C_ARTIST), "Trap Dickey")
        m.setData(m.index(i, C_TITLE), "Down South")
    j = m.row_for(str(files[2]))
    win.view.clearSelection()
    win.view.selectionModel().select(win.proxy.mapFromSource(m.index(j, 0)), SM.Select | SM.Rows)
    assert win.a_write.text() == "Write Checked (2)" and win.a_write.isEnabled()
    win.write_checked()
    wait_idle(win)
    from cratetag.tags import read_raw_fields
    assert all(read_raw_fields(f)["artist"] == ["Trap Dickey"] for f in files[:2])
    assert read_raw_fields(files[2])["artist"] == []
    win.close()


def test_activity_log_records_edits_writes_and_why_nothing_was_written(app, tmp_path, monkeypatch, fresh_log):
    from PySide6.QtCore import QItemSelectionModel as SM
    from cratetag.gui import C_ARTIST, C_TITLE, ActivityLogDialog, MainWindow
    lib = tmp_path / "lib12"
    lib.mkdir()
    a = make_mp3(lib / "Trap Dickey - Down South (Dirty).mp3")
    j = make_mp3(lib / "JAY-Z - Empire.mp3")
    monkeypatch.setattr(Settings, "save", lambda self: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *x, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "information", lambda *x, **k: 0)
    win = MainWindow(Settings(backup_mode="none", show_crates=False))
    win.scan([str(lib)])
    wait_idle(win)
    m = win.model
    i = m.row_for(str(a))
    m.setData(m.index(i, C_ARTIST), "Trap Dickey")
    m.setData(m.index(i, C_TITLE), "Down South")
    # Highlight JAY-Z and use the explicit "write selected": nothing to write, and the log says why
    k = m.row_for(str(j))
    win.view.clearSelection()
    win.view.selectionModel().select(win.proxy.mapFromSource(m.index(k, 0)), SM.Select | SM.Rows)
    win.write_selected()
    win.write_checked()
    wait_idle(win)
    log = fresh_log.read_text(encoding="utf-8")
    assert "started ────" in log
    assert "Load     folder/files: 2 tracks" in log
    assert "Trap Dickey - Down South (Dirty).mp3: artist (blank) → 'Trap Dickey' (not saved yet)" in log
    assert "nothing to write: 1 selected track(s) had no changes" in log and "checked: 1" in log
    assert "Write    started: 1 checked tracks" in log
    assert "Write OK     Trap Dickey - Down South (Dirty).mp3: artist (blank) → 'Trap Dickey', title (blank) → 'Down South'" in log
    assert "Write finished: 1 OK, 0 failed; no backup" in log

    dlg = ActivityLogDialog(win)
    assert "Write OK" in dlg.text.toPlainText()
    dlg.filter.setText("nothing to write")
    assert dlg.text.toPlainText().count("\n") == 0 and "nothing to write" in dlg.text.toPlainText()
    dlg.close()
    win.close()


def test_shortcut_registry_overrides_and_conflicts():
    from cratetag import shortcuts

    eff = shortcuts.effective({})
    assert eff["reveal"] == "Ctrl+R"  # ⌘R on a Mac
    assert not shortcuts.conflicts(eff), "the default shortcuts must not clash"
    eff2 = shortcuts.effective({"reveal": "Ctrl+Shift+R", "analyze": ""})
    assert eff2["reveal"] == "Ctrl+Shift+R" and eff2["analyze"] == ""
    assert shortcuts.overrides_from(eff2) == {"reveal": "Ctrl+Shift+R", "analyze": ""}
    eff2["set_title"] = "Ctrl+Y"
    assert shortcuts.conflicts(eff2) == {"Ctrl+Y": ["set_title", "set_year"]}


def test_reveal_shortcut_and_custom_shortcuts(app, tmp_path, monkeypatch, fresh_log):
    from PySide6.QtGui import QKeySequence

    from cratetag import activity
    from cratetag.gui import MainWindow, SettingsDialog

    lib = tmp_path / "lib"
    lib.mkdir()
    a = make_mp3(lib / "Artist A - Song A.mp3")
    b = make_mp3(lib / "Artist B - Song B.mp3")
    monkeypatch.setattr(Settings, "save", lambda self: None)
    win = MainWindow(Settings())
    win.show()
    win.scan([str(lib)])
    wait_idle(win)

    reveal = win.cmd["reveal"]
    assert reveal.shortcut() == QKeySequence("Ctrl+R")
    shown = []
    monkeypatch.setattr(win, "_reveal_many", lambda paths: shown.append(list(paths)))

    win.view.selectRow(0)
    reveal.trigger()
    assert len(shown) == 1 and shown[0][0] in (a, b)
    win.highlight_all()
    reveal.trigger()
    assert sorted(shown[1]) == sorted([a, b])  # every highlighted track
    win.view.clearSelection()
    reveal.trigger()
    assert len(shown) == 2  # nothing highlighted: nothing revealed

    # Settings > Keyboard Shortcuts: a clash blocks Save, a valid change applies live
    dlg = SettingsDialog(win.settings, win)
    dlg.show_tab("shortcuts")
    assert dlg.tabs.currentWidget() is dlg.keys
    dlg.keys.set_shortcut("reveal", "Ctrl+L")  # already Analyze
    assert dlg.keys.conflicts() and not dlg._save_btn.isEnabled()
    assert "Reveal in Finder" in dlg.keys.warning.text()
    dlg.keys.set_shortcut("reveal", "Ctrl+Shift+R")
    dlg.keys.set_shortcut("set_title", "Ctrl+T")
    assert not dlg.keys.conflicts() and dlg._save_btn.isEnabled()
    dlg.apply()
    assert win.settings.shortcuts == {"reveal": "Ctrl+Shift+R", "set_title": "Ctrl+T"}
    win.apply_shortcuts()
    assert reveal.shortcut() == QKeySequence("Ctrl+Shift+R")
    assert win.cmd["set_title"].shortcut() == QKeySequence("Ctrl+T")

    dlg.keys.reset_defaults()
    dlg.apply()
    assert win.settings.shortcuts == {}
    win.apply_shortcuts()
    assert reveal.shortcut() == QKeySequence("Ctrl+R")

    # changes made through open_settings are logged
    def fake_exec(d):
        d.keys.set_shortcut("reveal", "Ctrl+Alt+R")
        return 1
    monkeypatch.setattr(SettingsDialog, "exec", fake_exec)
    win.open_settings()
    assert win.cmd["reveal"].shortcut() == QKeySequence("Ctrl+Alt+R")
    assert "Shortcut changed: Reveal in Finder" in activity.tail()

    # Remove from List leaves the files alone and ignores the key while typing in search
    win.view.selectRow(0)
    win.cmd["remove"].trigger()
    assert len(win.model.rows) == 1 and a.exists() and b.exists()
    win.close()


def _deck_window(tmp_path, monkeypatch, n=4):
    from cratetag.gui import MainWindow
    lib = tmp_path / "deck"
    lib.mkdir()
    files = [make_mp3(lib / f"Artist {i} - Song {i}.mp3") for i in range(n)]
    monkeypatch.setattr(Settings, "save", lambda self: None)
    win = MainWindow(Settings(show_crates=False, backup_mode="none"))
    win.resize(1300, 700)
    win.show()
    win.scan([str(lib)])
    wait_idle(win)
    return win, files


def test_deck_stage_cards_filter_and_floating_bar(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QItemSelectionModel as SM
    from PySide6.QtTest import QTest
    from cratetag.lookup import Proposal

    win, files = _deck_window(tmp_path, monkeypatch)
    assert win.deck and win.stages is not None and not win.findChildren(type(win.addToolBar("x")))[1:]
    rows = win.model.rows
    s = win.settings
    win.model.apply_proposal(rows[0].key, Proposal(year="2004", genre="Reggaeton", confidence=95, source="Discogs"), s)
    win.model.apply_proposal(rows[1].key, Proposal(note="No match"), s)
    win.model.apply_proposal(rows[2].key, Proposal(year="1999", genre="Hip-Hop", confidence=92, source="Deezer"), s)
    win.model.apply_written(rows[2].key, True, "Written", rows[2].info)
    win._update_stages()
    num = {k: int(c.num.text().replace(",", "")) for k, c in win.stages.cards.items()}
    assert num == {"all": 4, "analyzed": 3, "ready": 1, "written": 1, "attention": 1}
    assert rows[0].stage() == "ready" and rows[1].stage() == "attention" and rows[3].stage() == "new"

    # Click "Ready to write": only that track shows; Analyze and Select All act on what's shown
    win.stages.cards["ready"].clicked.emit("ready")
    assert win.proxy.rowCount() == 1 and win._visible_rows() == [0]
    assert win.a_lookup.text() == "Analyze (1)"
    win.stages.cards["ready"].clicked.emit("ready")  # again: everything
    assert win.proxy.rowCount() == 4 and win.stages.current is None
    win.stages.cards["attention"].clicked.emit("attention")
    assert [win.model.rows[i].status for i in win._visible_rows()] == ["No match"]
    win.stages.cards["all"].clicked.emit("all")

    # Floating bar appears with the highlight and shows shortcuts
    assert not win.floating.isVisible()
    win.view.selectionModel().select(win.proxy.index(0, 0), SM.Select | SM.Rows)
    QTest.qWait(10)
    assert win.floating.isVisible() and "1" in win.floating.count.text()
    # The list stops above the bar, so no row is hidden under it
    vp = win.view.viewport()
    vp_bottom = vp.mapTo(win, vp.rect().bottomLeft()).y()
    assert vp_bottom < win.floating.mapTo(win, win.floating.rect().topLeft()).y()
    labels = [b.text() for b, _a, _t in win.floating.buttons]
    assert any(t.startswith("Reveal") for t in labels) and any(t.startswith("Set Year") for t in labels) and labels[-1].startswith("Write Selected (1)")
    win.view.clearSelection()
    QTest.qWait(10)
    assert not win.floating.isVisible() and win.view.viewportMargins().bottom() == 0
    win.close()


def test_deck_edit_artist_line_and_title(app, tmp_path, monkeypatch):
    from cratetag.gui import C_TITLE
    win, files = _deck_window(tmp_path, monkeypatch, n=1)
    from cratetag.gui import C_ARTIST
    win.model.rows[0].info.artist, win.model.rows[0].info.title = "Artist 0", "Song 0"
    win._set_column_visible(C_ARTIST, False)  # artist shows under the song only when its column is hidden
    idx = win.proxy.index(0, C_TITLE)
    win.delegate.start_artist_edit(idx)
    win.view.edit(idx)
    ed = win.view.indexWidget(idx) or win.view.focusWidget()
    assert ed.text() == "Artist 0"
    ed.setText("Artist Zero")
    win.view.commitData(ed)
    from PySide6.QtWidgets import QAbstractItemDelegate
    win.view.closeEditor(ed, QAbstractItemDelegate.NoHint)
    r = win.model.rows[0]
    assert r.new_artist == "Artist Zero" and r.new_title is None and r.checked
    win.view.edit(idx)  # the top line is the title
    ed = win.view.focusWidget()
    assert ed.text() == "Song 0"
    win.close()


def test_command_palette_runs_a_command(app, tmp_path, monkeypatch):
    from cratetag.deck import CommandPalette
    win, files = _deck_window(tmp_path, monkeypatch, n=2)
    ran = []
    monkeypatch.setattr(win, "highlight_all", lambda: ran.append("all"))
    win.cmd["highlight_all"].triggered.disconnect()
    win.cmd["highlight_all"].triggered.connect(lambda: win.highlight_all())

    def fake_exec(dlg):
        dlg.edit.setText("highlight all")
        dlg._run(dlg.list.currentItem())
        return 1
    monkeypatch.setattr(CommandPalette, "exec", fake_exec)
    win.open_palette()
    assert ran == ["all"]
    win.close()


def test_folder_browser_loads_a_folder(app, tmp_path, monkeypatch):
    from cratetag.gui import MainWindow
    lib = tmp_path / "Music" / "Latin" / "Punta"
    lib.mkdir(parents=True)
    for i in range(3):
        make_mp3(lib / f"Banda {i} - Song {i}.mp3")
    monkeypatch.setattr(Settings, "save", lambda self: None)
    win = MainWindow(Settings(show_crates=False, last_folder=str(tmp_path / "Music")))
    win.show()
    win.a_folders.trigger()
    assert win.folders is not None and win.side.isVisible() and win.a_folders.isChecked()
    assert not win.a_crates.isChecked()
    win.folders.folder_chosen.emit(str(lib), False)
    wait_idle(win)
    assert len(win.model.rows) == 3
    assert win.title_lbl.text() == "Punta" and "3 tracks" in win.subtitle_lbl.text()
    win.a_folders.trigger()  # click again: hide the column
    assert not win.side.isVisible()
    win.close()


def test_classic_look_still_available(app, monkeypatch):
    from PySide6.QtWidgets import QToolBar
    from cratetag.gui import C_FILE, MainWindow
    monkeypatch.setattr(Settings, "save", lambda self: None)
    win = MainWindow(Settings(theme="classic", show_crates=False))
    assert win.findChildren(QToolBar) and getattr(win, "stages", None) is None
    from PySide6.QtCore import Qt
    assert win.model.headerData(3, Qt.Horizontal) == "Title"
    assert not win.view.horizontalHeader().isSectionHidden(C_FILE)
    win.close()


def test_header_box_checks_and_unchecks_all(app, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    win, files = _deck_window(tmp_path, monkeypatch, n=4)
    hh = win.view.horizontalHeader()
    box = hh.check_box_rect()
    assert not box.isNull() and hh.check_state == Qt.Unchecked
    sort_before = hh.sortIndicatorSection(), hh.sortIndicatorOrder()

    QTest.mouseClick(hh.viewport(), Qt.LeftButton, Qt.NoModifier, box.center())
    assert all(r.checked for r in win.model.rows) and hh.check_state == Qt.Checked
    assert (hh.sortIndicatorSection(), hh.sortIndicatorOrder()) == sort_before  # the box doesn't sort

    win.model.set_checked([0], False)
    assert hh.check_state == Qt.PartiallyChecked
    QTest.mouseClick(hh.viewport(), Qt.LeftButton, Qt.NoModifier, box.center())  # some checked: check all
    assert all(r.checked for r in win.model.rows)
    QTest.mouseClick(hh.viewport(), Qt.LeftButton, Qt.NoModifier, box.center())  # all checked: uncheck all
    assert not any(r.checked for r in win.model.rows) and hh.check_state == Qt.Unchecked

    # While searching, only the tracks showing are checked
    win.track_search.setText("Song 2")
    win._apply_track_search()
    QTest.mouseClick(hh.viewport(), Qt.LeftButton, Qt.NoModifier, box.center())
    assert [r.checked for r in win.model.rows] == [False, False, True, False]
    assert hh.check_state == Qt.Checked
    win.close()


def test_deck_default_columns_and_button_order(app, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QToolButton
    from cratetag.gui import (C_ARTIST, C_CONF, C_FILE, C_GENRE, C_NGENRE, C_NYEAR, C_SEARCH, C_SRC, C_STATUS,
                              C_TITLE, C_YEAR, MainWindow)
    monkeypatch.setattr(Settings, "save", lambda self: None)
    old = Settings(show_crates=False, header_state="AAAA", header_cols="deck2|old layout")  # saved before the update
    win = MainWindow(old)
    hh = win.view.horizontalHeader()
    shown = [c for c in range(hh.count()) if not hh.isSectionHidden(c)]
    shown.sort(key=hh.visualIndex)
    assert [win.model.headerData(c, Qt.Horizontal) for c in shown] == \
        ["", "Artist", "Song", "Year in File", "Year Found", "Genre", "Match", "Source", "Status"]
    assert all(hh.isSectionHidden(c) for c in (C_FILE, C_GENRE, C_SEARCH))

    # Year from, Genre from on the left; Analyze and Stop under the Ready/Written cards; Write mode and Write right
    lay = win.opts_layout
    order = []
    for i in range(lay.count()):
        w = lay.itemAt(i).widget()
        if w is win.analyze_box:
            w = win.analyze_btn
        if isinstance(w, QToolButton):
            order.append(w.defaultAction().text().split(" (")[0])
        elif w is win.opts_left:
            order.append("[year from, genre from]")
        elif w is win.write_combo:
            order.append("[write]")
    assert order == ["[year from, genre from]", "Analyze", "Stop", "[write]", "Write Checked"]
    assert win.year_src_combo.parentWidget() is win.opts_left and win.genre_src_combo.parentWidget() is win.opts_left
    assert not any(t in order for t in ("Check All", "Check None", "Clear"))
    win.resize(1440, 900)
    win.show()
    from PySide6.QtTest import QTest
    QTest.qWait(30)
    def left(w):
        return w.mapTo(win, w.rect().topLeft()).x()
    ready, written = win.stages.cards["ready"], win.stages.cards["written"]
    assert left(ready) <= left(win.analyze_btn) < left(written)  # Analyze under Ready to write, next to Stop
    assert 5 <= left(written) - left(win.stop_btn) <= 30  # just left of the Written card
    gap = left(win.stop_btn) - (left(win.analyze_btn) + win.analyze_btn.width())
    assert 20 <= gap <= 50, gap
    win.close()


def test_sort_by_genre_uses_what_the_column_shows(app, monkeypatch):
    from pathlib import Path
    from PySide6.QtCore import Qt
    from cratetag.gui import C_ARTIST, C_NGENRE, MainWindow
    from cratetag.lookup import Proposal
    from cratetag.tags import TrackInfo
    monkeypatch.setattr(Settings, "save", lambda self: None)
    win = MainWindow(Settings(show_crates=False))
    data = [("Rolling Stones", "Top 40"), ("Beatles", ""), ("Ben E. King", "Other"), ("Chubby Checker", "Rock")]
    win.model.add_tracks([TrackInfo(path=Path(f"/x/{a}.mp3"), artist=a, title="t", genre=g) for a, g in data])

    def artists():
        return [win.model.rows[win.proxy.mapToSource(win.proxy.index(i, 0)).row()].info.artist
                for i in range(win.proxy.rowCount())]

    win.view.sortByColumn(C_NGENRE, Qt.AscendingOrder)  # not analyzed: the file's genres
    assert artists() == ["Ben E. King", "Chubby Checker", "Rolling Stones", "Beatles"]  # blank last
    win.view.sortByColumn(C_NGENRE, Qt.DescendingOrder)
    assert artists() == ["Rolling Stones", "Chubby Checker", "Ben E. King", "Beatles"]  # blank still last

    # A genre found by analysis sorts by the new genre
    beatles = next(r for r in win.model.rows if r.info.artist == "Beatles")
    win.model.apply_proposal(beatles.key, Proposal(genre="Pop", confidence=95, source="Deezer"), win.settings)
    win.view.sortByColumn(C_NGENRE, Qt.AscendingOrder)
    assert artists() == ["Ben E. King", "Beatles", "Chubby Checker", "Rolling Stones"]
    win.view.sortByColumn(C_ARTIST, Qt.AscendingOrder)
    assert artists() == ["Beatles", "Ben E. King", "Chubby Checker", "Rolling Stones"]
    win.close()


def test_checkbox_click_does_not_highlight(app, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from cratetag.gui import C_CHECK
    win, files = _deck_window(tmp_path, monkeypatch, n=4)
    for r in (2, 3):  # click two checkboxes, like checking two tracks by hand
        idx = win.proxy.index(r, C_CHECK)
        QTest.mouseClick(win.view.viewport(), Qt.LeftButton, Qt.NoModifier, win.view.visualRect(idx).center())
    assert sum(r.checked for r in win.model.rows) == 2
    assert not win.view.selectionModel().hasSelection()
    assert win.a_lookup.text() == "Analyze (2)"
    # clicking a box again unchecks it, still without highlighting
    idx = win.proxy.index(3, C_CHECK)
    QTest.mouseClick(win.view.viewport(), Qt.LeftButton, Qt.NoModifier, win.view.visualRect(idx).center())
    assert sum(r.checked for r in win.model.rows) == 1 and win.a_lookup.text() == "Analyze (1)"
    # a double-click on a box never highlights the row or opens anything
    QTest.mouseDClick(win.view.viewport(), Qt.LeftButton, Qt.NoModifier, win.view.visualRect(idx).center())
    assert not win.view.selectionModel().hasSelection() and win.view.state() != win.view.State.EditingState
    win.model.set_checked([win.proxy.mapToSource(idx).row()], False)
    # an existing highlight stays put when a box is clicked
    win.view.selectRow(0)
    idx = win.proxy.index(1, C_CHECK)
    QTest.mouseClick(win.view.viewport(), Qt.LeftButton, Qt.NoModifier, win.view.visualRect(idx).center())
    assert win._selected_source_rows() == [win.proxy.mapToSource(win.proxy.index(0, 0)).row()]
    win.close()


def test_check_for_updates_from_github(app, monkeypatch):
    from PySide6.QtTest import QTest
    from cratetag import updates
    from cratetag.gui import MainWindow

    assert updates.parse_version('"""x"""\n__version__ = "0.12.3"\n') == "0.12.3"
    assert updates.is_newer("0.12.10", "0.12.9") and not updates.is_newer("0.11.2", "0.12.0")
    assert not updates.is_newer(None)

    monkeypatch.setattr(Settings, "save", lambda self: None)
    msgs = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: msgs.append(a[2]))
    win = MainWindow(Settings(show_crates=False))
    launched = []
    monkeypatch.setattr(win, "_launch_updater", lambda args, q: launched.append((args, q)) or True)

    def wait():
        for _ in range(100):
            QTest.qWait(20)
            if not getattr(win, "_update_checking", False):
                return

    # Nothing newer: the quiet launch-time check says nothing; a manual check says you're up to date
    monkeypatch.setattr(updates, "latest_version", lambda: "0.0.1")
    win.check_for_updates(quiet=True)
    wait()
    assert not win.update_btn.isVisible() and not msgs
    win.check_for_updates()
    wait()
    assert msgs and "latest version" in msgs[-1]

    # Newer on GitHub: a button appears; clicking it runs the updater in GitHub mode
    monkeypatch.setattr(updates, "latest_version", lambda: "99.0.0")
    win.show()
    win.check_for_updates(quiet=True)
    wait()
    assert win.update_btn.isVisible() and win.update_btn.text() == "Update to 99.0.0"
    win.update_btn.click()
    assert launched and launched[-1][0] == ["--github"] and "99.0.0" in launched[-1][1]

    # GitHub unreachable: quiet check stays silent
    def boom():
        raise OSError("Couldn't reach GitHub: offline")
    monkeypatch.setattr(updates, "latest_version", boom)
    n = len(msgs)
    win.check_for_updates(quiet=True)
    wait()
    assert len(msgs) == n
    win.close()
