"""Look up release year and genre from MusicBrainz, Discogs and (optionally) AcoustID."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

from . import APP_NAME, __version__
from .config import Settings
from .parsing import (artist_similarity, clean_title, normalize, parse_filename, primary_artist,
                      search_attempts, title_similarity)
from .tags import TrackInfo

MB_ROOT = "https://musicbrainz.org/ws/2"
DISCOGS_ROOT = "https://api.discogs.com"
ACOUSTID_ROOT = "https://api.acoustid.org/v2/lookup"


# --------------------------------------------------------------------------- data

@dataclass
class Candidate:
    source: str
    artist: str
    title: str
    original_year: str = ""
    release_year: str = ""
    genres: list[str] = field(default_factory=list)   # broad (Discogs genre / MusicBrainz genres)
    styles: list[str] = field(default_factory=list)   # specific (Discogs styles)
    duration: float | None = None
    score: float = 0.0
    url: str = ""
    ref: str = ""  # source-specific id

    @property
    def year(self) -> str:
        return self.original_year or self.release_year


@dataclass
class Proposal:
    year: str = ""
    genre: str = ""
    confidence: int = 0
    source: str = ""
    year_source: str = ""
    genre_source: str = ""
    note: str = ""
    search_artist: str = ""
    search_title: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    error: str = ""
    fresh_info: object = None  # the file's real tags, when the row was listed from Serato's library


# --------------------------------------------------------------------------- http

class LookupError_(Exception):
    pass


class Http:
    """requests wrapper with per-host rate limiting and retry on 429/503."""

    INTERVALS = {
        "musicbrainz.org": 1.1,     # MusicBrainz allows ~1 request/second
        "api.discogs.com": 1.05,    # 60/minute with a token
        "api.acoustid.org": 0.34,   # 3/second
        "api.deezer.com": 0.12,     # Deezer allows 50 requests per 5 seconds
        "itunes.apple.com": 3.1,    # Apple allows about 20 searches a minute
    }

    def __init__(self, contact: str = "", timeout: float = 15.0):
        contact = contact.strip() or "personal use"
        self.session = requests.Session()
        self.session.headers["User-Agent"] = f"{APP_NAME}/{__version__} ( {contact} )"
        self.timeout = timeout
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.cancelled = False

    def _wait(self, host: str) -> None:
        gap = self.INTERVALS.get(host, 0.0)
        with self._lock:
            now = time.monotonic()
            ready = self._last.get(host, 0.0) + gap
            if ready > now:
                time.sleep(ready - now)
            self._last[host] = time.monotonic()

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> dict:
        host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
        for attempt in range(4):
            if self.cancelled:
                raise LookupError_("Cancelled")
            self._wait(host)
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt == 3:
                    raise LookupError_(f"Network error contacting {host}: {e}") from e
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (429, 503):
                wait = float(r.headers.get("Retry-After", 2 * (attempt + 1)))
                time.sleep(min(wait, 30))
                continue
            if r.status_code == 401:
                raise LookupError_(f"{host} rejected the credentials (check your token or key)")
            if r.status_code == 404:
                return {}
            if r.status_code == 403 and host == "itunes.apple.com":
                raise LookupError_("Apple is limiting searches right now; try again in a minute")
            if r.status_code >= 400:
                raise LookupError_(f"{host} returned HTTP {r.status_code}")
            try:
                return r.json()
            except ValueError as e:
                raise LookupError_(f"{host} sent an unreadable response") from e
        raise LookupError_(f"{host} is rate limiting; try again shortly")


# --------------------------------------------------------------------------- scoring

def _year(s: str | int | None) -> str:
    m = re.search(r"\d{4}", str(s or ""))
    return m.group(0) if m and m.group(0) != "0000" else ""


def _duration_score(a: float | None, b: float | None) -> float | None:
    if not a or not b:
        return None
    diff = abs(a - b)
    if diff <= 3:
        return 1.0
    if diff >= 25:
        return 0.0
    return 1.0 - (diff - 3) / 22


def score_candidate(c: Candidate, artist: str, title: str, duration: float | None) -> float:
    t = title_similarity(title, c.title) if title else 0.5
    a = artist_similarity(artist, c.artist) if artist else 0.5
    d = _duration_score(duration, c.duration)
    if d is None:
        s = 0.55 * t + 0.45 * a
    else:
        s = 0.47 * t + 0.38 * a + 0.15 * d
    # A wrong artist should sink the match even if the title is identical.
    if artist and a < 0.5:
        s *= 0.6
    return round(s, 4)


# --------------------------------------------------------------------------- MusicBrainz

_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def _lucene(s: str) -> str:
    return _LUCENE_SPECIAL.sub(r"\\\1", s)


def _mb_artist(credit: list) -> str:
    out = ""
    for part in credit or []:
        out += part.get("name") or part.get("artist", {}).get("name", "")
        out += part.get("joinphrase", "")
    return out.strip()


def _mb_genres(obj: dict) -> list[str]:
    items = obj.get("genres") or []
    if not items:
        # Fall back to user tags that look like genres (count-weighted)
        items = [t for t in (obj.get("tags") or []) if t.get("count", 0) > 0]
    items = sorted(items, key=lambda g: -int(g.get("count", 0)))
    return [g["name"].title() if g["name"].islower() else g["name"] for g in items if g.get("name")]


class MusicBrainz:
    def __init__(self, http: Http):
        self.http = http

    def search(self, artist: str, title: str, limit: int = 10) -> list[Candidate]:
        if not title:
            return []
        q = f'recording:"{_lucene(title)}"'
        if artist:
            q += f' AND artist:"{_lucene(artist)}"'
        data = self.http.get_json(f"{MB_ROOT}/recording", {"query": q, "fmt": "json", "limit": limit})
        if not data.get("recordings"):
            # Exact phrase found nothing; retry with loose word matching ("Lets Em Know" vs "LET 'EM KNOW")
            words = " ".join(_lucene(w) for w in re.findall(r"[\w$']+", title))
            if words:
                q = f"recording:({words})"
                if artist:
                    q += f' AND artist:"{_lucene(artist)}"'
                data = self.http.get_json(f"{MB_ROOT}/recording", {"query": q, "fmt": "json", "limit": limit})
        out = []
        for rec in data.get("recordings", []):
            c = self._from_recording(rec)
            out.append(c)
        return out

    def _from_recording(self, rec: dict) -> Candidate:
        years = [_year(r.get("date")) for r in rec.get("releases", []) if _year(r.get("date"))]
        first = _year(rec.get("first-release-date")) or (min(years) if years else "")
        length = rec.get("length")
        return Candidate(
            source="MusicBrainz",
            artist=_mb_artist(rec.get("artist-credit", [])),
            title=rec.get("title", ""),
            original_year=first,
            release_year=first,
            genres=_mb_genres(rec),
            duration=(length / 1000.0) if length else None,
            url=f"https://musicbrainz.org/recording/{rec.get('id', '')}",
            ref=rec.get("id", ""),
        )

    def lookup_recording(self, mbid: str) -> Candidate | None:
        data = self.http.get_json(f"{MB_ROOT}/recording/{mbid}",
                                  {"inc": "artist-credits+releases+genres+tags", "fmt": "json"})
        if not data:
            return None
        return self._from_recording(data)

    def fill_genres(self, c: Candidate) -> None:
        """Recording genres are often empty; fall back to release group, then artist."""
        if c.genres or not c.ref:
            return
        data = self.http.get_json(f"{MB_ROOT}/recording/{c.ref}",
                                  {"inc": "genres+tags+release-groups+artists", "fmt": "json"})
        if not data:
            return
        c.genres = _mb_genres(data)
        if c.genres:
            return
        for rel in data.get("releases", [])[:3]:
            rg = (rel.get("release-group") or {}).get("id")
            if not rg:
                continue
            rgd = self.http.get_json(f"{MB_ROOT}/release-group/{rg}", {"inc": "genres+tags", "fmt": "json"})
            c.genres = _mb_genres(rgd)
            if c.genres:
                return
        for credit in data.get("artist-credit", [])[:1]:
            aid = (credit.get("artist") or {}).get("id")
            if aid:
                ad = self.http.get_json(f"{MB_ROOT}/artist/{aid}", {"inc": "genres+tags", "fmt": "json"})
                c.genres = _mb_genres(ad)


# --------------------------------------------------------------------------- Discogs

def _discogs_duration(s: str) -> float | None:
    m = re.fullmatch(r"\s*(?:(\d+):)?(\d+):(\d{2})\s*", s or "")
    if not m:
        return None
    h, mnt, sec = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + int(sec)


def _discogs_artist(s: str) -> str:
    # Discogs disambiguates duplicate names as "Artist (2)"; drop that and trailing '*'.
    return re.sub(r"\s*\(\d+\)$", "", (s or "").strip()).rstrip("*").strip()


class Discogs:
    def __init__(self, http: Http, token: str):
        self.http = http
        self.token = token.strip()

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Discogs token={self.token}"}

    def search(self, artist: str, title: str, verify: int = 3) -> list[Candidate]:
        if not self.token or not title:
            return []
        results: list[dict] = []
        for kind in ("master", "release"):
            params = {"track": title, "type": kind, "per_page": 10}
            if artist:
                params["artist"] = artist
            data = self.http.get_json(f"{DISCOGS_ROOT}/database/search", params, self.headers)
            results = data.get("results", [])
            if results:
                break

        out: list[Candidate] = []
        for i, r in enumerate(results):
            raw = r.get("title", "")
            r_artist = _discogs_artist(raw.split(" - ", 1)[0]) if " - " in raw else ""
            c = Candidate(
                source="Discogs",
                artist=r_artist,
                title=title,  # Search matched on track; verified against the tracklist below
                release_year=_year(r.get("year")),
                original_year=_year(r.get("year")) if r.get("type") == "master" else "",
                genres=list(r.get("genre") or []),
                styles=list(r.get("style") or []),
                url="https://www.discogs.com" + r.get("uri", "") if r.get("uri", "").startswith("/") else r.get("uri", ""),
                ref=f"{r.get('type')}/{r.get('id')}",
            )
            if i < verify:
                self._verify(c, title, r)
            else:
                c.title = ""  # unverified: don't give it full title credit
            out.append(c)
        return out

    def _verify(self, c: Candidate, title: str, r: dict) -> None:
        kind, rid = r.get("type"), r.get("id")
        path = "masters" if kind == "master" else "releases"
        d = self.http.get_json(f"{DISCOGS_ROOT}/{path}/{rid}", None, self.headers)
        if not d:
            return
        best, best_track = 0.0, None
        for t in d.get("tracklist", []):
            s = title_similarity(title, t.get("title", ""))
            if s > best:
                best, best_track = s, t
        if best_track is not None:
            c.title = best_track.get("title", "")
            c.duration = _discogs_duration(best_track.get("duration", ""))
        else:
            c.title = ""
        if d.get("artists"):
            c.artist = " ".join(
                _discogs_artist(a.get("name", "")) + (f" {a['join']}" if a.get("join") and a["join"] != "," else "")
                for a in d["artists"]
            ).strip()
        y = _year(d.get("year"))
        if y:
            if kind == "master":
                c.original_year = y
            c.release_year = y
        c.genres = list(d.get("genres") or c.genres)
        c.styles = list(d.get("styles") or c.styles)


# --------------------------------------------------------------------------- Deezer

DEEZER_ROOT = "https://api.deezer.com"
# Deezer's genre names, mapped to the common names the other sources use (your genre aliases still apply)
DEEZER_GENRES = {"Rap/Hip Hop": "Hip Hop", "Latin Music": "Latin", "Electro": "Electronic",
                 "Films/Games": "Soundtrack", "Alternative": "Alternative"}


class Deezer:
    """Deezer's public catalog: free, no account or key. Strong on release dates for recent music;
    genres are broad and come from the album."""

    def __init__(self, http: Http):
        self.http = http
        self._albums: dict[int, dict] = {}

    def _get(self, url: str, params: dict | None = None) -> dict:
        for attempt in range(3):
            data = self.http.get_json(url, params)
            err = data.get("error") if isinstance(data, dict) else None
            if not err:
                return data
            if err.get("code") == 4:  # quota exceeded: back off and retry
                time.sleep(2 * (attempt + 1))
                continue
            if err.get("code") in (800, 801):  # no data / not found
                return {}
            raise LookupError_(f"Deezer: {err.get('message', 'error')}")
        raise LookupError_("Deezer is rate limiting; try again shortly")

    def album(self, album_id: int) -> dict:
        if album_id not in self._albums:
            self._albums[album_id] = self._get(f"{DEEZER_ROOT}/album/{album_id}")
        return self._albums[album_id]

    def search(self, artist: str, title: str, verify: int = 3) -> list[Candidate]:
        if not title:
            return []
        q = f'track:"{title}"' + (f' artist:"{artist}"' if artist else "")
        data = self._get(f"{DEEZER_ROOT}/search", {"q": q, "limit": 10})
        results = data.get("data", []) if data else []
        if not results:  # strict search found nothing: try plain words
            data = self._get(f"{DEEZER_ROOT}/search", {"q": f"{artist} {title}".strip(), "limit": 10})
            results = data.get("data", []) if data else []
        out: list[Candidate] = []
        for i, r in enumerate(results):
            alb = r.get("album") or {}
            c = Candidate(
                source="Deezer",
                artist=(r.get("artist") or {}).get("name", ""),
                title=r.get("title") or r.get("title_short", ""),
                duration=float(r["duration"]) if r.get("duration") else None,
                url=r.get("link") or f"https://www.deezer.com/track/{r.get('id', '')}",
                ref=f"deezer/{r.get('id', '')}",
            )
            if i < verify and alb.get("id"):
                try:
                    a = self.album(alb["id"])
                except LookupError_ as e:
                    if str(e) == "Cancelled":
                        raise
                    a = {}
                y = _year(a.get("release_date"))
                # A compilation's date is when it was compiled, not when the song came out
                if y and a.get("record_type") != "compile":
                    c.release_year = y
                    if a.get("record_type") in ("single", "ep"):
                        c.original_year = y
                c.genres = [DEEZER_GENRES.get(g.get("name", ""), g.get("name", ""))
                            for g in (a.get("genres") or {}).get("data", []) if g.get("name")]
            out.append(c)
        return out


# --------------------------------------------------------------------------- Apple iTunes

ITUNES_SEARCH = "https://itunes.apple.com/search"
# Apple's genre names, mapped to the names the other sources use (your genre aliases still apply)
ITUNES_GENRES = {"Hip-Hop/Rap": "Hip Hop", "Hip-Hop": "Hip Hop", "Rap": "Hip Hop", "R&B/Soul": "R&B",
                 "Urbano latino": "Latin Urban", "Urbano Latino": "Latin Urban", "Música Tropical": "Tropical",
                 "Pop Latino": "Latin Pop", "Electronic": "Electronic", "Dance": "Dance"}


class ITunes:
    """Apple's iTunes Search API: free, no account or key, about 20 searches a minute.
    CrateTag uses it as a backup when the other sources miss a year or genre."""

    def __init__(self, http: Http):
        self.http = http

    def search(self, artist: str, title: str) -> list[Candidate]:
        if not title:
            return []
        term = f"{artist} {title}".strip()
        data = self.http.get_json(ITUNES_SEARCH, {"term": term, "media": "music", "entity": "song", "limit": 10})
        out: list[Candidate] = []
        for r in (data or {}).get("results", []):
            if r.get("wrapperType", "track") != "track" or r.get("kind", "song") != "song":
                continue
            c = Candidate(
                source="iTunes",
                artist=r.get("artistName", ""),
                title=r.get("trackName", ""),
                duration=(r["trackTimeMillis"] / 1000) if r.get("trackTimeMillis") else None,
                url=r.get("trackViewUrl", ""),
                ref=f"itunes/{r.get('trackId', '')}",
            )
            album = r.get("collectionName") or ""
            various = (r.get("collectionArtistName") or "").lower() in ("various artists", "varios artistas")
            y = _year(r.get("releaseDate"))
            # A compilation's date is when it was compiled, not when the song came out
            if y and not various:
                c.release_year = y
                if re.search(r"\s-\s(Single|EP)$", album):
                    c.original_year = y
            g = r.get("primaryGenreName") or ""
            if g and g.lower() != "music":
                c.genres = [ITUNES_GENRES.get(g, g)]
            out.append(c)
        return out


# --------------------------------------------------------------------------- AcoustID

def find_fpcalc(configured: str = "") -> str | None:
    candidates = []
    if configured:
        candidates.append(configured)
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidates += [str(base / "fpcalc"), str(Path(sys.executable).parent / "fpcalc")]
    w = shutil.which("fpcalc")
    if w:
        candidates.append(w)
    candidates += ["/opt/homebrew/bin/fpcalc", "/usr/local/bin/fpcalc"]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


class AcoustID:
    def __init__(self, http: Http, key: str, fpcalc: str):
        self.http = http
        self.key = key.strip()
        self.fpcalc = fpcalc

    def fingerprint(self, path: Path) -> tuple[float, str]:
        proc = subprocess.run([self.fpcalc, "-json", str(path)], capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise LookupError_(f"fpcalc failed: {proc.stderr.strip()[:200]}")
        data = json.loads(proc.stdout)
        return float(data["duration"]), data["fingerprint"]

    def identify(self, path: Path) -> list[tuple[float, str, str, str]]:
        """Return [(score, recording_mbid, artist, title)] best first."""
        duration, fp = self.fingerprint(path)
        data = self.http.get_json(ACOUSTID_ROOT, {
            "client": self.key, "duration": int(duration), "fingerprint": fp,
            "meta": "recordings", "format": "json",
        })
        if data.get("status") != "ok":
            msg = (data.get("error") or {}).get("message", "unknown error")
            raise LookupError_(f"AcoustID: {msg}")
        out = []
        for res in data.get("results", []):
            for rec in res.get("recordings", []) or []:
                artist = " ".join(a.get("name", "") for a in rec.get("artists", []))
                out.append((float(res.get("score", 0)), rec.get("id", ""), artist, rec.get("title", "")))
        out.sort(key=lambda x: -x[0])
        return out


# --------------------------------------------------------------------------- engine

def _pick_genre(cands: list[Candidate], settings: Settings) -> tuple[str, str]:
    """Return (genre_text, source_label).

    "auto" tries Discogs style, then Discogs genre, then MusicBrainz. A specific source
    is strict: if it has nothing, the genre is left blank so you know where it came from.
    """
    auto = [("Discogs", "styles"), ("Discogs", "genres"), ("MusicBrainz", "genres"), ("Deezer", "genres"),
            ("iTunes", "genres")]
    order = {
        "auto": auto,
        "discogs_style": [("Discogs", "styles")],
        "discogs_genre": [("Discogs", "genres")],
        "musicbrainz": [("MusicBrainz", "genres")],
        "deezer": [("Deezer", "genres")],
        "itunes": [("iTunes", "genres")],
    }.get(settings.genre_source, auto)

    for source, attr in order:
        for c in cands:  # already sorted best first
            if not c.source.startswith(source):
                continue
            values = [settings.genre_aliases.get(v, v) for v in getattr(c, attr) if v]
            seen, uniq = set(), []
            for v in values:
                if v.lower() not in seen:
                    seen.add(v.lower())
                    uniq.append(v)
            if uniq:
                n = max(1, int(settings.max_genres or 1))
                return settings.genre_separator.join(uniq[:n]), c.source
    return "", ""


STRONG = 0.62  # candidates below this are ignored for proposals


def _from_source(c: Candidate, pref: str) -> bool:
    return c.source.lower().startswith(pref.lower())


def choose_year(cands: list[Candidate], settings: Settings) -> tuple[str, str]:
    """Pick the year from candidates, honouring the user's preferred source and year mode."""
    strong = sorted([c for c in cands if c.score >= STRONG], key=lambda c: -c.score)
    pref = getattr(settings, "year_source", "auto") or "auto"
    if pref != "auto":
        # Strict: a specific source only uses that source's matches.
        strong = [c for c in strong if _from_source(c, pref)]
    dated = [c for c in strong if c.year]
    if not dated:
        return "", ""
    best = dated[0]
    if settings.year_mode == "best_match":
        return best.year, best.source
    # Earliest year among matches nearly as good as the best one. Kept tight so one
    # weaker, wrong match (a cover, a mislabeled compilation) can't drag the year back.
    close = [c for c in dated if c.score >= best.score - 0.08]
    pick = min(close, key=lambda c: c.year)
    return pick.year, pick.source


def choose_genre(cands: list[Candidate], settings: Settings) -> tuple[str, str]:
    strong = sorted([c for c in cands if c.score >= STRONG], key=lambda c: -c.score)
    return _pick_genre(strong, settings)


def _short(src: str) -> str:
    return "MusicBrainz" if src.startswith("MusicBrainz") else src.split(" ")[0]


def apply_choices(prop: Proposal, settings: Settings) -> None:
    """(Re)compute year and genre on a proposal from its stored candidates."""
    prop.year, ys = choose_year(prop.candidates, settings)
    prop.genre, gs = choose_genre(prop.candidates, settings)
    prop.year_source, prop.genre_source = ys, gs
    if ys and gs and _short(ys) == _short(gs):
        prop.source = _short(ys)
    else:
        parts = []
        if ys:
            parts.append(f"Year: {_short(ys)}")
        if gs:
            parts.append(f"Genre: {_short(gs)}")
        prop.source = " · ".join(parts)


class Engine:
    """Runs lookups for one track at a time. Safe to reuse across tracks."""

    STRONG = STRONG  # candidates below this are ignored for proposals

    def __init__(self, settings: Settings, http: Http | None = None,
                 log: Callable[[str], None] | None = None):
        self.settings = settings
        self.http = http or Http(settings.contact_email)
        self.log = log or (lambda m: None)
        self.mb = MusicBrainz(self.http) if settings.use_musicbrainz else None
        self.discogs = Discogs(self.http, settings.discogs_token) if (settings.use_discogs and settings.discogs_token) else None
        self.deezer = Deezer(self.http) if getattr(settings, "use_deezer", True) else None
        self.itunes = ITunes(self.http) if getattr(settings, "use_itunes", True) else None
        fp = find_fpcalc(settings.fpcalc_path) if settings.use_acoustid else None
        self.acoustid = AcoustID(self.http, settings.acoustid_key, fp) if (fp and settings.acoustid_key) else None

    def cancel(self) -> None:
        self.http.cancelled = True

    MAX_ATTEMPTS = 4

    def _ignore(self) -> list[str]:
        return [w.strip() for w in (self.settings.ignore_words or "").split(",") if w.strip()]

    def lookup(self, info: TrackInfo) -> Proposal:
        ignore = self._ignore()
        override_a = (info.extra or {}).get("search_artist")
        override_t = (info.extra or {}).get("search_title")
        if override_t:
            attempts = [((override_a or "").strip(), override_t.strip())]
        else:
            attempts = search_attempts(info.artist, info.title, info.path, ignore)[: self.MAX_ATTEMPTS]

        # Score artists against the full credited artist, even on title-only fallback searches,
        # so a same-named song by someone else doesn't win.
        f_artist, _ = parse_filename(info.path, ignore)
        score_artist = primary_artist((override_a if override_t else None) or info.artist or f_artist)

        # DJ edits (intros, extended mixes) run longer than the original; don't penalise length then.
        raw_title = info.title or ""
        is_edit = bool(raw_title) and normalize(clean_title(raw_title, ignore=ignore)) != normalize(raw_title)
        duration = None if is_edit else (info.duration or None)

        artist, title = attempts[0] if attempts else ("", "")
        prop = Proposal(search_artist=artist, search_title=title)
        cands: list[Candidate] = []
        errors: list[str] = []

        # 1. Audio fingerprint: identifies the song even with a useless filename.
        if self.acoustid and not override_t:
            try:
                hits = self.acoustid.identify(info.path)
                if hits and hits[0][0] >= 0.75:
                    score, mbid, a_artist, a_title = hits[0]
                    if a_title:
                        attempts.insert(0, (primary_artist(a_artist or artist), a_title))
                        score_artist = primary_artist(a_artist) or score_artist
                    if mbid and self.mb:
                        c = self.mb.lookup_recording(mbid)
                        if c:
                            c.source = "MusicBrainz (AcoustID)"
                            c.score = round(min(1.0, 0.5 + score / 2), 4)
                            cands.append(c)
            except LookupError_ as e:
                if str(e) == "Cancelled":
                    raise
                errors.append(str(e))
            except Exception as e:  # noqa: BLE001
                errors.append(f"Fingerprint: {e}")

        if not attempts:
            prop.error = "No title in tags or filename to search with"
            return prop

        # 2. Text searches: try each cleaned-up variant until one gives a confident match.
        seen_refs: set[str] = {c.ref for c in cands}
        searched_with: dict[int, tuple[str, str]] = {}  # candidate -> the search that found it
        for artist, title in attempts:
            prop.search_artist, prop.search_title = artist, title
            for name, client in (("MusicBrainz", self.mb), ("Discogs", self.discogs), ("Deezer", self.deezer)):
                if client is None:
                    continue
                try:
                    found = client.search(artist, title)
                except LookupError_ as e:
                    if str(e) == "Cancelled":
                        raise
                    if f"{name}: {e}" not in errors:
                        errors.append(f"{name}: {e}")
                    continue
                for c in found:
                    if c.ref and c.ref in seen_refs:
                        continue
                    seen_refs.add(c.ref)
                    c.score = score_candidate(c, score_artist, title, duration)
                    cands.append(c)
                    searched_with[id(c)] = (artist, title)
            # Keep trying search variants until every enabled source has a confident match,
            # so switching Year/Genre source later has real data from each one.
            enabled = [n for n, cl in (("MusicBrainz", self.mb), ("Discogs", self.discogs), ("Deezer", self.deezer))
                       if cl is not None]
            if all(any(c.score >= self.STRONG and c.source.startswith(n) for c in cands) for n in enabled):
                break

        # 3. Apple iTunes, as a backup: only when the others left the year or genre empty (or you chose
        #    iTunes as the source). It allows about 20 searches a minute, so it isn't asked every time.
        if self.itunes is not None:
            want = ("itunes" in (getattr(self.settings, "year_source", ""), getattr(self.settings, "genre_source", "")))
            if not want:
                y, _ = choose_year(cands, self.settings)
                g, _ = choose_genre(cands, self.settings)
                if not g and self.mb:  # MusicBrainz genres are fetched separately; check those first
                    mb_best = max((c for c in cands if c.score >= self.STRONG and c.source.startswith("MusicBrainz")),
                                  key=lambda c: c.score, default=None)
                    if mb_best is not None and not mb_best.genres:
                        try:
                            self.mb.fill_genres(mb_best)
                        except LookupError_ as e:
                            if str(e) == "Cancelled":
                                raise
                        g, _ = choose_genre(cands, self.settings)
                want = not y or not g
            if want:
                best_now = max((c for c in cands if c.score >= self.STRONG), key=lambda c: c.score, default=None)
                tries = [searched_with[id(best_now)]] if best_now is not None and id(best_now) in searched_with else []
                tries += [a for a in attempts if a not in tries]
                for artist, title in tries[:2]:
                    try:
                        found = self.itunes.search(artist, title)
                    except LookupError_ as e:
                        if str(e) == "Cancelled":
                            raise
                        errors.append(f"iTunes: {e}")
                        break
                    hit = False
                    for c in found:
                        if c.ref and c.ref in seen_refs:
                            continue
                        seen_refs.add(c.ref)
                        c.score = score_candidate(c, score_artist, title, duration)
                        cands.append(c)
                        searched_with[id(c)] = (artist, title)
                        hit = hit or c.score >= self.STRONG
                    if hit:
                        break

        cands.sort(key=lambda c: -c.score)
        strong = [c for c in cands if c.score >= self.STRONG]
        prop.candidates = cands[:15]

        if not strong:
            prop.error = "; ".join(errors) if errors else ""
            prop.note = "No confident match" if cands else "No results"
            return prop

        best = strong[0]
        # Show the search that actually found the best match, not just the last one tried
        if id(best) in searched_with:
            prop.search_artist, prop.search_title = searched_with[id(best)]

        # Make sure the best MusicBrainz candidate has genres if MB might be used for genre.
        if self.mb:
            mb_best = next((c for c in strong if c.source.startswith("MusicBrainz")), None)
            if mb_best and not mb_best.genres:
                try:
                    self.mb.fill_genres(mb_best)
                except LookupError_ as e:
                    if str(e) == "Cancelled":
                        raise
                    errors.append(f"MusicBrainz genres: {e}")

        apply_choices(prop, self.settings)
        close = [c for c in strong if c.score >= best.score - 0.15]

        # Confidence: best score, nudged by agreement between sources on the year.
        conf = best.score
        years_by_source: dict[str, set[str]] = {}
        for c in close:
            if c.year:
                years_by_source.setdefault(c.source.split(" ")[0], set()).add(c.year)
        notes = []
        if len(years_by_source) >= 2:
            sets = list(years_by_source.values())
            if set.intersection(*sets):
                conf = min(1.0, conf + 0.05)
                notes.append("sources agree on year")
            else:
                notes.append("sources differ on year")
        prop.confidence = int(round(conf * 100))
        prop.note = ", ".join(notes)
        if errors:
            prop.note = (prop.note + "; " if prop.note else "") + "; ".join(errors)
        return prop
