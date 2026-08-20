#!/usr/bin/env python3
"""
One-off: three structural fixes on the Pi's library, found while chasing
duplicate albums in Navidrome.

  1. A nested "Music/Music" directory holding Låpsley and José González — both
     artists whose accented names the SMB mount cannot traverse, so this is
     almost certainly a copy that landed one level too deep.

  2. "Jose Gonzalez" (ASCII) and the decomposed accented spelling as separate
     artists, splitting the same album in two. The ASCII one is kept, since a
     name with no accents cannot be caught by NFD/NFC ambiguity again.

  3. "Fratellis" (13 lossy) alongside "The Fratellis" (13 FLAC) — the same album
     twice. deploy.sh missed it because its name matching did not strip a leading
     "The"; since fixed.

Written in Python rather than shell, and deliberately never types a non-ASCII
name: the top-level folders are NFD while a typed literal is NFC, so a shell
version using "José González" created a *fourth* spelling instead of merging
into the existing one. Every path here comes from the filesystem.

Each nested album is compared with its top-level counterpart before anything
moves, because the two cases need opposite treatment: Låpsley's nested copy is
byte-identical and redundant, while González's holds a track the top-level copy
does not.

    ssh raspberrypi.local 'python3 oneoff_tidy_folders.py'            # dry run
    ssh raspberrypi.local 'python3 oneoff_tidy_folders.py --apply'
"""

import argparse
import os
import shutil
import sys
import unicodedata

MUSIC = "/srv/shares/media/Music"
SUPERSEDED = "/srv/shares/media/_superseded"
AUDIO = (".mp3", ".m4a", ".mp4", ".flac")


def fold(name):
    """Accent- and case-insensitive key, so NFD and NFC spellings collide."""
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def prefer_spelling(names):
    """Which of several spellings of one artist to keep.

    The pure-ASCII spelling wins. Folder names are cosmetic to Navidrome, which
    takes the display name from the tags, so the only thing that matters is
    picking a name that cannot be caught by NFD/NFC ambiguity again — and an
    unaccented name never can. That ambiguity has already produced a nested
    duplicate directory here and misled three of my own commands.
    """
    ascii_only = [n for n in names if all(ord(c) < 128 for c in n)]
    return sorted(ascii_only or names)[0]


def files_in(directory):
    """{filename: size} for audio files, ignoring macOS sidecars."""
    out = {}
    for root, _, names in os.walk(directory):
        for n in names:
            if n.startswith("._") or not n.lower().endswith(AUDIO):
                continue
            out[n] = os.path.getsize(os.path.join(root, n))
    return out


class Plan:
    def __init__(self, apply_changes):
        self.apply = apply_changes
        self.actions = 0

    def move(self, src, dest_dir):
        os.makedirs(dest_dir, exist_ok=True) if self.apply else None
        dest = os.path.join(dest_dir, os.path.basename(src))
        # A collision here is usually a third copy of the same track: both the
        # nested folder and the accented folder held "04 Heartbeats.mp3", and
        # whichever step runs second finds the destination taken. Skipping would
        # leave that copy behind and stop the empty directories being pruned, so
        # park an identical duplicate instead and only refuse when they differ.
        if os.path.exists(dest):
            try:
                same = os.path.getsize(src) == os.path.getsize(dest)
            except OSError:
                same = False
            if not same:
                print(f"     ! {dest} exists with a different size — leaving both")
                return
            parked = os.path.join(SUPERSEDED, "duplicate-copies",
                                  os.path.basename(os.path.dirname(src)))
            print(f"     {'parking' if self.apply else 'would park'} duplicate {src}")
            print(f"       -> {os.path.join(parked, os.path.basename(src))}")
            if self.apply:
                os.makedirs(parked, exist_ok=True)
                target = os.path.join(parked, os.path.basename(src))
                if os.path.exists(target):
                    os.remove(src)
                else:
                    shutil.move(src, target)
            self.actions += 1
            return
        print(f"     {'moving' if self.apply else 'would move'} {src}")
        print(f"       -> {dest}")
        if self.apply:
            shutil.move(src, dest)
        self.actions += 1

    def prune(self, directory):
        """Remove a directory only if it is genuinely empty."""
        if not self.apply:
            print(f"     would remove if empty: {directory}")
            return
        for root, dirs, files in os.walk(directory, topdown=False):
            if not dirs and not [f for f in files if not f.startswith("._")]:
                for junk in files:
                    os.remove(os.path.join(root, junk))
                try:
                    os.rmdir(root)
                except OSError as err:
                    print(f"     ! could not remove {root}: {err}")


def tidy_nested(music, plan):
    nested_root = os.path.join(music, "Music")
    if not os.path.isdir(nested_root):
        print("1. no nested Music/Music — nothing to do")
        return
    print("1. nested Music/Music")
    # Several spellings can fold to one key, so choose deterministically rather
    # than letting the last one in the directory listing win.
    grouped = {}
    for n in os.listdir(music):
        if n != "Music":
            grouped.setdefault(fold(n), []).append(n)
    tops = {k: prefer_spelling(v) for k, v in grouped.items()}
    for artist in sorted(os.listdir(nested_root)):
        apath = os.path.join(nested_root, artist)
        if not os.path.isdir(apath):
            continue
        top_name = tops.get(fold(artist))
        print(f"   artist {artist!r} -> {top_name!r}")
        if top_name is None:
            # No counterpart: the whole artist folder simply belongs one level up.
            plan.move(apath, music)
            continue
        top_path = os.path.join(music, top_name)
        top_albums = {fold(a): a for a in os.listdir(top_path)}
        for album in sorted(os.listdir(apath)):
            src_album = os.path.join(apath, album)
            if not os.path.isdir(src_album):
                continue
            match = top_albums.get(fold(album))
            if match is None:
                plan.move(src_album, top_path)
                continue
            nested_files = files_in(src_album)
            top_files = files_in(os.path.join(top_path, match))
            redundant = [f for f, s in nested_files.items() if top_files.get(f) == s]
            unique = [f for f in nested_files if f not in top_files]
            print(f"     album {album!r}: {len(nested_files)} nested, "
                  f"{len(redundant)} identical to top level, {len(unique)} unique")
            if unique:
                # Complementary copies: keep the tracks the top level lacks.
                for f in unique:
                    plan.move(os.path.join(src_album, f), os.path.join(top_path, match))
            if redundant and not unique:
                # Byte-identical duplicate: park it rather than delete it.
                plan.move(src_album, os.path.join(SUPERSEDED, "nested-duplicates", artist))
        plan.prune(apath)
    plan.prune(nested_root)


def merge_accent_duplicates(music, plan):
    """Consolidate artist folders that differ only by accents into one.

    The ASCII spelling is kept — see prefer_spelling for why.
    """
    print("2. duplicate artist folders differing only by accents")
    groups = {}
    for name in os.listdir(music):
        if not os.path.isdir(os.path.join(music, name)) or name == "Music":
            continue
        groups.setdefault(fold(name), []).append(name)
    for key, names in sorted(groups.items()):
        if len(names) < 2:
            continue
        keep = prefer_spelling(names)
        others = [n for n in names if n != keep]
        print(f"   keeping {keep!r}, merging {others}")
        keep_path = os.path.join(music, keep)
        keep_albums = {fold(a): a for a in os.listdir(keep_path)}
        for other in others:
            other_path = os.path.join(music, other)
            for album in sorted(os.listdir(other_path)):
                src = os.path.join(other_path, album)
                if not os.path.isdir(src):
                    continue
                match = keep_albums.get(fold(album))
                if match is None:
                    plan.move(src, keep_path)
                else:
                    for f in sorted(files_in(src)):
                        plan.move(os.path.join(src, f), os.path.join(keep_path, match))
                    plan.prune(src)
            plan.prune(other_path)
    if not any(len(v) > 1 for v in groups.values()):
        print("   none found")


def supersede_lossy_duplicate(music, plan, lossy_key="fratellis"):
    """Move a lossy album aside when a FLAC copy of it exists elsewhere."""
    print("3. lossy copy superseded by FLAC")
    byfold = {}
    for name in os.listdir(music):
        if os.path.isdir(os.path.join(music, name)):
            byfold.setdefault(fold(name).replace("the ", "", 1), []).append(name)
    for key, names in byfold.items():
        if key != lossy_key or len(names) < 2:
            continue
        counts = {}
        for n in names:
            f = files_in(os.path.join(music, n))
            counts[n] = (sum(1 for k in f if k.lower().endswith(".flac")), len(f))
        flac_folder = max(counts, key=lambda n: counts[n][0])
        for n in names:
            if n == flac_folder:
                continue
            flacs, total = counts[flac_folder][0], counts[n][1]
            print(f"   {n!r} ({total} lossy) vs {flac_folder!r} ({flacs} FLAC)")
            if flacs < total:
                print("     ! fewer FLAC tracks than lossy — leaving alone, check by hand")
                continue
            plan.move(os.path.join(music, n), SUPERSEDED)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--music", default=MUSIC)
    ap.add_argument("--apply", action="store_true", help="actually move things")
    args = ap.parse_args()

    if not os.path.isdir(args.music):
        sys.exit(f"no library at {args.music} — run this on the Pi")
    if not args.apply:
        print("DRY RUN — nothing will move. Re-run with --apply.\n")

    plan = Plan(args.apply)
    # Order matters: consolidating the duplicate spellings first gives the nested
    # lift a single unambiguous destination. The other way round, step 2 moved the
    # very file step 1 had just placed.
    merge_accent_duplicates(args.music, plan)
    print()
    tidy_nested(args.music, plan)
    print()
    supersede_lossy_duplicate(args.music, plan)
    print()
    print(f"{'moved' if args.apply else 'would move'} {plan.actions} item(s)")
    if args.apply:
        print("Rescan Navidrome.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
