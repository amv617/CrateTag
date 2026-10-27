"""Read Serato crates (read-only).

Serato keeps a `_Serato_` folder in ~/Music for the internal drive and at the top level of every
external drive it has used. Each crate is a file in `_Serato_/Subcrates`, named with `%%` between
levels ("#CASE CRATES#%%SEP OCT WED PREP%%092626 WEDDING.crate").

A .crate file is a sequence of records: a 4-byte ASCII tag, a 4-byte big-endian length, then the
payload. Track entries are `otrk` records containing a `ptrk` record with the song's path in
UTF-16 big-endian. Paths are relative to the root of the drive the `_Serato_` folder is on
(so "Users/me/Music/x.mp3" for the internal drive, "Music/x.mp3" on an external drive).

CrateTag never writes to these files.
"""

from __future__ import annotations

import os
import re
import struct
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

CRATE_EXT = ".crate"
SEP = "%%"
SMART_SEP = "\u226b\u226b"  # "≫≫": how Serato nests Smart Crates under a crate
_ESCAPE = re.compile("\u241b{1,2}([0-9A-Fa-f]{2})")  # "␛␛2a" = "*" in crate file names


def display_name(raw: str) -> str:
    """Serato writes some characters in crate file names as codes ("␛␛2a" for "*"). Decode them."""
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), raw)


def split_key(stem: str) -> list[str]:
    """'A%%B≫≫C' -> ['A', 'B', 'C'] (raw parts, codes left as they are)."""
    return [p for p in re.split("%%|\u226b\u226b", stem) if p != ""] or [stem]


@dataclass
class Library:
    label: str            # "Macintosh HD" or the external drive's name
    serato_dir: Path      # .../_Serato_
    root: Path            # what track paths are relative to ("/" or "/Volumes/Drive")
    external: bool
    crates: list["Crate"] = field(default_factory=list)
    smart_crates: list[str] = field(default_factory=list)       # display names ("SMART CRATES > 40-59")
    smart_keys: list[list[str]] = field(default_factory=list)   # raw parts, for nesting in the tree
    order: list[str] = field(default_factory=list)  # Serato's custom crate order (neworder.pref)


@dataclass
class Crate:
    parts: list[str]      # ["#CASE CRATES#", "SEP OCT WED PREP", "092626 WEDDING"]
    file: Path
    library: Library

    @property
    def name(self) -> str:
        return display_name(self.parts[-1])

    @property
    def full_name(self) -> str:
        return " > ".join(display_name(p) for p in self.parts)

    @property
    def key(self) -> str:
        return SEP.join(self.parts)


# --------------------------------------------------------------------------- crate files

def _records(data: bytes):
    i, n = 0, len(data)
    while i + 8 <= n:
        tag = data[i:i + 4]
        (length,) = struct.unpack(">I", data[i + 4:i + 8])
        start, end = i + 8, i + 8 + length
        if end > n:  # truncated file: stop rather than misread
            return
        yield tag, data[start:end]
        i = end


def read_crate_paths(file: str | Path) -> list[str]:
    """The raw track paths stored in a .crate file, in crate order."""
    data = Path(file).read_bytes()
    paths: list[str] = []
    for tag, payload in _records(data):
        if tag != b"otrk":
            continue
        for sub, value in _records(payload):
            if sub == b"ptrk":
                try:
                    paths.append(value.decode("utf-16-be"))
                except UnicodeDecodeError:
                    pass
                break
    return paths


def write_crate(file: str | Path, paths: list[str]) -> None:
    """Write a minimal .crate file. Used only by the tests to build sample crates."""
    def rec(tag: bytes, payload: bytes) -> bytes:
        return tag + struct.pack(">I", len(payload)) + payload
    out = rec(b"vrsn", "1.0/Serato ScratchLive Crate".encode("utf-16-be"))
    for p in paths:
        out += rec(b"otrk", rec(b"ptrk", p.encode("utf-16-be")))
    Path(file).parent.mkdir(parents=True, exist_ok=True)
    Path(file).write_bytes(out)


def resolve_track(raw: str, library: Library) -> Path:
    """Turn a stored crate path into a real file path for this library's drive."""
    p = raw.replace("\\", "/")
    if p.startswith("/"):
        return Path(p)
    # Internal-drive crates can point at external drives ("Volumes/Drive/...") and vice versa.
    if p.startswith("Volumes/") and not library.external:
        return Path("/") / p
    return library.root / p


def _existing_form(path: Path) -> Path:
    """macOS may store accented names decomposed while Serato stores them composed (or the
    reverse). If the path as written isn't there, try the other Unicode form."""
    if path.exists():
        return path
    for form in ("NFD", "NFC"):
        alt = Path(unicodedata.normalize(form, str(path)))
        if alt.exists():
            return alt
    return path


@dataclass
class CrateNode:
    """One crate as Serato shows it: merged across every drive that has a file for it.

    Serato combines the crate lists of all connected drives into one tree. A crate whose songs
    live on two drives has a .crate file on each, and a sub-crate's parent may be stored on a
    different drive (or not at all). members is the list of actual .crate files behind this node;
    it is empty for a parent that exists only because it has sub-crates.
    """
    parts: list[str]
    members: list[Crate] = field(default_factory=list)
    children: list["CrateNode"] = field(default_factory=list)
    smart: bool = False      # a Smart Crate: shown, but its songs can't be read
    order_key: str = ""      # the name Serato's order file uses (Smart Crates use "≫≫")

    @property
    def name(self) -> str:
        return display_name(self.parts[-1])

    @property
    def full_name(self) -> str:
        return " > ".join(display_name(p) for p in self.parts)

    @property
    def key(self) -> str:
        return SEP.join(self.parts)

    @property
    def library(self) -> Library | None:
        return self.members[0].library if self.members else None

    @property
    def drives(self) -> list[str]:
        return [m.library.label for m in self.members]

    def descendants(self) -> list["CrateNode"]:
        out = []
        for c in self.children:
            out.append(c)
            out.extend(c.descendants())
        return out


def merged_order(libs: list[Library]) -> list[str]:
    """Combine each drive's crate order into one, keeping every drive's own order.

    The drive with the most crates sets the base order. A crate that's only on another drive is
    placed just before the next crate both drives share, which is how Serato interleaves them.
    """
    seqs = sorted((l.order for l in libs if l.order), key=len, reverse=True)
    if not seqs:
        return []
    merged = list(dict.fromkeys(seqs[0]))
    for seq in seqs[1:]:
        pos = {k: i for i, k in enumerate(merged)}
        out: list[str] = []
        pending: list[str] = []
        i = 0
        for k in seq:
            j = pos.get(k)
            if j is None:
                if k not in pending:
                    pending.append(k)
                continue
            if j >= i:
                out.extend(merged[i:j])
                out.extend(pending)
                out.append(merged[j])
                i = j + 1
            else:  # the two drives disagree on order: keep the base order
                out.extend(pending)
            pending = []
        out.extend(merged[i:])
        out.extend(pending)
        merged = list(dict.fromkeys(out))
    return merged


def build_tree(libs: list[Library], order_libs: list[Library] | None = None) -> list[CrateNode]:
    """Merge the crates of these libraries into one nested tree, like Serato's crate list.

    Missing parents are filled in, so "MHS06%%CASE'S LIST" always sits under MHS06 even when
    MHS06.crate is on another drive or doesn't exist. Siblings follow Serato's custom order
    (the first library's order wins), then A to Z.
    """
    nodes: dict[str, CrateNode] = {}
    order = {k: i for i, k in enumerate(merged_order(order_libs or libs))}

    def node(parts: list[str]) -> CrateNode:
        key = SEP.join(parts)
        n = nodes.get(key)
        if n is None:
            n = nodes[key] = CrateNode(parts=list(parts))
            if len(parts) > 1:
                node(parts[:-1]).children.append(n)
        return n

    for lib in libs:
        for c in lib.crates:
            node(c.parts).members.append(c)
    for lib in libs:
        for parts in lib.smart_keys:
            n = node(parts)
            if not n.members:
                n.smart = True
                n.order_key = SEP.join(parts[:-1]) + SMART_SEP + parts[-1] if len(parts) > 1 else parts[0]

    def sort(children: list[CrateNode]) -> None:
        children.sort(key=lambda n: (order.get(n.order_key or n.key, order.get(n.key, 10**9)), n.name.lower()))
        for ch in children:
            sort(ch.children)

    top = [n for n in nodes.values() if len(n.parts) == 1]
    sort(top)
    return top


_COUNT_CACHE: dict[str, tuple[float, int, int]] = {}


def crate_file_count(file: Path) -> int:
    """Number of tracks in a .crate file, cached until the file changes (fast on slow USB drives)."""
    try:
        st = file.stat()
    except OSError:
        return 0
    key = str(file)
    hit = _COUNT_CACHE.get(key)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    try:
        n = len(read_crate_paths(file))
    except OSError:
        n = 0
    _COUNT_CACHE[key] = (st.st_mtime, st.st_size, n)
    return n


def node_track_count(n: CrateNode) -> int:
    return sum(crate_file_count(m.file) for m in n.members)


def crate_tracks(crate: "Crate | CrateNode", include_subcrates: bool = False) -> list[Path]:
    if isinstance(crate, CrateNode):
        nodes = [crate] + (crate.descendants() if include_subcrates else [])
        crates = [m for n in nodes for m in n.members]
    else:
        crates = [crate]
        if include_subcrates:
            prefix = crate.key + SEP
            crates += [c for c in crate.library.crates if c.key.startswith(prefix)]
    seen: set[str] = set()
    out: list[Path] = []
    for c in crates:
        try:
            raws = read_crate_paths(c.file)
        except OSError:
            continue
        for raw in raws:
            path = _existing_form(resolve_track(raw, c.library))
            k = str(path)
            if k not in seen:
                seen.add(k)
                out.append(path)
    return out


# --------------------------------------------------------------------------- libraries

def _crate_order(serato_dir: Path) -> list[str]:
    """Serato's custom crate order from neworder.pref, if present ("[crate]A%%B" lines)."""
    f = serato_dir / "neworder.pref"
    if not f.exists():
        return []
    raw = f.read_bytes()
    text = ""
    for enc in ("utf-16", "utf-16-be", "utf-8"):
        try:
            text = raw.decode(enc)
            if "[crate]" in text:
                break
        except UnicodeDecodeError:
            continue
    order = []
    for line in text.splitlines():
        line = line.strip().strip("\x00")
        if line.startswith("[crate]"):
            order.append(line[len("[crate]"):])
    return order


def load_library(serato_dir: Path, root: Path, label: str, external: bool) -> Library:
    lib = Library(label=label, serato_dir=serato_dir, root=root, external=external)
    sub = serato_dir / "Subcrates"
    if sub.is_dir():
        for f in sub.iterdir():
            if f.suffix.lower() == CRATE_EXT and not f.name.startswith("._"):
                lib.crates.append(Crate(parts=f.stem.split(SEP), file=f, library=lib))
    smart = serato_dir / "SmartCrates"
    if smart.is_dir():
        stems = sorted(f.stem for f in smart.iterdir()
                       if f.suffix.lower() == ".scrate" and not f.name.startswith("._"))
        lib.smart_keys = [split_key(st) for st in stems]
        lib.smart_crates = [" > ".join(display_name(p) for p in k) for k in lib.smart_keys]
    lib.order = _crate_order(serato_dir)
    order = {k: i for i, k in enumerate(lib.order)}

    def sort_key(c: Crate):
        # Keep each crate right after its parent; order siblings by Serato's order, else A to Z.
        keys = []
        for depth in range(1, len(c.parts) + 1):
            k = SEP.join(c.parts[:depth])
            keys.append((order.get(k, 10**6), c.parts[depth - 1].lower()))
        return keys
    lib.crates.sort(key=sort_key)
    return lib


def find_libraries(home: Path | None = None, volumes: Path | None = None) -> list[Library]:
    """Every Serato library on this Mac: the internal one, then each connected drive."""
    home = home or Path.home()
    volumes = volumes or Path("/Volumes")
    libs: list[Library] = []
    seen: set[str] = set()

    internal = home / "Music" / "_Serato_"
    if internal.is_dir():
        libs.append(load_library(internal, Path("/"), _internal_label(volumes), external=False))
        seen.add(os.path.realpath(internal))

    if volumes.is_dir():
        try:
            entries = sorted(volumes.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            entries = []
        for vol in entries:
            try:
                if os.path.realpath(vol) == "/":  # /Volumes/Macintosh HD points at the internal drive
                    continue
                sd = vol / "_Serato_"
                if sd.is_dir() and os.path.realpath(sd) not in seen:
                    seen.add(os.path.realpath(sd))
                    libs.append(load_library(sd, vol, vol.name, external=True))
            except OSError:
                continue  # drive went away or access was refused
    return libs


def _internal_label(volumes: Path) -> str:
    try:
        for vol in volumes.iterdir():
            if os.path.realpath(vol) == "/":
                return vol.name
    except OSError:
        pass
    return "This Mac"


# --------------------------------------------------------------------------- whole library ("All Songs")

ALL_SONGS_KEY = "\x00all-songs"
DB_FILE = "database V2"
_MP3_MARK = b"ttyp\x00\x00\x00\x06" + "mp3".encode("utf-16-be")


@dataclass
class DbTrack:
    path: Path
    ftype: str
    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    year: str = ""
    seconds: float = 0.0
    missing: bool = False


def _length_seconds(s: str) -> float:
    """Serato lengths look like '05:07.57' or '1:02:03.10'."""
    try:
        parts = [float(p) for p in s.split(":")]
    except ValueError:
        return 0.0
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def read_database(library: Library):
    """Yield every track in a drive's Serato library file (database V2), read-only."""
    f = library.serato_dir / DB_FILE
    try:
        data = f.read_bytes()
    except OSError:
        return
    for tag, payload in _records(data):
        if tag != b"otrk":
            continue
        fields: dict[bytes, bytes] = {}
        for sub, value in _records(payload):
            fields[sub] = value

        def text(key: bytes) -> str:
            v = fields.get(key)
            if not v:
                return ""
            try:
                return v.decode("utf-16-be").strip("\x00").strip()
            except UnicodeDecodeError:
                return ""

        raw = text(b"pfil")
        if not raw:
            continue
        m = re.search(r"\d{4}", text(b"ttyr"))
        yield DbTrack(
            path=resolve_track(raw, library),
            ftype=text(b"ttyp").lower(),
            title=text(b"tsng"),
            artist=text(b"tart"),
            album=text(b"talb"),
            genre=text(b"tgen"),
            year=m.group(0) if m else "",
            seconds=_length_seconds(text(b"tlen")),
            missing=fields.get(b"bmis", b"\x00")[:1] == b"\x01",
        )


_DB_COUNT_CACHE: dict[str, tuple[float, int, int]] = {}


def database_mp3_count(library: Library) -> int:
    """How many MP3s a drive's Serato library lists, without fully reading it (cached)."""
    f = library.serato_dir / DB_FILE
    try:
        st = f.stat()
    except OSError:
        return 0
    hit = _DB_COUNT_CACHE.get(str(f))
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    try:
        n = f.read_bytes().count(_MP3_MARK)
    except OSError:
        n = 0
    _DB_COUNT_CACHE[str(f)] = (st.st_mtime, st.st_size, n)
    return n


def write_database(file: str | Path, tracks: list[dict]) -> None:
    """Write a minimal database V2 file. Used only by the tests."""
    def rec(tag: bytes, payload: bytes) -> bytes:
        return tag + struct.pack(">I", len(payload)) + payload
    out = rec(b"vrsn", "2.0/Serato Scratch LIVE Database".encode("utf-16-be"))
    for t in tracks:
        body = b""
        for key, value in t.items():
            if key == "bmis":
                body += rec(b"bmis", b"\x01" if value else b"\x00")
            else:
                body += rec(key.encode(), str(value).encode("utf-16-be"))
        out += rec(b"otrk", body)
    Path(file).parent.mkdir(parents=True, exist_ok=True)
    Path(file).write_bytes(out)
