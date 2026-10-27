"""Turn messy filenames and tags into clean search terms, and compare strings.

DJ-pool titles pack a lot of extra information around the real song name, e.g.

    LET 'EM KNOW - KidCutUp Clap Intro (Dirty) - 11A - 84
    Janice STFU - Tall Boys Remix (Intro - Clean) - 1A - 130
    Pop Dat Thang x Pop That (Scooter Blend / Clean) - 10B - 138

Everything except "LET 'EM KNOW", "Janice STFU" and "Pop Dat Thang" is noise
for a release-year/genre lookup, so it is removed before searching.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

# Words that mark a bracket or " - " segment as an edit/version credit rather than
# part of the song name. Matched as whole words, case-insensitive.
EDIT_WORDS = [
    "intro", "outro", "edit", "re-edit", "reedit", "remix", "rmx", "mix", "extended", "ext",
    "clean", "dirty", "explicit", "radio", "version", "ver", "instrumental", "inst",
    "acapella", "acappella", "a cappella", "acap", "acapella out", "hype", "clap", "slam",
    "break", "wordplay", "segue", "blend", "mashup", "mash-up", "transition", "redrum",
    "bootleg", "vip", "flip", "rework", "refix", "quick hit", "short", "snipz", "crank",
    "tool", "drop", "drums", "loop", "verse", "super clean", "remaster", "remastered",
    "official", "audio", "video", "lyrics", "lyric", "hq", "hd", "kbps", "live", "en vivo",
    "bonus track", "original mix", "club", "dub", "cutz", "build", "chorus", "hook",
]
_EDIT_RE = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in
                                              sorted(EDIT_WORDS, key=len, reverse=True)), re.I)

_BRACKET = re.compile(r"\s*([\(\[\{])([^\(\)\[\]\{\}]*)([\)\]\}])")
_ANY_BRACKET = re.compile(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]")
# Key / BPM tails: " - 11A - 84", " 8A 98", " - 9A", " - 128 BPM", " (128 BPM)"
# Camelot (11A) or musical key (F#m). Case-sensitive and word-bounded so "Area 51" survives.
_KEY = r"(?<![\w'])(?:\d{1,2}[ABab]|[A-G][#b♯♭]?(?:m|min|maj)?)"
_KEY_BPM_TAIL = re.compile(r"\s*[-–|]?\s*%s\s*[-–|]?\s*\d{2,3}(?:\.\d+)?\s*(?i:bpm)?\s*$" % _KEY)
_KEY_TAIL = re.compile(r"\s*[-–|]\s*\d{1,2}[ABab]\s*$")
_BPM_TAIL = re.compile(r"\s*[-–|(\[]?\s*\d{2,3}(?:\.\d+)?\s*bpm\s*[)\]]?\s*$", re.I)
_TRAILING_VERSION = re.compile(r"(?:\s+(?:clean|dirty|explicit|extended|short|quick hit))+\s*$", re.I)
_SEGMENT_SPLIT = re.compile(r"\s+[-–—|]\s+")
_MASHUP_SPLIT = re.compile(r"\s+(?:vs\.?|x|×)\s+", re.I)
_TRAILING_REMIX = re.compile(r"\s+(?:remix|rmx|edit|mix|refix|flip|bootleg)\s*$", re.I)

# Artist credits
_FEAT = re.compile(r"\s*[\(\[]?\s*\b(?:feat\.?|ft\.?|featuring|f/)\s+.*$", re.I)
_VERSUS = re.compile(r"\s+(?:vs\.?|x|×)\s+.*$", re.I)
_LIST_SPLIT = re.compile(r"\s*(?:,|&|\+|\band\b|/)\s*", re.I)

_TRACKNUM = re.compile(r"^\s*(?:\d{1,3}|[A-D]\d)\s*[-._)\s]\s*")
_UNTITLED = re.compile(r"(?:track|audio|aud|untitled|unknown)?[\s._-]*\d*", re.I)        # filenames
_UNTITLED_TAG = re.compile(r"(?:track|audio|aud|untitled|unknown)[\s._-]*\d*|\s*", re.I)  # tag titles ("1999" is real)


def normalize(s: str) -> str:
    """Lowercase, strip accents and punctuation, collapse spaces."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ").replace("$", "s")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _strip_ignore(t: str, ignore: list[str]) -> str:
    for w in ignore:
        w = w.strip()
        if w:
            t = re.sub(r"(?<!\w)%s(?!\w)" % re.escape(w), " ", t, flags=re.I)
    return t


def _strip_tails(t: str) -> str:
    prev = None
    while prev != t:
        prev = t
        t = _KEY_BPM_TAIL.sub("", t)
        t = _KEY_TAIL.sub("", t)
        t = _BPM_TAIL.sub("", t)
        t = t.rstrip(" -–|_.")
    return t


def clean_title(title: str, drop_all_brackets: bool = False, ignore: list[str] | None = None,
                keep_mashup: bool = False) -> str:
    """Reduce a DJ-pool style title to the song name."""
    t = (title or "").replace("_", " ")
    t = re.sub(r"\s+", " ", t).strip()
    if ignore:
        t = _strip_ignore(t, ignore)

    t = _strip_tails(t)

    # Brackets that hold edit credits go entirely: "(DJcity Intro - Clean)", "[Dirty]"
    def _bracket(m: re.Match) -> str:
        inner = m.group(2)
        if drop_all_brackets or _EDIT_RE.search(inner) or re.fullmatch(r"\s*(?:\d{1,2}[AB]|\d{2,3}\s*bpm)\s*", inner, re.I):
            return ""
        return m.group(0)
    prev = None
    while prev != t:
        prev = t
        t = _BRACKET.sub(_bracket, t)

    t = _strip_tails(t)

    # " - KidCutUp Clap Intro", " - Tall Boys Remix", " - No Nicki Verse": drop credit segments
    segs = [s for s in _SEGMENT_SPLIT.split(t) if s.strip()]
    if len(segs) > 1:
        keep = [segs[0]] + [s for s in segs[1:] if not _EDIT_RE.search(s)]
        t = " - ".join(keep)

    t = _TRAILING_VERSION.sub("", t)

    # "Pop Dat Thang x Pop That" -> "Pop Dat Thang" (a blend's first song)
    if not keep_mashup:
        parts = _MASHUP_SPLIT.split(t)
        if len(parts) > 1 and parts[0].strip():
            t = parts[0]

    t = re.sub(r"\s+", " ", t).strip(" -–|_.,/")
    return t


def primary_artist(artist: str) -> str:
    """'Big Sean ft Nicki Minaj' -> 'Big Sean'; '310babii vs Juicy J' -> '310babii'."""
    a = (artist or "").strip()
    a2 = _FEAT.sub("", a).strip()
    a2 = _VERSUS.sub("", a2).strip()
    return a2 or a


def first_artist(artist: str) -> str:
    """'Big Sean, Nicki Minaj' -> 'Big Sean'; 'Doechii & JT' -> 'Doechii'."""
    a = primary_artist(artist)
    parts = [p for p in _LIST_SPLIT.split(a) if p.strip()]
    return parts[0].strip() if parts else a


def parse_filename(path: str | Path, ignore: list[str] | None = None) -> tuple[str, str]:
    """Best-effort (artist, title) from a filename like '03 - Artist - Title (Clean).mp3'."""
    stem = Path(path).stem
    stem = stem.replace("_", " ")
    stem = re.sub(r"\s+", " ", stem).strip()
    stem = _TRACKNUM.sub("", stem)
    parts = [p.strip() for p in _SEGMENT_SPLIT.split(stem) if p.strip()]
    if len(parts) >= 2:
        artist, title = parts[0], " - ".join(parts[1:])
    else:
        bare = stem.split("-", 1)
        if len(bare) == 2 and bare[0].strip() and bare[1].strip():
            artist, title = bare[0].strip(), bare[1].strip()
        else:
            artist, title = "", stem
    title = clean_title(title, ignore=ignore)
    if _UNTITLED.fullmatch(title.lstrip(".")):
        title = ""
    return artist, title


def search_terms(artist_tag: str, title_tag: str, path: str | Path,
                 ignore: list[str] | None = None) -> tuple[str, str]:
    """The single best artist/title to search with: tags first, filename as fallback."""
    attempts = search_attempts(artist_tag, title_tag, path, ignore)
    return attempts[0] if attempts else ("", "")


def search_attempts(artist_tag: str, title_tag: str, path: str | Path,
                    ignore: list[str] | None = None) -> list[tuple[str, str]]:
    """Ordered (artist, title) queries to try until one finds a confident match."""
    f_artist, f_title = parse_filename(path, ignore)
    artist = (artist_tag or "").strip() or f_artist
    title = clean_title(title_tag, ignore=ignore) if (title_tag or "").strip() else f_title
    if title and _UNTITLED_TAG.fullmatch(title):
        title = ""
    if not title and f_title:
        title = f_title

    out: list[tuple[str, str]] = []

    def add(a: str, t: str) -> None:
        a, t = a.strip(), t.strip()
        if t and (a, t) not in out:
            out.append((a, t))

    main_artist = primary_artist(artist)
    add(main_artist, title)
    add(first_artist(artist), title)
    # Filename sometimes carries a cleaner title than the tag (or vice versa)
    if f_title and normalize(f_title) != normalize(title):
        add(main_artist, f_title)
    # "Pink Blush Remix" -> "Pink Blush"
    bare = _TRAILING_REMIX.sub("", title)
    if bare != title:
        add(first_artist(artist), bare)
    # Title only, for mislabeled artists
    add("", title)
    return out


def similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    # Reward containment ("La Gozadera" vs "La Gozadera (Remix)")
    if (na in nb or nb in na) and min(len(na), len(nb)) >= 3:
        ratio = max(ratio, 0.85)
    return ratio


def title_similarity(a: str, b: str) -> float:
    """Compare titles, also trying with all brackets removed."""
    return max(similarity(a, b), similarity(clean_title(a, True), clean_title(b, True)))


def artist_similarity(a: str, b: str) -> float:
    best = max(similarity(a, b), similarity(primary_artist(a), primary_artist(b)))
    # A credited artist among several still counts: "Big Sean" vs "Big Sean & Nicki Minaj"
    fa, fb = first_artist(a), first_artist(b)
    best = max(best, similarity(fa, fb))
    na, nb = normalize(a), normalize(b)
    for p in [normalize(x) for x in _LIST_SPLIT.split(primary_artist(b)) if x.strip()]:
        if p and normalize(fa) == p:
            best = max(best, 0.9)
    if na and nb and (normalize(fa) in nb or normalize(fb) in na):
        best = max(best, 0.85)
    return best
