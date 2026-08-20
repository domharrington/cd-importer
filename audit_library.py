#!/usr/bin/env python3
"""
Audit the Navidrome library on the Raspberry Pi and write a report.

Reads the library over SSH and changes nothing. SSH rather than the SMB mount
deliberately: the mount cannot traverse some accented paths (Låpsley,
José González), so anything measured through it is quietly incomplete.

Usage:
    ./audit_library.py                      # writes audit-report.md
    ./audit_library.py --out /tmp/audit.md
    ./audit_library.py --host pi --path /srv/shares/media/Music

The interesting output is the compilation section. Tracks from a various-artists
compilation are usually tagged with the *track* artist and no ALBUMARTIST, so
each one lands in its own artist folder and looks like a one-track album. That
single cause accounts for most "incomplete albums" in this library.
"""

import argparse
import collections
import os
import subprocess
import sys
import unicodedata

AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".flac", ".wma", ".ogg", ".opus", ".wav"}
LOSSLESS_EXTS = {".flac", ".wav"}
ARTIST_IMAGE_NAMES = {"artist.jpg", "artist.jpeg", "artist.png", "artist.webp"}
# Folder names that mean "no album", not a real album title.
UNKNOWN_ALBUM_NAMES = {"unknown album", "unknown", "untitled", ""}
# Artist folders that are already compilation buckets, not real artists.
COMPILATION_ARTISTS = {"compilations", "various artists", "various", "va", "soundtracks"}


def fold(name):
    """Strip accents and case so 'José González' and 'Jose Gonzalez' collide."""
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower().strip()


def list_remote_files(host, path):
    """Every file under path, as repo-relative POSIX paths."""
    # -print0 so newlines in filenames cannot corrupt the listing.
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, f"find {path!r} -type f -print0"],
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.exit(f"ssh/find failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    prefix = path.rstrip("/") + "/"
    out = []
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        p = raw.decode("utf-8", "surrogateescape")
        out.append(p[len(prefix):] if p.startswith(prefix) else p)
    return out


class Library:
    def __init__(self, rel_paths):
        self.junk = collections.Counter()
        self.albums = collections.defaultdict(list)   # (artist, album) -> [filenames]
        self.artist_images = set()
        self.other_files = collections.Counter()
        self.deep = []                                # anything not Artist/Album/Track

        for rel in rel_paths:
            parts = rel.split("/")
            base = parts[-1]
            if base.startswith("._"):
                self.junk["AppleDouble ._*"] += 1
                continue
            if base == ".DS_Store":
                self.junk[".DS_Store"] += 1
                continue
            if base.lower() in ARTIST_IMAGE_NAMES and len(parts) == 2:
                self.artist_images.add(parts[0])
                continue
            ext = os.path.splitext(base)[1].lower()
            if ext not in AUDIO_EXTS:
                self.other_files[ext or "(no extension)"] += 1
                continue
            if len(parts) == 3:
                self.albums[(parts[0], parts[1])].append(base)
            else:
                self.deep.append(rel)

    @property
    def artists(self):
        return {artist for artist, _ in self.albums}

    def track_count(self):
        return sum(len(v) for v in self.albums.values())

    def format_mix(self):
        counts = collections.Counter()
        for files in self.albums.values():
            for f in files:
                counts[os.path.splitext(f)[1].lower()] += 1
        return counts

    def single_track_albums(self):
        return [k for k, v in self.albums.items() if len(v) == 1]

    def shattered_compilations(self):
        """Album titles that appear under several artist folders with one track each.

        That shape means one compilation split across artists, not many albums.
        """
        by_title = collections.defaultdict(list)
        for artist, album in self.single_track_albums():
            if album.strip().lower() in UNKNOWN_ALBUM_NAMES:
                continue
            by_title[album].append(artist)
        return {t: a for t, a in by_title.items() if len(a) > 1}

    def true_singles(self):
        by_title = collections.defaultdict(list)
        for artist, album in self.single_track_albums():
            by_title[album].append(artist)
        return sorted(
            (a, t) for t, arts in by_title.items()
            if len(arts) == 1 and t.strip().lower() not in UNKNOWN_ALBUM_NAMES
            for a in arts
        )

    def unknown_album_folders(self):
        return sorted(
            (artist, album, self.albums[(artist, album)])
            for (artist, album) in self.albums
            if album.strip().lower() in UNKNOWN_ALBUM_NAMES
        )

    def short_albums(self, low=2, high=5):
        return sorted(
            (artist, album, len(self.albums[(artist, album)]))
            for (artist, album) in self.albums
            if low <= len(self.albums[(artist, album)]) <= high
        )

    def duplicate_artist_folders(self):
        groups = collections.defaultdict(list)
        for artist in self.artists:
            groups[fold(artist)].append(artist)
        return {k: sorted(v) for k, v in groups.items() if len(v) > 1}

    def artists_missing_image(self):
        return sorted(a for a in self.artists if a not in self.artist_images)


def write_report(lib, out_path, host, path):
    L = []
    w = L.append
    fmt = lib.format_mix()
    lossless = sum(c for e, c in fmt.items() if e in LOSSLESS_EXTS)
    shattered = lib.shattered_compilations()
    shattered_tracks = sum(len(v) for v in shattered.values())
    singles = lib.true_singles()
    unknown = lib.unknown_album_folders()

    w(f"# Library audit — {host}:{path}\n")
    w("Read-only. Nothing in the library was modified.\n")

    w("## Summary\n")
    w("| | |")
    w("|---|---|")
    w(f"| artists | {len(lib.artists)} |")
    w(f"| albums | {len(lib.albums)} |")
    w(f"| tracks | {lib.track_count()} |")
    w(f"| lossless tracks | {lossless} |")
    w(f"| single-track albums | {len(lib.single_track_albums())} |")
    w(f"| artists without an image | {len(lib.artists_missing_image())} |")
    for k, v in sorted(lib.junk.items()):
        w(f"| {k} | {v} |")
    w("")
    w("Formats: " + ", ".join(f"{e.lstrip('.') or '?'} {c}" for e, c in fmt.most_common()) + "\n")

    w("## Shattered compilations\n")
    w("One compilation whose tracks each sit in their own artist folder, because")
    w("the files carry the track artist with no `ALBUMARTIST` or compilation flag.")
    w("Repairing these turns most of the single-track albums into real albums.\n")
    if shattered:
        w(f"**{len(shattered)} albums covering {shattered_tracks} tracks.**\n")
        w("| album | artist folders | proposed destination |")
        w("|---|---|---|")
        for title, artists in sorted(shattered.items(), key=lambda kv: -len(kv[1])):
            w(f"| {title} | {len(artists)} | `Various Artists/{title}/` |")
        w("")
        for title, artists in sorted(shattered.items(), key=lambda kv: -len(kv[1])):
            w(f"<details><summary>{title} ({len(artists)} folders)</summary>\n")
            for a in sorted(artists):
                for f in lib.albums[(a, title)]:
                    w(f"- `{a}/{title}/{f}`")
            w("\n</details>\n")
    else:
        w("None found.\n")

    w("## Genuine one-off singles\n")
    w("A single track whose album title appears nowhere else — most likely a")
    w("standalone download rather than a broken album. Probably fine as-is.\n")
    if singles:
        w(f"**{len(singles)}.**\n")
        for artist, album in singles:
            w(f"- {artist} — {album}")
        w("")
    else:
        w("None.\n")

    w("## `Unknown Album`\n")
    w("Tracks with no usable album tag. These need identifying, filing or deleting.\n")
    if unknown:
        w(f"**{len(unknown)} folders, {sum(len(f) for _, _, f in unknown)} tracks.**\n")
        for artist, album, files in unknown:
            w(f"- **{artist}** ({len(files)})")
            for f in sorted(files):
                w(f"  - `{f}`")
        w("")
    else:
        w("None.\n")

    w("## Short albums (2–5 tracks)\n")
    w("Could be EPs and singles, or albums missing most of their tracks. Track")
    w("numbers in the tags would settle it; listed here for your judgement.\n")
    short = lib.short_albums()
    if short:
        w(f"**{len(short)}.**\n")
        for artist, album, n in short:
            w(f"- {n} tracks — {artist} / {album}")
        w("")
    else:
        w("None.\n")

    w("## Duplicate artist folders\n")
    w("Same artist under two spellings — usually accented vs unaccented, which is")
    w("how macOS/SMB filename normalisation manifests. Merge these.\n")
    dups = lib.duplicate_artist_folders()
    if dups:
        for _, names in sorted(dups.items()):
            w(f"- {' | '.join(f'`{n}`' for n in names)}")
        w("")
    else:
        w("None.\n")

    w("## Artists without an image\n")
    w("Navidrome's default `ArtistArtPriority` looks for `artist.*` in the artist")
    w("folder. `fetch_artist_art.py` fills these in.\n")
    missing = lib.artists_missing_image()
    w(f"**{len(missing)} of {len(lib.artists)}.**\n")
    if missing and len(missing) < len(lib.artists):
        for a in missing:
            w(f"- {a}")
        w("")

    if lib.deep:
        w("## Unexpected depth\n")
        w("Audio not at `Artist/Album/Track` — multi-disc subfolders are normal here.\n")
        for p in sorted(lib.deep)[:40]:
            w(f"- `{p}`")
        if len(lib.deep) > 40:
            w(f"- …and {len(lib.deep) - 40} more")
        w("")

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser(description="Audit the Navidrome library on the Pi")
    ap.add_argument("--host", default="raspberrypi.local")
    ap.add_argument("--path", default="/srv/shares/media/Music")
    ap.add_argument("--out", default="audit-report.md")
    args = ap.parse_args()

    print(f"reading {args.host}:{args.path} over ssh...", file=sys.stderr)
    files = list_remote_files(args.host, args.path)
    print(f"  {len(files)} files", file=sys.stderr)

    lib = Library(files)
    write_report(lib, args.out, args.host, args.path)

    shattered = lib.shattered_compilations()
    print(f"\nwrote {args.out}", file=sys.stderr)
    print(f"  {len(lib.artists)} artists, {len(lib.albums)} albums, "
          f"{lib.track_count()} tracks", file=sys.stderr)
    print(f"  {len(lib.single_track_albums())} single-track albums, of which "
          f"{sum(len(v) for v in shattered.values())} belong to "
          f"{len(shattered)} shattered compilations", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
