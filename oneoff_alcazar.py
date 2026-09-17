#!/usr/bin/env python3
"""
One-off: rebuild the Mezzanine De L'Alcazar tracks into three real albums.

54 files claimed to be one album called "Mezzanine De L'alcazar". They are not.
They are single discs from three different volumes of a 2xCD series, which is
why every track number appeared four times over:

    Volume 2 (2002) disc 1   14 tracks
    Volume 3 (2003) disc 2   14 tracks
    Volume 4 (2005) disc 1   14 tracks
    Volume 4 (2005) disc 2   12 tracks

Every track was matched against the Discogs release for its volume, so the
artist, title, disc and position here are the published ones rather than
anything inferred. The tags on disk were close to useless: ARTIST was the
literal string "Varios" on all 54, the real artist was mashed into the title
("Herbert - I'll Do It"), and no file carried a disc number at all.

Two corroborations worth stating, because a title match alone would not be
enough to justify rewriting 54 files:

  - our own track number agreed with the Discogs disc position in every case
  - the one file that matched nothing was the one position left empty, Volume 4
    disc 2 track 8 ("Spririt" on disk, "Spirit" on the release)

Files are renamed to "<disc>-<track> <title>" and gathered under
Various Artists/<volume>/, because disc 1 track 1 and disc 2 track 1 would
otherwise collide in a single folder.

    scp oneoff_alcazar.py alcazar_map.json my-pi:~/
    ssh my-pi 'python3 oneoff_alcazar.py'            # dry run
    ssh my-pi 'python3 oneoff_alcazar.py --apply'
    ssh my-pi 'python3 oneoff_alcazar.py --restore <backup.json>'
"""

import argparse
import datetime
import json
import os
import re
import shutil
import sys

try:
    from mutagen import File
    from mutagen.id3 import TCMP
except ImportError:
    sys.exit("mutagen is required:  sudo apt install python3-mutagen")

MUSIC_PATH = os.environ.get("MUSIC_PATH", "/srv/music")
MAP_FILE = "alcazar_map.json"
ALBUM_ARTIST = "Various Artists"
# Every volume in this series is a 2xCD. Volumes 2 and 3 are represented here by
# a single disc each, but the total is still 2 — writing "disc 2 of 1" for the
# Volume 3 tracks would be nonsense, and tagging the real total means the missing
# disc slots straight in if it is ever bought.
DISC_TOTALS = {"Mezzanine De L'Alcazar Volume 2": 2,
               "Mezzanine De L'Alcazar Volume 3": 2,
               "Mezzanine De L'Alcazar Volume 4": 2}


def sanitize(name):
    """Filesystem-safe, readable, and identical in spirit to cd_automic.sh."""
    name = re.sub(r"[/:]", "-", name)
    name = re.sub(r'[\\?*<>|"]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "Untitled"


def read_tags(path):
    easy = File(path, easy=True)
    t = easy.tags or {}
    def g(k):
        v = t.get(k)
        return str(v[0]) if v else ""
    return {"path": path, "artist": g("artist"), "albumartist": g("albumartist"),
            "album": g("album"), "title": g("title"),
            "tracknumber": g("tracknumber"), "discnumber": g("discnumber"),
            "date": g("date")}


def apply_tags(path, row, disc_total):
    easy = File(path, easy=True)
    easy["album"] = row["album"]
    easy["albumartist"] = ALBUM_ARTIST
    easy["artist"] = row["artist"]
    easy["title"] = row["title"]
    easy["tracknumber"] = str(row["num"])
    easy["discnumber"] = f"{row['disc']}/{disc_total}"
    easy["date"] = row["year"]
    easy.save()
    # A genuine various-artists compilation, so the flag belongs here — unlike
    # Confidential, which only looked like one.
    raw = File(path)
    if raw.tags is None:
        return
    if path.lower().endswith((".m4a", ".mp4")):
        raw.tags["cpil"] = [True]
    else:
        raw.tags.setall("TCMP", [TCMP(encoding=0, text=["1"])])
    raw.save()


def locate(root, row):
    """Find a mapped file on disk, by the folder it was recorded in."""
    candidate = os.path.join(root, row["dir"], row["file"])
    if os.path.isfile(candidate):
        return candidate
    # Fall back to a search, in case a previous partial run moved it.
    for dp, _, fns in os.walk(root):
        if row["file"] in fns:
            return os.path.join(dp, row["file"])
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--music", default=MUSIC_PATH)
    ap.add_argument("--map", default=MAP_FILE)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restore", metavar="BACKUP.json")
    args = ap.parse_args()

    if args.restore:
        with open(args.restore, encoding="utf-8") as fh:
            data = json.load(fh)
        for mv in reversed(data["moves"]):
            if os.path.isfile(mv["to"]):
                os.makedirs(os.path.dirname(mv["from"]), exist_ok=True)
                shutil.move(mv["to"], mv["from"])
        for row in data["tags"]:
            p = row["path"]
            if not os.path.isfile(p):
                print(f"  ! missing: {p}")
                continue
            easy = File(p, easy=True)
            for k in ("artist", "albumartist", "album", "title",
                      "tracknumber", "discnumber", "date"):
                if row.get(k):
                    easy[k] = row[k]
                elif k in (easy.tags or {}):
                    del easy[k]
            easy.save()
        print(f"restored {len(data['tags'])} file(s)")
        return 0

    if not os.path.isdir(args.music):
        sys.exit(f"no library at {args.music} — run this on the music host")
    if not os.path.isfile(args.map):
        sys.exit(f"mapping not found: {args.map} (scp it alongside this script)")
    with open(args.map, encoding="utf-8") as fh:
        rows = json.load(fh)
    if not args.apply:
        print("DRY RUN — nothing will be written. Re-run with --apply.\n")

    backup, moves, missing = [], [], []
    by_album = {}
    for row in sorted(rows, key=lambda r: (r["album"], r["disc"], r["num"])):
        src = locate(args.music, row)
        if src is None:
            missing.append(row["file"])
            continue
        disc_total = DISC_TOTALS.get(row["album"], 1)
        dest_dir = os.path.join(args.music, ALBUM_ARTIST, sanitize(row["album"]))
        fname = f"{row['disc']}-{row['num']:02d} {sanitize(row['title'])}" \
                + os.path.splitext(src)[1]
        dest = os.path.join(dest_dir, fname)

        by_album.setdefault(row["album"], []).append(row)
        if len(by_album[row["album"]]) == 1:
            print(f"\n{row['album']} ({row['year']}, {disc_total} disc(s))")
        print(f"  d{row['disc']}t{row['num']:02d}  {row['artist'][:26]:<26} {row['title'][:34]}")

        backup.append(read_tags(src))
        if args.apply:
            os.makedirs(dest_dir, exist_ok=True)
            if os.path.abspath(src) != os.path.abspath(dest):
                shutil.move(src, dest)
                moves.append({"from": src, "to": dest})
            apply_tags(dest, row, disc_total)

    if missing:
        print(f"\n! {len(missing)} mapped file(s) not found on disk:")
        for f in missing[:8]:
            print(f"    {f}")

    if args.apply:
        # Clear out folders the moves emptied.
        for stale in ("Varios", "Various Artists"):
            d = os.path.join(args.music, stale, "Mezzanine De L'alcazar")
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        vd = os.path.join(args.music, "Varios")
        if os.path.isdir(vd) and not os.listdir(vd):
            os.rmdir(vd)

        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        out = f"tagbackup-alcazar-{stamp}.json"
        if os.path.exists(out):
            sys.exit(f"refusing to overwrite {out}")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"tags": backup, "moves": moves}, fh, indent=1)
        print(f"\nbackup written to {out}")
        print(f"undo with:  python3 {os.path.basename(__file__)} --restore {out}")

    verb = "rebuilt" if args.apply else "would rebuild"
    print(f"\n{verb} {len(backup)} track(s) into {len(by_album)} album(s)")
    if not args.apply:
        print("Nothing was written. Re-run with --apply.")
    else:
        print("Rescan Navidrome to pick the changes up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
