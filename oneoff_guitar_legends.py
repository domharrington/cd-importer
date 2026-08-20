#!/usr/bin/env python3
"""
One-off: reunite "Guitar Legends I" and "II" as one 2-disc album.

They are not two albums. MusicBrainz identifies them as discs 1 and 2 of
Capital Gold Guitar Legends (2004-11-22, GB) — 21 and 20 tracks, matching 19 and
16 titles in the same order — and the 1-xx / 2-xx filenames say the same thing.
Because every track carries its own original recording year (1955–2005), and
Navidrome groups albums by name + album artist + date, they currently appear as
29 separate albums.

This sets, across all 41 files:
    ALBUM         Capital Gold Guitar Legends
    ALBUMARTIST   Various Artists
    COMPILATION   1
    DATE          2004-11-22          (original year preserved per track)
    DISCNUMBER    1 or 2, from the filename prefix
    DISCTOTAL     2

Folders are left alone — Navidrome reads tags, not paths.

    scp oneoff_guitar_legends.py raspberrypi.local:~/
    ssh raspberrypi.local 'python3 oneoff_guitar_legends.py'          # dry run
    ssh raspberrypi.local 'python3 oneoff_guitar_legends.py --apply'

Undo with:  python3 oneoff_guitar_legends.py --restore <the backup it prints>
"""

import argparse
import datetime
import json
import os
import re
import sys

try:
    from mutagen.mp4 import MP4, MP4FreeForm
except ImportError:
    sys.exit("mutagen is required:  sudo apt install python3-mutagen")

ROOT = "/srv/shares/media/Music"
FOLDERS = ["Compilations/Guitar Legends I", "Compilations/Guitar Legends II"]
ALBUM = "Capital Gold Guitar Legends"
ALBUM_ARTIST = "Various Artists"
ALBUM_DATE = "2004-11-22"
DISC_TOTAL = 2
MP4_ORIGDATE = "----:com.apple.iTunes:ORIGINALDATE"


def disc_from_name(filename):
    """Disc number from the "N-NN" filename prefix these files already use."""
    m = re.match(r"^\s*(\d+)-(\d+)", filename)
    return int(m.group(1)) if m else None


def collect(root, folders):
    out = []
    for rel in folders:
        d = os.path.join(root, rel)
        if not os.path.isdir(d):
            print(f"⚠️  missing folder: {d}")
            continue
        for name in sorted(os.listdir(d)):
            if name.startswith("._") or not name.lower().endswith(".m4a"):
                continue
            disc = disc_from_name(name)
            if disc is None:
                print(f"⚠️  no disc prefix, skipping: {rel}/{name}")
                continue
            out.append((os.path.join(d, name), rel, name, disc))
    return out


def restore(backup_path):
    """Put every field this script touches back exactly as it was.

    repair_tags.py's restore only covers album artist, compilation and dates, so
    it would leave ALBUM and the disc numbers rewritten. This reverses all of it.
    """
    with open(backup_path, encoding="utf-8") as fh:
        data = json.load(fh)
    files = data.get("files", [])
    print(f"restoring {len(files)} file(s) from {backup_path}")
    ok = failed = 0
    for entry in files:
        path, saved = entry["path"], entry["tags"]
        if not os.path.exists(path):
            print(f"  ✗ gone: {path}")
            failed += 1
            continue
        try:
            audio = MP4(path)
            for atom, field in (("\xa9alb", "album"), ("aART", "albumartist"),
                                ("\xa9day", "date")):
                if saved.get(field):
                    audio[atom] = [saved[field]]
                else:
                    audio.pop(atom, None)
            if saved.get("compilation"):
                audio["cpil"] = True
            else:
                audio.pop("cpil", None)
            if saved.get("originaldate"):
                audio[MP4_ORIGDATE] = [MP4FreeForm(saved["originaldate"].encode("utf-8"))]
            else:
                audio.pop(MP4_ORIGDATE, None)
            for atom, field in (("disk", "disk"), ("trkn", "trkn")):
                pair = saved.get(field)
                if pair:
                    audio[atom] = [tuple(pair)]
                else:
                    audio.pop(atom, None)
            audio.save()
            ok += 1
        except Exception as err:
            failed += 1
            print(f"  ✗ {path}: {err}")
    print(f"✅ {ok} restored" + (f", {failed} failed" if failed else ""))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--backup", default=None)
    ap.add_argument("--restore", metavar="BACKUP.json", help="undo a previous run")
    args = ap.parse_args()

    if args.restore:
        return restore(args.restore)

    files = collect(args.root, FOLDERS)
    if not files:
        sys.exit("no files found — is --root correct? run this on the Pi")

    print(f"{'APPLYING' if args.apply else 'DRY RUN'}: {len(files)} file(s)\n")
    print(f"  ALBUM       -> {ALBUM}")
    print(f"  ALBUMARTIST -> {ALBUM_ARTIST}")
    print(f"  DATE        -> {ALBUM_DATE}   (each track's own year kept as ORIGINALDATE)")
    print(f"  DISCTOTAL   -> {DISC_TOTAL}\n")

    backup, plan = [], []
    for path, rel, name, disc in files:
        try:
            audio = MP4(path)
        except Exception as err:
            print(f"  ✗ {rel}/{name}: {err}")
            continue
        tags = audio.tags or {}
        before = {
            "album": (tags.get("\xa9alb") or [""])[0],
            "albumartist": (tags.get("aART") or [""])[0],
            "compilation": "1" if tags.get("cpil") else "",
            "date": (tags.get("\xa9day") or [""])[0],
            "originaldate": (tags.get(MP4_ORIGDATE) or [""])[0] if tags.get(MP4_ORIGDATE) else "",
            # Full pairs, not just the numbers: MP4 stores (number, total), and
            # a restore that dropped the totals would not be a true undo.
            "disk": list(tags.get("disk") or [(0, 0)])[0] if tags.get("disk") else None,
            "trkn": list(tags.get("trkn") or [(0, 0)])[0] if tags.get("trkn") else None,
            "track": str((tags.get("trkn") or [(0, 0)])[0][0]),
        }
        backup.append({"path": path, "tags": before})
        plan.append((path, disc, before))
        print(f"  d{disc} t{before['track']:>2}  {name[:44]:44} "
              f"{before['date'] or '(none)':>10} -> {ALBUM_DATE[:4]}")

    if not args.apply:
        print("\nNothing changed. Re-run with --apply.")
        return 0

    backup_path = args.backup or datetime.datetime.now().strftime(
        "tagbackup-guitarlegends-%Y%m%d-%H%M%S.json")
    with open(backup_path, "w", encoding="utf-8") as fh:
        json.dump({"root": args.root, "mode": "oneoff-guitar-legends",
                   "created": datetime.datetime.now().isoformat(),
                   "files": backup}, fh, indent=2)
    print(f"\n💾 tag backup: {backup_path}")

    changed = failed = 0
    for path, disc, before in plan:
        try:
            audio = MP4(path)
            audio["\xa9alb"] = [ALBUM]
            audio["aART"] = [ALBUM_ARTIST]
            audio["cpil"] = True
            # Keep the song's original year rather than discarding it.
            if before["date"] and not before["originaldate"]:
                audio[MP4_ORIGDATE] = [MP4FreeForm(before["date"].encode("utf-8"))]
            audio["\xa9day"] = [ALBUM_DATE]
            audio["disk"] = [(disc, DISC_TOTAL)]
            # Track numbers are already correct; left untouched.
            audio.save()
            changed += 1
        except Exception as err:
            failed += 1
            print(f"  ✗ {path}: {err}")

    print(f"✅ {changed} file(s) retagged" + (f", {failed} failed" if failed else ""))
    print(f"\nUndo with:  python3 oneoff_guitar_legends.py --restore {backup_path}")
    print("Rescan Navidrome: 29 album rows should become one 2-disc album.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
