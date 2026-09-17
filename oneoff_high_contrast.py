#!/usr/bin/env python3
"""
One-off: undo a bad compilation call, and rehome a stray remix.

Two unrelated problems that both trace back to High Contrast.

1. "Confidential" is NOT a various-artists compilation, but an earlier run of
   repair_tags.py labelled it one. It is a 2-disc High Contrast album
   (2009-04-27): disc 1 is his own twelve tracks, disc 2 is his remixes of other
   people. The repair heuristic — one album title spread across three or more
   artists, a track each — describes a remix disc exactly as well as it
   describes a compilation, so it fired wrongly here. Setting ALBUMARTIST to
   Various Artists is why the album shows the wrong image.

   It also has no DISCNUMBER at all, so both discs' numbering collides inside one
   album. The track totals give the discs away: 1/12..12/12 is disc one,
   2/13..13/13 is disc two.

2. "High Contrast (Adele)" is an artist folder holding one file — a 64 kbps
   Hometown Glory, which is the High Contrast remix with the remixer glued into
   the artist tag. It gets filed under Adele as its own single. Deliberately NOT
   into Adele/19, which is a clean FLAC rip: dropping a 64 kbps remix in there
   would both duplicate the track and mix formats inside one album.

    scp oneoff_high_contrast.py my-pi:~/
    ssh my-pi 'python3 oneoff_high_contrast.py'            # dry run
    ssh my-pi 'python3 oneoff_high_contrast.py --apply'
    ssh my-pi 'python3 oneoff_high_contrast.py --restore <backup.json>'
"""

import argparse
import datetime
import json
import os
import shutil
import sys

try:
    from mutagen import File
    from mutagen.id3 import TCMP, TPOS, TPE2
except ImportError:
    sys.exit("mutagen is required:  sudo apt install python3-mutagen")

MUSIC_PATH = os.environ.get("MUSIC_PATH", "/srv/music")

ALBUM = "Confidential"
ALBUM_ARTIST = "High Contrast"
DISC_TOTAL = 2

STRAY_FOLDER = "High Contrast (Adele)"
STRAY_ARTIST = "Adele"
STRAY_TITLE = "Hometown Glory (High Contrast Remix)"

AUDIO = (".mp3", ".m4a", ".mp4", ".flac")


def audio_files(directory):
    if not os.path.isdir(directory):
        return []
    out = []
    for root, _, names in os.walk(directory):
        for n in sorted(names):
            if not n.startswith("._") and n.lower().endswith(AUDIO):
                out.append(os.path.join(root, n))
    return out


def read_tags(path):
    """Everything we might overwrite, so a restore can put it all back."""
    easy, raw = File(path, easy=True), File(path)
    t = easy.tags or {}
    def g(k):
        v = t.get(k)
        return str(v[0]) if v else ""
    compilation = ""
    if raw.tags is not None:
        if "TCMP" in raw.tags:
            compilation = str(raw.tags["TCMP"])
        elif "cpil" in raw.tags:
            compilation = str(raw.tags["cpil"])
    return {
        "path": path, "artist": g("artist"), "albumartist": g("albumartist"),
        "album": g("album"), "title": g("title"),
        "tracknumber": g("tracknumber"), "discnumber": g("discnumber"),
        "compilation": compilation,
    }


def find_confidential(root):
    """Every Confidential track, wherever its artist folder put it."""
    hits = []
    for artist in sorted(os.listdir(root)):
        d = os.path.join(root, artist, ALBUM)
        hits.extend(audio_files(d))
    return hits


def disc_for(path):
    """Disc number from the track total: /12 is disc one, /13 is disc two."""
    easy = File(path, easy=True)
    raw = (easy.tags or {}).get("tracknumber")
    text = str(raw[0]) if raw else ""
    if "/" in text:
        total = text.split("/", 1)[1].strip()
        if total.isdigit():
            return 1 if int(total) == 12 else 2
    return None


def set_tags(path, changes):
    """Apply easy-interface tags plus the compilation flag, format-appropriately."""
    easy = File(path, easy=True)
    for key, value in changes.items():
        if key == "compilation":
            continue
        easy[key] = value
    easy.save()

    if "compilation" in changes:
        raw = File(path)
        want = changes["compilation"]
        if raw.tags is None:
            return
        if path.lower().endswith((".m4a", ".mp4")):
            if want:
                raw.tags["cpil"] = [True]
            else:
                raw.tags.pop("cpil", None)
        else:
            if want:
                raw.tags.setall("TCMP", [TCMP(encoding=0, text=["1"])])
            else:
                raw.tags.delall("TCMP")
        raw.save()


def fix_confidential(root, apply_changes, backup):
    files = find_confidential(root)
    if not files:
        print(f"  no '{ALBUM}' tracks found under {root}")
        return 0
    print(f"  {len(files)} '{ALBUM}' tracks across "
          f"{len({os.path.basename(os.path.dirname(os.path.dirname(f))) for f in files})} "
          f"artist folders")
    changed = 0
    for path in files:
        before = read_tags(path)
        disc = disc_for(path)
        if disc is None:
            print(f"     ! no track total, skipping: {os.path.relpath(path, root)}")
            continue
        backup.append(before)
        changes = {
            "albumartist": ALBUM_ARTIST,
            "discnumber": f"{disc}/{DISC_TOTAL}",
            "compilation": False,
        }
        rel = os.path.relpath(path, root)
        print(f"     disc {disc}  {before['tracknumber']:<7} {rel}")
        if apply_changes:
            set_tags(path, changes)
        changed += 1
    return changed


def fix_stray(root, apply_changes, backup, moves):
    src_dir = os.path.join(root, STRAY_FOLDER)
    files = audio_files(src_dir)
    if not files:
        print(f"  nothing to do — no '{STRAY_FOLDER}' folder")
        return 0
    dest_dir = os.path.join(root, STRAY_ARTIST, STRAY_TITLE)
    for path in files:
        backup.append(read_tags(path))
        dest = os.path.join(dest_dir, os.path.basename(path))
        print(f"     {os.path.relpath(path, root)}")
        print(f"       -> {os.path.relpath(dest, root)}")
        if apply_changes:
            os.makedirs(dest_dir, exist_ok=True)
            shutil.move(path, dest)
            set_tags(dest, {
                "artist": STRAY_ARTIST,
                "albumartist": STRAY_ARTIST,
                "album": STRAY_TITLE,
                "title": STRAY_TITLE,
            })
            moves.append({"from": path, "to": dest})
    if apply_changes:
        # The folder also holds an artist.jpg for an artist that never existed.
        for leftover in ("artist.jpg", "artist.jpeg", "artist.png"):
            p = os.path.join(src_dir, leftover)
            if os.path.isfile(p):
                os.remove(p)
        for root_dir, dirs, names in os.walk(src_dir, topdown=False):
            if not os.listdir(root_dir):
                os.rmdir(root_dir)
    return len(files)


def restore(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    for mv in reversed(data.get("moves", [])):
        if os.path.isfile(mv["to"]):
            os.makedirs(os.path.dirname(mv["from"]), exist_ok=True)
            shutil.move(mv["to"], mv["from"])
            print(f"  moved back {os.path.basename(mv['from'])}")
    for row in data.get("tags", []):
        p = row["path"]
        if not os.path.isfile(p):
            print(f"  ! missing, cannot restore: {p}")
            continue
        easy = File(p, easy=True)
        for k in ("artist", "albumartist", "album", "title",
                  "tracknumber", "discnumber"):
            if row.get(k):
                easy[k] = row[k]
            elif k in (easy.tags or {}):
                del easy[k]
        easy.save()
        raw = File(p)
        if raw.tags is not None and row.get("compilation"):
            if p.lower().endswith((".m4a", ".mp4")):
                raw.tags["cpil"] = [True]
            else:
                raw.tags.setall("TCMP", [TCMP(encoding=0, text=["1"])])
            raw.save()
    print(f"restored {len(data.get('tags', []))} file(s)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--music", default=MUSIC_PATH)
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default: dry run)")
    ap.add_argument("--restore", metavar="BACKUP.json")
    args = ap.parse_args()

    if args.restore:
        return restore(args.restore)
    if not os.path.isdir(args.music):
        sys.exit(f"no library at {args.music} — run this on the music host")
    if not args.apply:
        print("DRY RUN — nothing will be written. Re-run with --apply.\n")

    backup, moves = [], []
    print(f"1. {ALBUM}: -> albumartist '{ALBUM_ARTIST}', compilation off, "
          f"disc 1/2 from the track totals")
    n1 = fix_confidential(args.music, args.apply, backup)
    print(f"\n2. {STRAY_FOLDER}: -> {STRAY_ARTIST}/{STRAY_TITLE}")
    n2 = fix_stray(args.music, args.apply, backup, moves)

    if args.apply and backup:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        out = f"tagbackup-highcontrast-{stamp}.json"
        if os.path.exists(out):
            sys.exit(f"refusing to overwrite {out}")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"tags": backup, "moves": moves}, fh, indent=1)
        print(f"\nbackup written to {out}")
        print(f"undo with:  python3 {os.path.basename(__file__)} --restore {out}")

    verb = "changed" if args.apply else "would change"
    print(f"\n{verb} {n1} Confidential track(s) and {n2} stray file(s)")
    if not args.apply:
        print("Nothing was written. Re-run with --apply.")
    else:
        print("Rescan Navidrome to pick the changes up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
