"""Tests for tag safety, parsing, and the lookup engine (network mocked)."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.id3 import COMM, GEOB, ID3, POPM, TBPM, TCON, TDRC, TIT2, TKEY, TPE1, TYER
from mutagen.mp3 import MP3

from cratetag.config import Settings
from cratetag.lookup import Engine, Proposal
from cratetag.parsing import parse_filename, search_terms
from cratetag.tags import find_mp3s, read_track, write_year_genre

FFMPEG = shutil.which("ffmpeg")
SERATO_MARKERS = b"\x01\x01" + b"CUE\x00" + bytes(range(256)) * 4  # arbitrary binary payload
SERATO_BEATGRID = b"\x01\x00" + b"\x00\x00\x00\x01" + b"\x3e\x99\x99\x9a" * 40


def make_mp3(path: Path, seconds: float = 2.0) -> Path:
    if not FFMPEG:
        pytest.skip("ffmpeg not available")
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", "-c:a", "libmp3lame", "-b:a", "128k",
                    "-id3v2_version", "0", "-write_xing", "0", str(path)], check=True)
    return path


def audio_hash(path: Path) -> str:
    """Hash only the audio after the ID3v2 header so we can prove audio is untouched."""
    data = path.read_bytes()
    if data[:3] == b"ID3":
        size = 0
        for b in data[6:10]:
            size = (size << 7) | (b & 0x7F)
        data = data[10 + size:]
    if data[-128:-125] == b"TAG":
        data = data[:-128]
    return hashlib.sha256(data).hexdigest()


def add_serato_style_tags(path: Path, version: int) -> None:
    tags = ID3()
    tags.add(TPE1(encoding=3, text=["Aventura"]))
    tags.add(TIT2(encoding=3, text=["Obsesion"]))
    tags.add(TBPM(encoding=3, text=["126"]))
    tags.add(TKEY(encoding=3, text=["8A"]))
    tags.add(COMM(encoding=3, lang="eng", desc="", text=["my notes"]))
    tags.add(POPM(email="no@email", rating=196, count=3))
    tags.add(GEOB(encoding=0, mime="application/octet-stream", filename="", desc="Serato Markers2", data=SERATO_MARKERS))
    tags.add(GEOB(encoding=0, mime="application/octet-stream", filename="", desc="Serato BeatGrid", data=SERATO_BEATGRID))
    if version == 3:
        tags.add(TYER(encoding=1, text=["1999"]))
        tags.add(TCON(encoding=1, text=["Latin"]))
        tags.update_to_v23()
    else:
        tags.add(TDRC(encoding=3, text=["1999"]))
        tags.add(TCON(encoding=3, text=["Latin"]))
    tags.save(path, v2_version=version)


# ---------------------------------------------------------------- tag safety

@pytest.mark.parametrize("version", [3, 4])
def test_write_preserves_serato_frames_and_audio(tmp_path, version):
    f = make_mp3(tmp_path / "song.mp3")
    add_serato_style_tags(f, version)
    before_audio = audio_hash(f)
    before = ID3(f)

    write_year_genre(f, year="2002", genre="Bachata")

    after = ID3(f)
    assert after.version[1] == version, "ID3 version must not change"
    assert read_track(f).year == "2002"
    assert read_track(f).genre == "Bachata"
    # Serato GEOB frames byte-for-byte identical
    for desc in ("Serato Markers2", "Serato BeatGrid"):
        assert after[f"GEOB:{desc}"].data == before[f"GEOB:{desc}"].data
    # Other frames untouched
    for key in ("TPE1", "TIT2", "TBPM", "TKEY"):
        assert str(after[key]) == str(before[key])
    assert after.getall("COMM")[0].text == ["my notes"]
    assert after.getall("POPM")[0].rating == 196
    # Audio identical and file still decodes
    assert audio_hash(f) == before_audio
    assert MP3(f).info.length > 1.5


def test_v23_year_stored_as_tyer(tmp_path):
    f = make_mp3(tmp_path / "a.mp3")
    add_serato_style_tags(f, 3)
    write_year_genre(f, year="2005")
    raw = f.read_bytes()[:4096]
    assert b"TYER" in raw and b"TDRC" not in raw


def test_untagged_file_gets_v23(tmp_path):
    f = make_mp3(tmp_path / "plain.mp3")
    write_year_genre(f, year="1987", genre="Punta")
    t = ID3(f)
    assert t.version[1] == 3
    info = read_track(f)
    assert (info.year, info.genre) == ("1987", "Punta")


def test_none_leaves_field_and_blank_removes(tmp_path):
    f = make_mp3(tmp_path / "x.mp3")
    add_serato_style_tags(f, 4)
    write_year_genre(f, year=None, genre="Reggaeton")
    assert read_track(f).year == "1999"
    write_year_genre(f, year="", genre=None)
    assert read_track(f).year == ""
    assert read_track(f).genre == "Reggaeton"


def test_rejects_bad_year(tmp_path):
    f = make_mp3(tmp_path / "x.mp3")
    with pytest.raises(ValueError):
        write_year_genre(f, year="99")


def test_keep_mtime(tmp_path):
    import os
    f = make_mp3(tmp_path / "x.mp3")
    os.utime(f, (1_000_000_000, 1_000_000_000))
    write_year_genre(f, year="2010", keep_mtime=True)
    assert int(f.stat().st_mtime) == 1_000_000_000


def test_find_mp3s_skips_resource_forks(tmp_path):
    make_mp3(tmp_path / "a.mp3")
    (tmp_path / "._a.mp3").write_bytes(b"junk")
    (tmp_path / "sub").mkdir()
    make_mp3(tmp_path / "sub" / "B.MP3")
    (tmp_path / "notes.txt").write_text("x")
    names = [p.name for p in find_mp3s([tmp_path])]
    assert names == ["a.mp3", "B.MP3"]


# ---------------------------------------------------------------- parsing

@pytest.mark.parametrize("name,expected", [
    ("03 - Aventura - Obsesion (Clean).mp3", ("Aventura", "Obsesion")),
    ("Wisin & Yandel - Rakata (Intro Dirty) 8A 98.mp3", ("Wisin & Yandel", "Rakata")),
    ("Aventura_-_Obsesion_[Official Audio].mp3", ("Aventura", "Obsesion")),
    ("Garifuna Kids - Wanaragua.mp3", ("Garifuna Kids", "Wanaragua")),
    ("track01.mp3", ("", "")),
    ("01.mp3", ("", "")),
])
def test_parse_filename(name, expected):
    assert parse_filename(name) == expected


def test_search_terms_prefers_tags_and_strips_feat():
    assert search_terms("Bad Bunny feat. Drake", "Mia (Quick Hit)", "x.mp3") == ("Bad Bunny", "Mia")
    assert search_terms("", "", "Aventura - Obsesion.mp3") == ("Aventura", "Obsesion")


# ---------------------------------------------------------------- engine (mocked HTTP)

class FakeHttp:
    """Serves canned JSON by URL substring, records calls."""

    def __init__(self, routes: dict[str, dict]):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []
        self.cancelled = False

    def get_json(self, url, params=None, headers=None):
        self.calls.append((url, params or {}))
        kind = (params or {}).get("type")
        for key, val in self.routes.items():
            if key in url and (kind is None or key != "/database/search" or True):
                if callable(val):
                    return val(url, params or {})
                return val
        return {}


def mb_search(_url, _p):
    return {"recordings": [
        {"id": "rec-1", "title": "Obsesión", "length": 252000, "first-release-date": "2002-04-30",
         "artist-credit": [{"name": "Aventura"}], "releases": [{"date": "2002-04-30"}, {"date": "2005"}]},
        {"id": "rec-2", "title": "Obsesión (live)", "length": 300000, "first-release-date": "2007",
         "artist-credit": [{"name": "Aventura"}], "releases": []},
        {"id": "rec-3", "title": "Obsession", "length": 200000, "first-release-date": "1990",
         "artist-credit": [{"name": "Someone Else"}], "releases": []},
    ]}


def discogs_search(_url, p):
    if p.get("type") == "master":
        return {"results": [{"type": "master", "id": 77, "title": "Aventura - We Broke The Rules",
                             "year": "2002", "genre": ["Latin"], "style": ["Bachata"], "uri": "/master/77"}]}
    return {"results": []}


def discogs_master(_url, _p):
    return {"year": 2002, "genres": ["Latin"], "styles": ["Bachata"],
            "artists": [{"name": "Aventura", "join": ""}],
            "tracklist": [{"title": "Obsesión", "duration": "4:12"}, {"title": "Otra", "duration": "3:00"}]}


ROUTES = {
    "musicbrainz.org/ws/2/recording/rec-1": {"id": "rec-1", "genres": [{"name": "bachata", "count": 3}],
                                             "artist-credit": [{"name": "Aventura", "artist": {"id": "a1"}}]},
    "musicbrainz.org/ws/2/recording": mb_search,
    "api.discogs.com/database/search": discogs_search,
    "api.discogs.com/masters/77": discogs_master,
}


def track(tmp_path, name="Aventura - Obsesion.mp3", artist="Aventura", title="Obsesion", duration=252.0):
    from cratetag.tags import TrackInfo
    return TrackInfo(path=tmp_path / name, artist=artist, title=title, duration=duration)


def test_engine_combines_sources(tmp_path):
    s = Settings(discogs_token="t")
    eng = Engine(s, http=FakeHttp(ROUTES))
    p = eng.lookup(track(tmp_path))
    assert p.year == "2002"
    assert p.genre == "Bachata"
    assert p.confidence >= 90
    assert "agree" in p.note
    assert p.candidates[0].artist == "Aventura"
    # the wrong-artist "Obsession" from 1990 must not set the year
    assert p.year != "1990"


def test_engine_musicbrainz_only_genre_fallback(tmp_path):
    s = Settings(use_discogs=False, genre_source="auto")
    eng = Engine(s, http=FakeHttp(ROUTES))
    p = eng.lookup(track(tmp_path))
    assert p.year == "2002"
    assert p.genre == "Bachata"  # from MusicBrainz genres (title-cased)


def test_engine_discogs_broad_genre(tmp_path):
    s = Settings(discogs_token="t", genre_source="discogs_genre")
    p = Engine(s, http=FakeHttp(ROUTES)).lookup(track(tmp_path))
    assert p.genre == "Latin"


def test_engine_no_title(tmp_path):
    from cratetag.tags import TrackInfo
    p = Engine(Settings(), http=FakeHttp(ROUTES)).lookup(TrackInfo(path=tmp_path / "Track 07.mp3"))
    assert p.error and not p.year


def test_engine_no_results(tmp_path):
    p = Engine(Settings(use_discogs=False), http=FakeHttp({})).lookup(track(tmp_path))
    assert not p.year and not p.genre
    assert p.note == "No results"


def test_engine_wrong_artist_only_is_not_confident(tmp_path):
    routes = {"musicbrainz.org/ws/2/recording": lambda u, p: {"recordings": [
        {"id": "x", "title": "Obsesion", "first-release-date": "1990",
         "artist-credit": [{"name": "Totally Different Band"}], "releases": []}]}}
    p = Engine(Settings(use_discogs=False), http=FakeHttp(routes)).lookup(track(tmp_path))
    assert p.year == ""


def test_genre_aliases_and_max(tmp_path):
    def ds(_u, p):
        return {"results": [{"type": "master", "id": 77, "title": "Aventura - X", "year": "2002",
                             "genre": ["Folk, World, & Country", "Latin"], "style": [], "uri": "/master/77"}]} \
            if p.get("type") == "master" else {"results": []}

    def dm(_u, _p):
        return {"year": 2002, "genres": ["Folk, World, & Country", "Latin"], "styles": [],
                "artists": [{"name": "Aventura"}], "tracklist": [{"title": "Obsesion", "duration": "4:12"}]}

    routes = dict(ROUTES)
    routes["api.discogs.com/database/search"] = ds
    routes["api.discogs.com/masters/77"] = dm
    s = Settings(discogs_token="t", use_musicbrainz=False, genre_source="discogs_genre", max_genres=2)
    p = Engine(s, http=FakeHttp(routes)).lookup(track(tmp_path))
    assert p.genre == "World; Latin"


# ---------------------------------------------------------------- DJ-pool titles

@pytest.mark.parametrize("artist,title,expected", [
    ("T.I.", "LET 'EM KNOW - KidCutUp Clap Intro (Dirty) - 11A - 84", ("T.I.", "LET 'EM KNOW")),
    ("Drake", "Janice STFU - Tall Boys Remix (Intro - Clean) - 1A - 130", ("Drake", "Janice STFU")),
    ("310babii", "swang (R-You Slam Edit) (Dirty Extended) - 8A - 100", ("310babii", "swang")),
    ("310babii vs Juicy J", "Back It Up (DJ Nasa Break Intro) (Clean Extended) - 9A - 100", ("310babii", "Back It Up")),
    ("A Boogie Wit Da Hoodie", "PGD (DJcity Intro - Clean) - 7A - 128", ("A Boogie Wit Da Hoodie", "PGD")),
    ("Belly Gang Kushington", "Friend Do - MarkCutz Hype Intro (Clean) - 2A - 80", ("Belly Gang Kushington", "Friend Do")),
    ("Big Sean ft Nicki Minaj", "Dance (Ass) (Remix) (DJ Nasa Slam Edit) (Clean Extended) - 9A - 80", ("Big Sean", "Dance (Ass)")),
    ("Big Sean, Nicki Minaj", "Dance A$$ (Deville Wordplay Intro) Dirty - No Nicki Verse - 9A - 80", ("Big Sean, Nicki Minaj", "Dance A$$")),
    ("DaBaby vs French Montana", "Pop Dat Thang x Pop That (Scooter Blend / Clean) - 10B - 138", ("DaBaby", "Pop Dat Thang")),
    ("Doechii & JT", "Alter Ego - KidCutUp Acap Intro (Clean) - 7A - 127", ("Doechii & JT", "Alter Ego")),
    ("Prince", "1999", ("Prince", "1999")),
    ("Blink", "Area 51", ("Blink", "Area 51")),
    ("Daddy Yankee", "Gasolina - 8A", ("Daddy Yankee", "Gasolina")),
])
def test_dj_pool_cleanup(artist, title, expected):
    assert search_terms(artist, title, "x.mp3") == expected


def test_attempts_fall_back_to_first_artist_and_title_only():
    from cratetag.parsing import search_attempts
    att = search_attempts("Doechii & JT", "Alter Ego - KidCutUp Acap Intro (Clean) - 7A - 127",
                          "Doechii & JT - Alter Ego - KidCutUp Acap Intro (Clean) - 7A - 127.mp3")
    assert att[0] == ("Doechii & JT", "Alter Ego")
    assert ("Doechii", "Alter Ego") in att
    assert ("", "Alter Ego") in att


def test_ignore_words():
    assert search_terms("X", "Song Name DJ Foo Special", "x.mp3", ["DJ Foo Special"]) == ("X", "Song Name")


def test_engine_searches_clean_title_and_falls_back(tmp_path):
    from cratetag.tags import TrackInfo
    queries = []

    def mb(_u, p):
        q = p.get("query", "")
        queries.append(q)
        # Only the first-artist query finds it
        if 'artist:"Doechii"' in q and "Alter Ego" in q:
            return {"recordings": [{"id": "r9", "title": "Alter Ego", "first-release-date": "2024-09-27",
                                    "artist-credit": [{"name": "Doechii"}, {"joinphrase": " & ", "name": "JT"}],
                                    "genres": [{"name": "hip hop", "count": 2}], "releases": []}]}
        return {"recordings": []}

    info = TrackInfo(path=tmp_path / "a.mp3", artist="Doechii & JT",
                     title="Alter Ego - KidCutUp Acap Intro (Clean) - 7A - 127", duration=300)
    p = Engine(Settings(use_discogs=False), http=FakeHttp({"musicbrainz.org/ws/2/recording": mb})).lookup(info)
    assert p.year == "2024"
    assert p.genre == "Hip-Hop"
    assert all("KidCutUp" not in q and "127" not in q for q in queries)
    assert (p.search_artist, p.search_title) == ("Doechii", "Alter Ego")


def test_engine_override_search_terms(tmp_path):
    from cratetag.tags import TrackInfo
    seen = []
    routes = {"musicbrainz.org/ws/2/recording": lambda u, p: seen.append(p["query"]) or {"recordings": []}}
    info = TrackInfo(path=tmp_path / "junk.mp3", artist="Wrong", title="Garbage", extra={
        "search_artist": "Aventura", "search_title": "Obsesion"})
    Engine(Settings(use_discogs=False), http=FakeHttp(routes)).lookup(info)
    assert seen and all('"Obsesion"' in q or "Obsesion" in q for q in seen)
    assert all("Garbage" not in q for q in seen)


# ---------------------------------------------------------------- source preference

def test_choose_year_respects_source_preference():
    from cratetag.lookup import Candidate, choose_year
    cands = [Candidate("MusicBrainz", "Wiz Khalifa", "Young, Wild & Free", original_year="2011", score=0.95),
             Candidate("Discogs", "Wiz Khalifa", "Young, Wild & Free", release_year="2012", score=0.93)]
    assert choose_year(cands, Settings(year_source="auto"))[0] == "2011"
    assert choose_year(cands, Settings(year_source="discogs"))[0] == "2012"
    assert choose_year(cands, Settings(year_source="musicbrainz"))[0] == "2011"
    # A specific source is strict: nothing from Discogs means no year, Auto still finds it
    assert choose_year(cands[:1], Settings(year_source="discogs"))[0] == ""
    assert choose_year(cands[:1], Settings(year_source="auto"))[0] == "2011"


def test_weak_wrong_match_cannot_drag_year_back():
    from cratetag.lookup import Candidate, choose_year
    cands = [Candidate("MusicBrainz", "Wiz Khalifa", "Young Wild & Free", original_year="2011", score=0.97),
             Candidate("Discogs", "Various", "Young Wild & Free", release_year="2001", score=0.80)]
    assert choose_year(cands, Settings())[0] == "2011"


def test_genre_auto_vs_strict():
    from cratetag.lookup import Candidate, choose_genre
    cands = [Candidate("MusicBrainz", "Dolly Babe", "Pink Blush", original_year="2026", genres=[], score=0.95),
             Candidate("Discogs", "Dolly Babe", "Pink Blush", release_year="2026", genres=["Hip Hop"],
                       styles=["Trap"], score=0.90)]
    assert choose_genre(cands, Settings(genre_source="auto"))[0] == "Trap"
    assert choose_genre(cands, Settings(genre_source="discogs_genre"))[0] == "Hip-Hop"
    assert choose_genre(cands, Settings(genre_source="musicbrainz"))[0] == ""


def test_search_continues_until_each_source_matches(tmp_path):
    """MusicBrainz matching on the first try must not stop Discogs from trying the next variant."""
    from cratetag.tags import TrackInfo
    dq = []

    def mb(_u, p):
        return {"recordings": [{"id": "m1", "title": "Pink Blush Remix", "first-release-date": "2026",
                                "artist-credit": [{"name": "Dolly Babe"}], "releases": []}]}

    def ds(_u, p):
        dq.append((p.get("artist"), p.get("track"), p.get("type")))
        if p.get("artist") == "Dolly Babe" and p.get("type") == "master":
            return {"results": [{"type": "master", "id": 5, "title": "Dolly Babe - Pink Blush", "year": "2026",
                                 "genre": ["Hip Hop"], "style": ["Trap"], "uri": "/master/5"}]}
        return {"results": []}

    def dm(_u, _p):
        return {"year": 2026, "genres": ["Hip Hop"], "styles": ["Trap"], "artists": [{"name": "Dolly Babe"}],
                "tracklist": [{"title": "Pink Blush (Remix)", "duration": ""}]}

    routes = {"musicbrainz.org/ws/2/recording/m1": {"id": "m1"},
              "musicbrainz.org/ws/2/recording": mb,
              "api.discogs.com/database/search": ds, "api.discogs.com/masters/5": dm}
    info = TrackInfo(path=tmp_path / "x.mp3", artist="Dolly Babe & Megan Thee Stallion",
                     title="Pink Blush Remix (DJcity Intro - Clean) - 9A - 136", year="2026")
    p = Engine(Settings(discogs_token="t", genre_source="auto"), http=FakeHttp(routes)).lookup(info)
    assert p.year == "2026"
    assert p.genre == "Trap"
    assert any(a == "Dolly Babe" for a, _, _ in dq)


# ---------------------------------------------------------------- Serato crates

def _fake_serato(tmp_path):
    import os
    from cratetag.serato import write_crate
    home, vols = tmp_path / "home", tmp_path / "Volumes"
    drive = vols / "DJ DRIVE"
    (drive / "Music").mkdir(parents=True)
    (home / "Music").mkdir(parents=True)
    os.symlink("/", vols / "Macintosh HD")
    a = make_mp3(drive / "Music" / "Aventura - Obsesion.mp3")
    b = make_mp3(drive / "Music" / "Whitney Houston - Its Not Right.mp3")
    c = make_mp3(drive / "Music" / "Who Da Funk - Sting Me Red.mp3")
    (drive / "Music" / "Song.m4a").write_bytes(b"not really")
    sd = drive / "_Serato_" / "Subcrates"
    write_crate(sd / "#CASE CRATES#.crate", ["Music/Aventura - Obsesion.mp3"])
    write_crate(sd / "#CASE CRATES#%%092626 WEDDING.crate",
                ["Music/Whitney Houston - Its Not Right.mp3", "Music/Gone.mp3", "Music/Song.m4a",
                 "Music/Aventura - Obsesion.mp3"])
    write_crate(sd / "test.crate", ["Music/Who Da Funk - Sting Me Red.mp3"])
    write_crate(home / "Music" / "_Serato_" / "Subcrates" / "Internal.crate", [str(c).lstrip("/")])
    return home, vols, (a, b, c)


def test_find_libraries_and_crate_tracks(tmp_path):
    from cratetag.serato import crate_tracks, find_libraries, read_crate_paths
    home, vols, (a, b, c) = _fake_serato(tmp_path)
    libs = find_libraries(home, vols)
    assert [l.label for l in libs] == ["Macintosh HD", "DJ DRIVE"]
    ext = libs[1]
    assert ext.external and ext.root == vols / "DJ DRIVE"
    assert [cr.full_name for cr in ext.crates] == ["#CASE CRATES#", "#CASE CRATES# > 092626 WEDDING", "test"]
    wedding = ext.crates[1]
    assert crate_tracks(wedding)[0] == b
    parent = ext.crates[0]
    assert crate_tracks(parent) == [a]
    assert set(crate_tracks(parent, include_subcrates=True)) >= {a, b}
    assert crate_tracks(libs[0].crates[0]) == [c]
    assert read_crate_paths(wedding.file)[1] == "Music/Gone.mp3"


def test_crate_files_are_never_modified(tmp_path):
    import hashlib
    from cratetag.serato import crate_tracks, find_libraries
    home, vols, _ = _fake_serato(tmp_path)
    files = sorted((vols / "DJ DRIVE" / "_Serato_").rglob("*"))
    before = {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in files if f.is_file()}
    for lib in find_libraries(home, vols):
        for cr in lib.crates:
            crate_tracks(cr, include_subcrates=True)
    after = {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in files if f.is_file()}
    assert before == after


def test_serato_name_codes_order_merge_and_smart_crates(tmp_path):
    import os
    from cratetag.serato import build_tree, display_name, find_libraries, merged_order, write_crate
    assert display_name("␛␛2a␛␛2aMUST PLAY␛␛2a") == "**MUST PLAY*"
    home, vols = tmp_path / "home", tmp_path / "Volumes"
    (home / "Music").mkdir(parents=True)
    (vols / "Ext").mkdir(parents=True)
    os.symlink("/", vols / "OS")
    isd, esd = home / "Music/_Serato_", vols / "Ext/_Serato_"
    for k in ["Recorded", "OLD SCHOOL", "#CASE#", "SMART CRATES"]:
        write_crate(isd / f"Subcrates/{k}.crate", [])
    for k in ["MHS06%%CASE'S LIST", "102326 INTRO", "#CASE#", "#CASE#%%␛␛2aMUST␛␛2a"]:
        write_crate(esd / f"Subcrates/{k}.crate", [])
    (isd / "SmartCrates").mkdir()
    (isd / "SmartCrates/SMART CRATES≫≫40-59.scrate").write_bytes(b"")
    (isd / "neworder.pref").write_bytes(
        "[crate]Recorded\n[crate]OLD SCHOOL\n[crate]#CASE#\n[crate]SMART CRATES\n".encode("utf-16"))
    (esd / "neworder.pref").write_bytes(
        "[crate]MHS06\n[crate]MHS06%%CASE'S LIST\n[crate]102326 INTRO\n[crate]#CASE#\n"
        "[crate]#CASE#%%\u241b\u241b2aMUST\u241b\u241b2a\n".encode("utf-16"))
    libs = find_libraries(home, vols)
    # Larger order is the base; Mac-only crates slot in before the next shared crate
    assert merged_order(libs)[:6] == ["MHS06", "MHS06%%CASE'S LIST", "102326 INTRO", "Recorded", "OLD SCHOOL", "#CASE#"]
    roots = build_tree(libs)
    assert [r.name for r in roots] == ["MHS06", "102326 INTRO", "Recorded", "OLD SCHOOL", "#CASE#", "SMART CRATES"]
    assert [c.name for c in roots[0].children] == ["CASE'S LIST"] and not roots[0].members
    case = roots[4]
    assert [c.name for c in case.children] == ["*MUST*"] and case.drives == ["OS", "Ext"]
    smart = roots[5].children
    assert [(c.name, c.smart) for c in smart] == [("40-59", True)]


# ---------------------------------------------------------------- Deezer

def _deezer_routes(record_type="single"):
    def search(_u, p):
        q = p.get("q", "")
        if "all the small things" in q.lower():
            return {"data": [
                {"id": 1, "title": "All The Small Things", "duration": 168, "link": "https://www.deezer.com/track/1",
                 "artist": {"name": "blink-182"}, "album": {"id": 10, "title": "Enema of the State"}},
                {"id": 2, "title": "All The Small Things", "duration": 168,
                 "artist": {"name": "blink-182"}, "album": {"id": 20, "title": "Greatest Hits"}},
                {"id": 3, "title": "All The Small Things (Karaoke)", "duration": 170,
                 "artist": {"name": "Karaoke Stars"}, "album": {"id": 30, "title": "Karaoke"}}]}
        return {"data": []}

    albums = {
        "album/10": {"release_date": "1999-06-01", "record_type": record_type,
                     "genres": {"data": [{"name": "Rock"}, {"name": "Alternative"}]}},
        "album/20": {"release_date": "2005-11-01", "record_type": "compile", "genres": {"data": [{"name": "Rock"}]}},
        "album/30": {"release_date": "2015-01-01", "record_type": "album", "genres": {"data": [{"name": "Pop"}]}},
    }

    def album(u, _p):
        return next((v for k, v in albums.items() if u.endswith(k)), {"error": {"code": 800, "message": "no data"}})

    return {"api.deezer.com/search": search, "api.deezer.com/album/": album}


def test_deezer_years_genres_and_compilations(tmp_path):
    from cratetag.lookup import Deezer
    from cratetag.tags import TrackInfo
    dz = Deezer(FakeHttp(_deezer_routes()))
    cands = dz.search("blink-182", "All The Small Things")
    assert [c.year for c in cands] == ["1999", "", "2015"]  # the compilation's date is ignored
    assert cands[0].genres == ["Rock", "Alternative"] and cands[0].url.endswith("/track/1")

    # Deezer only (other sources off): year and genre come from Deezer
    s = Settings(use_musicbrainz=False, use_discogs=False, use_deezer=True)
    info = TrackInfo(path=tmp_path / "x.mp3", artist="Blink-182", title="All The Small Things (Deville Intro) - 8B - 149",
                     duration=240)
    p = Engine(s, http=FakeHttp(_deezer_routes())).lookup(info)
    assert (p.year, p.genre, p.source) == ("1999", "Rock", "Deezer")
    assert (p.search_artist, p.search_title) == ("Blink-182", "All The Small Things")
    # Year from / Genre from: Deezer as a specific source
    from cratetag.lookup import apply_choices
    apply_choices(p, Settings(year_source="deezer", genre_source="deezer"))
    assert (p.year, p.genre) == ("1999", "Rock")
    apply_choices(p, Settings(year_source="musicbrainz", genre_source="musicbrainz"))
    assert (p.year, p.genre) == ("", "")


def test_deezer_turned_off_is_not_called(tmp_path):
    from cratetag.tags import TrackInfo
    http = FakeHttp(_deezer_routes())
    s = Settings(use_musicbrainz=False, use_discogs=False, use_deezer=False)
    Engine(s, http=http).lookup(TrackInfo(path=tmp_path / "x.mp3", artist="blink-182", title="All The Small Things"))
    assert not any("deezer" in u for u, _ in http.calls)


def test_deezer_fills_genre_when_discogs_has_none(tmp_path):
    from cratetag.tags import TrackInfo
    routes = dict(ROUTES)
    routes.update(_deezer_routes())
    # MusicBrainz knows the song (year) but has no genre; Discogs is off
    routes["musicbrainz.org/ws/2/recording/"] = {}
    routes["musicbrainz.org/ws/2/recording"] = lambda u, p: {"recordings": [
        {"id": "m9", "title": "All The Small Things", "length": 168000, "first-release-date": "1999-06-01",
         "artist-credit": [{"name": "blink-182"}], "releases": []}]}
    p = Engine(Settings(use_discogs=False), http=FakeHttp(routes)).lookup(
        TrackInfo(path=tmp_path / "x.mp3", artist="blink-182", title="All The Small Things", duration=168))
    assert p.year == "1999" and p.genre == "Rock"
    assert "Deezer" in p.source


# ---------------------------------------------------------------- Apple iTunes

def _itunes_routes():
    def search(_u, p):
        term = p.get("term", "").lower()
        if "gasolina" not in term:
            return {"resultCount": 0, "results": []}
        return {"resultCount": 3, "results": [
            {"wrapperType": "track", "kind": "song", "trackId": 1, "artistName": "Daddy Yankee", "trackName": "Gasolina",
             "collectionName": "Gasolina - Single", "releaseDate": "2004-07-13T07:00:00Z",
             "primaryGenreName": "Urbano latino", "trackTimeMillis": 192000,
             "trackViewUrl": "https://music.apple.com/us/album/gasolina/1?i=1"},
            {"wrapperType": "track", "kind": "song", "trackId": 2, "artistName": "Daddy Yankee", "trackName": "Gasolina",
             "collectionName": "Reggaeton Hits", "collectionArtistName": "Various Artists",
             "releaseDate": "2019-01-01T08:00:00Z", "primaryGenreName": "Latin", "trackTimeMillis": 192000},
            {"wrapperType": "track", "kind": "music-video", "trackId": 3, "artistName": "Daddy Yankee",
             "trackName": "Gasolina (Video)"}]}
    return {"itunes.apple.com/search": search}


def test_itunes_years_genres_and_compilations():
    from cratetag.lookup import ITunes
    cands = ITunes(FakeHttp(_itunes_routes())).search("Daddy Yankee", "Gasolina")
    assert len(cands) == 2  # the music video is skipped
    single, comp = cands
    assert single.year == "2004" and single.original_year == "2004" and single.genres == ["Latin Urban"]
    assert comp.year == "" and comp.genres == ["Latin"]  # a Various Artists compilation's date is ignored
    assert single.source == "iTunes" and single.url.startswith("https://music.apple.com")


def test_itunes_is_a_backup_only_when_needed(tmp_path):
    from cratetag.lookup import apply_choices
    from cratetag.tags import TrackInfo
    info = TrackInfo(path=tmp_path / "x.mp3", artist="Daddy Yankee", title="Gasolina (Intro - Dirty)", duration=200)

    # Only iTunes can find this song: it fills both year and genre
    http = FakeHttp(_itunes_routes())
    p = Engine(Settings(use_musicbrainz=False, use_discogs=False, use_deezer=False), http=http).lookup(info)
    assert (p.year, p.genre, p.source) == ("2004", "Latin Urban", "iTunes")
    assert sum("itunes" in u for u, _ in http.calls) == 1

    # Deezer already found year and genre: Apple isn't asked at all
    routes = _deezer_routes()
    routes.update(_itunes_routes())
    http = FakeHttp(routes)
    blink = TrackInfo(path=tmp_path / "b.mp3", artist="blink-182", title="All The Small Things", duration=168)
    p = Engine(Settings(use_musicbrainz=False, use_discogs=False), http=http).lookup(blink)
    assert (p.year, p.genre) == ("1999", "Rock")
    assert not any("itunes" in u for u, _ in http.calls)

    # Choosing Apple iTunes as the source asks it even when the others found everything
    http = FakeHttp(_itunes_routes())
    s = Settings(use_musicbrainz=False, use_discogs=False, use_deezer=False, year_source="itunes",
                 genre_source="itunes")
    p = Engine(s, http=http).lookup(info)
    assert (p.year, p.genre) == ("2004", "Latin Urban")
    apply_choices(p, Settings(year_source="deezer", genre_source="deezer"))
    assert (p.year, p.genre) == ("", "")

    # Turned off in Settings: never asked
    http = FakeHttp(_itunes_routes())
    Engine(Settings(use_musicbrainz=False, use_discogs=False, use_deezer=False, use_itunes=False),
           http=http).lookup(info)
    assert not any("itunes" in u for u, _ in http.calls)


# ---------------------------------------------------------------- updating from GitHub

def _fake_project(d, version):
    (d / "cratetag").mkdir(parents=True, exist_ok=True)
    (d / "cratetag" / "__init__.py").write_text(f'__version__ = "{version}"\nAPP_NAME = "CrateTag"\n')
    (d / "build_mac.sh").write_text('#!/bin/bash\necho "fake build of $(sed -n 1p cratetag/__init__.py)"\n')
    (d / "build_mac.sh").chmod(0o755)
    (d / ".gitignore").write_text(".venv/\n")


def test_updater_pulls_latest_from_github_into_git_folder(tmp_path):
    import os
    import shutil
    import subprocess
    if not shutil.which("git"):
        pytest.skip("git not installed")
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "CRATETAG_NO_LAUNCH": "1", "HOME": str(tmp_path)}

    def git(*a, cwd):
        subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True)

    # "GitHub": a repository with 1.0, then 1.1 pushed later
    remote, work = tmp_path / "remote.git", tmp_path / "author"
    git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    work.mkdir()
    _fake_project(work, "1.0")
    shutil.copy(root / "Update CrateTag.command", work / "Update CrateTag.command")
    git("init", "-b", "main", cwd=work)
    git("add", ".", cwd=work)
    git("commit", "-m", "1.0", cwd=work)
    git("remote", "add", "origin", str(remote), cwd=work)
    git("push", "-u", "origin", "main", cwd=work)

    # Your Mac: a clone at 1.0, with a private environment and a stray local edit
    mine = tmp_path / "Downloads" / "CrateTag"
    mine.parent.mkdir()
    git("clone", str(remote), str(mine), cwd=tmp_path)
    (mine / ".venv").mkdir()
    (mine / ".venv" / "keep.txt").write_text("python env")
    (mine / "notes.txt").write_text("local")

    (work / "cratetag" / "__init__.py").write_text('__version__ = "1.1"\nAPP_NAME = "CrateTag"\n')
    git("commit", "-am", "1.1", cwd=work)
    git("push", cwd=work)

    r = subprocess.run(["bash", str(mine / "Update CrateTag.command"), "--github"], env=env,
                       capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "New version: 1.1" in out and 'fake build of __version__ = "1.1"' in out
    assert '"1.1"' in (mine / "cratetag" / "__init__.py").read_text()
    assert (mine / ".venv" / "keep.txt").exists()          # environment left alone
    stash = subprocess.run(["git", "stash", "list"], cwd=mine, capture_output=True, text=True).stdout
    assert "before CrateTag update" in stash                 # local edit kept safely in a stash
    log = subprocess.run(["git", "log", "--oneline", "before-update"], cwd=mine, capture_output=True, text=True).stdout
    assert "1.0" in log                                      # the old version is kept in a branch


def test_update_check_prefers_github_api_then_raw():
    from cratetag import updates
    calls = []

    def get_ok(url):
        calls.append(url)
        return '__version__ = "1.2.3"\n'
    assert updates.latest_version(get=get_ok) == "1.2.3" and calls == [updates.API_URL]

    calls.clear()

    def api_busy(url):
        calls.append(url)
        if url == updates.API_URL:
            raise RuntimeError("403 rate limited")
        return '__version__ = "1.2.4"\n'
    assert updates.latest_version(get=api_busy) == "1.2.4" and calls == [updates.API_URL, updates.VERSION_URL]

    def offline(url):
        raise RuntimeError("no network")
    with pytest.raises(OSError):
        updates.latest_version(get=offline)
