#!/usr/bin/env python3
"""
Repair the tag problems that make Navidrome show one album as several.

Navidrome groups albums by tags, never by folder, so two distinct faults both
present as duplicate or fragmented albums:

  compilations  A various-artists compilation whose tracks are tagged with the
                track artist and no ALBUMARTIST. Each track becomes its own
                one-track album. ~250 of the 319 single-track albums are this.

  dates         Tracks in one album disagreeing on DATE — typically each song
                carries its original recording year, so "Guitar Legends II"
                (1973-1989) becomes 16 separate albums. Navidrome groups on
                name + album artist + release date, so any disagreement splits
                the album. 20 album groups, 389 tracks are affected.

Both modes are dry-run by default and write a JSON backup of every tag they
touch before touching it, so --restore can put things back.

Run this ON THE PI, where the files are local:

    scp repair_tags.py raspberrypi.local:~/
    ssh raspberrypi.local 'sudo apt install -y python3-mutagen'
    ssh -t raspberrypi.local './repair_tags.py dates --list'
    ssh -t raspberrypi.local './repair_tags.py dates --album "Guitar Legends II" --apply'
    ssh -t raspberrypi.local './repair_tags.py compilations --list'

(apt, not pip: the Pi runs Debian 12, whose Python is "externally managed", so
plain pip installs are refused. python3-mutagen is packaged at 1.46.0.)

Reversal:

    ssh -t raspberrypi.local './repair_tags.py restore tagbackup-....json'
"""

import argparse
import collections
import datetime
import json
import os
import re
import sys
import unicodedata

# Imported lazily so --help still works on a machine without mutagen (this
# script is meant to run on the Pi, but the CLI should be inspectable anywhere).
try:
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3, TCMP, TPE2
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False

AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".flac"}
VARIOUS = "Various Artists"
# MP4 has no standard "original date" atom, so use the iTunes freeform one.
MP4_ORIGDATE = "----:com.apple.iTunes:ORIGINALDATE"


# ── Reading and writing the three tag formats we have ────────────────────────

def read_tags(path):
    """The handful of fields this script cares about, normalised across formats."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            audio = MP3(path)
            tags = audio.tags or {}
            return {
                "artist": str(tags.get("TPE1", [""])[0]) if tags.get("TPE1") else "",
                "albumartist": str(tags.get("TPE2", [""])[0]) if tags.get("TPE2") else "",
                "album": str(tags.get("TALB", [""])[0]) if tags.get("TALB") else "",
                "compilation": str(tags.get("TCMP", [""])[0]) if tags.get("TCMP") else "",
                "track": str(tags.get("TRCK", [""])[0]).split("/")[0] if tags.get("TRCK") else "",
                "disc": str(tags.get("TPOS", [""])[0]).split("/")[0] if tags.get("TPOS") else "",
                "date": str(tags.get("TDRC", [""])[0]) if tags.get("TDRC") else "",
                "originaldate": str(tags.get("TDOR", [""])[0]) if tags.get("TDOR") else "",
            }
        if ext in (".m4a", ".mp4"):
            audio = MP4(path)
            t = audio.tags or {}
            return {
                "artist": (t.get("\xa9ART") or [""])[0],
                "albumartist": (t.get("aART") or [""])[0],
                "album": (t.get("\xa9alb") or [""])[0],
                "compilation": "1" if t.get("cpil") else "",
                "track": str((t.get("trkn") or [(0, 0)])[0][0]),
                "disc": str((t.get("disk") or [(0, 0)])[0][0]),
                "date": (t.get("\xa9day") or [""])[0],
                "originaldate": (t.get(MP4_ORIGDATE) or [""])[0]
                                 if t.get(MP4_ORIGDATE) else "",
            }
        if ext == ".flac":
            audio = FLAC(path)
            return {
                "artist": (audio.get("ARTIST") or [""])[0],
                "albumartist": (audio.get("ALBUMARTIST") or [""])[0],
                "album": (audio.get("ALBUM") or [""])[0],
                "compilation": (audio.get("COMPILATION") or [""])[0],
                "track": (audio.get("TRACKNUMBER") or [""])[0].split("/")[0],
                "disc": (audio.get("DISCNUMBER") or [""])[0].split("/")[0],
                "date": (audio.get("DATE") or [""])[0],
                "originaldate": (audio.get("ORIGINALDATE") or [""])[0],
            }
    except Exception as err:                                  # unreadable / corrupt
        return {"error": str(err)}
    return {"error": f"unsupported extension {ext}"}


def write_compilation_tags(path, album_artist=VARIOUS):
    """Set album artist + compilation flag, leaving the track artist untouched."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        try:
            tags = ID3(path)
        except Exception:
            tags = ID3()
        tags.setall("TPE2", [TPE2(encoding=3, text=[album_artist])])
        tags.setall("TCMP", [TCMP(encoding=3, text=["1"])])     # iTunes compilation
        tags.save(path)
    elif ext in (".m4a", ".mp4"):
        audio = MP4(path)
        audio["aART"] = [album_artist]
        audio["cpil"] = True
        audio.save()
    elif ext == ".flac":
        audio = FLAC(path)
        audio["ALBUMARTIST"] = [album_artist]
        audio["COMPILATION"] = ["1"]
        audio.save()
    else:
        raise ValueError(f"unsupported extension {ext}")


def write_album_date(path, album_date, keep_original=True):
    """Give every track the album's release date.

    The per-track value is preserved in the "original date" tag, which is where
    a song's original recording year belongs — losing it would be destroying
    real information, and it is what made these albums fragment in the first
    place.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        from mutagen.id3 import TDRC, TDOR
        tags = ID3(path)
        previous = str(tags.get("TDRC", [""])[0]) if tags.get("TDRC") else ""
        if keep_original and previous and not tags.get("TDOR"):
            tags.setall("TDOR", [TDOR(encoding=3, text=[previous])])
        tags.setall("TDRC", [TDRC(encoding=3, text=[album_date])])
        tags.save(path)
    elif ext in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4FreeForm
        audio = MP4(path)
        previous = (audio.get("\xa9day") or [""])[0]
        if keep_original and previous and MP4_ORIGDATE not in audio:
            audio[MP4_ORIGDATE] = [MP4FreeForm(str(previous).encode("utf-8"))]
        audio["\xa9day"] = [album_date]
        audio.save()
    elif ext == ".flac":
        audio = FLAC(path)
        previous = (audio.get("DATE") or [""])[0]
        if keep_original and previous and "ORIGINALDATE" not in audio:
            audio["ORIGINALDATE"] = [previous]
        audio["DATE"] = [album_date]
        audio.save()
    else:
        raise ValueError(f"unsupported extension {ext}")


def restore_tags(path, saved):
    """Put back exactly what was there before, including absent fields."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        from mutagen.id3 import TDRC, TDOR
        tags = ID3(path)
        for frame, field in (("TDRC", "date"), ("TDOR", "originaldate")):
            cls = TDRC if frame == "TDRC" else TDOR
            if saved.get(field):
                tags.setall(frame, [cls(encoding=3, text=[saved[field]])])
            else:
                tags.delall(frame)
        if saved.get("albumartist"):
            tags.setall("TPE2", [TPE2(encoding=3, text=[saved["albumartist"]])])
        else:
            tags.delall("TPE2")
        if saved.get("compilation"):
            tags.setall("TCMP", [TCMP(encoding=3, text=[saved["compilation"]])])
        else:
            tags.delall("TCMP")
        tags.save(path)
    elif ext in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4FreeForm
        audio = MP4(path)
        if saved.get("date"):
            audio["\xa9day"] = [saved["date"]]
        else:
            audio.pop("\xa9day", None)
        if saved.get("originaldate"):
            audio[MP4_ORIGDATE] = [MP4FreeForm(saved["originaldate"].encode("utf-8"))]
        else:
            audio.pop(MP4_ORIGDATE, None)
        if saved.get("albumartist"):
            audio["aART"] = [saved["albumartist"]]
        else:
            audio.pop("aART", None)
        if saved.get("compilation"):
            audio["cpil"] = True
        else:
            audio.pop("cpil", None)
        audio.save()
    elif ext == ".flac":
        audio = FLAC(path)
        for key, field in (("ALBUMARTIST", "albumartist"), ("COMPILATION", "compilation"),
                           ("DATE", "date"), ("ORIGINALDATE", "originaldate")):
            if saved.get(field):
                audio[key] = [saved[field]]
            else:
                audio.pop(key, None)
        audio.save()


# ── Finding the scattered tracks ─────────────────────────────────────────────

def scan(root):
    """(artist_folder, album_folder) -> [full paths]"""
    albums = collections.defaultdict(list)
    for artist in sorted(os.listdir(root)):
        apath = os.path.join(root, artist)
        if not os.path.isdir(apath):
            continue
        for album in sorted(os.listdir(apath)):
            alpath = os.path.join(apath, album)
            if not os.path.isdir(alpath):
                continue
            for name in sorted(os.listdir(alpath)):
                if name.startswith("._"):
                    continue
                if os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                    albums[(artist, album)].append(os.path.join(alpath, name))
    return albums


def shattered(albums, min_artists=3):
    """Album titles spread across several artist folders as one compilation.

    Two earlier attempts at this were wrong in opposite directions:

      - counting only single-track folders missed compilations where an artist
        contributed two tracks, leaving the album split rather than unified;
      - excluding groups whose track numbers repeat lost multi-disc
        compilations entirely, since a 3-disc set with no disc numbers has each
        number three times.

    The dependable signal is how many *distinct* artists share the title. A
    compilation has many; an album that merely shares its name with another
    ("Veneer" under both "José González" and "Jose Gonzalez") has one or two.
    Accents are folded so those duplicate spellings count once. Anything above
    the threshold is still shown for review before it is written.
    """
    def fold(name):
        decomposed = unicodedata.normalize("NFKD", name)
        return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()

    by_title = collections.defaultdict(dict)
    for (artist, album), paths in albums.items():
        if album.strip().lower() in ("unknown album", "unknown", ""):
            continue
        by_title[album][artist] = paths

    out = {}
    for title, per_artist in by_title.items():
        if len({fold(a) for a in per_artist}) >= min_artists:
            out[title] = sorted(per_artist)
    return out


# ── Actions ──────────────────────────────────────────────────────────────────

def looks_like_two_albums(paths):
    """True if a folder holds more than one album.

    Detected by colliding (disc, track) numbers: a real multi-disc album has
    track 1 on each disc, but never two "disc 1 track 1"s. When this is true the
    differing DATE is the only thing keeping the two albums apart in Navidrome,
    so unifying it would merge them — the opposite of a repair.
    """
    def as_int(value, default=0):
        # Numeric comparison: TPOS "01" and "1" are the same disc, and a missing
        # disc number conventionally means disc 1. Treating it as 0 let two
        # colliding track-1s slip past this check.
        try:
            return int(str(value or "").strip().split("/")[0] or default)
        except ValueError:
            return default

    seen = collections.Counter()
    for p in paths:
        tags = read_tags(p)
        if "error" in tags:
            continue
        track = as_int(tags.get("track"))
        if not track:
            continue
        seen[(as_int(tags.get("disc"), 1) or 1, track)] += 1
    return [k for k, n in seen.items() if n > 1]


def date_split_albums(root):
    """Album folders whose tracks disagree on DATE, so Navidrome splits them."""
    out = {}
    for key, paths in scan(root).items():
        dates = collections.Counter()
        for p in paths:
            tags = read_tags(p)
            if "error" in tags:
                continue
            # Year granularity: "1976-05-01" and "1976" are the same release.
            dates[(tags.get("date") or "")[:4]] += 1
        real = {d for d in dates if d}
        blank = sum(n for d, n in dates.items() if not d)
        # A mix of "has a date" and "has none" splits the album just as surely as
        # two different years — Sick Music had 19 undated tracks and 2 from 2009,
        # and only comparing the dates that exist missed it entirely.
        if len(real) > 1 or (real and blank):
            out[key] = {"paths": paths, "dates": dates}
    return out


def choose_album_date(dates):
    """The album's release date, inferred from the per-track years.

    The most common year wins. Taking the latest looks reasonable for
    compilations but is wrong wherever one track is mis-tagged: a single stray
    2016 would redate a 2006 album. Ties break to the earliest.
    """
    real = {y: n for y, n in dates.items() if y}
    if not real:
        return ""
    best = max(real.values())
    if best == 1:
        # Every track a different year: a genuine compilation spanning decades.
        # The release year is not in the tags, so refuse to invent one.
        return ""
    return sorted(y for y, n in real.items() if n == best)[0]


def describe_dates(info):
    """One line showing the spread, so an override is a visible choice."""
    counts = {y: n for y, n in info["dates"].items() if y}
    blank = sum(n for y, n in info["dates"].items() if not y)
    spread = ", ".join(f"{y}×{n}" for y, n in sorted(counts.items()))
    if blank:
        spread += f"{', ' if spread else ''}(no date)×{blank}"
    return counts, blank, spread


def do_list_dates(root):
    splits = date_split_albums(root)
    total = sum(len(v["paths"]) for v in splits.values())
    print(f"{len(splits)} album folder(s) split by inconsistent DATE, {total} tracks\n")
    for (artist, album), info in sorted(splits.items(),
                                        key=lambda kv: -len(kv[1]["dates"])):
        counts, blank, spread = describe_dates(info)
        chosen = choose_album_date(info["dates"])
        print(f"  {len(counts):3d} distinct years  {artist}/{album}")
        print(f"       {spread}")
        if looks_like_two_albums(info["paths"]):
            print("       -> ⛔ more than one album in this folder; do NOT unify "
                  "the date, split the folder instead")
        elif not chosen:
            print("       -> no majority year: pass --date YYYY (the release year)")
        else:
            confident = counts.get(chosen, 0) * 2 > sum(counts.values()) + blank
            print(f"       -> DATE={chosen}"
                  f"{'  (majority)' if confident else '  (weak majority — check)'}")
    print("\nRepair one with:  dates --album \"<album folder>\" --apply")
    return 0


def do_repair_dates(root, titles, apply_changes, backup_path, forced_date):
    splits = date_split_albums(root)
    targets = []
    for title in titles:
        matches = [k for k in splits if k[1].lower() == title.lower()]
        if not matches:
            print(f"⚠️  no date-split album folder called {title!r}; try dates --list")
            continue
        targets.extend(matches)
    if not targets:
        print("nothing to do")
        return 1

    print(f"{'APPLYING' if apply_changes else 'DRY RUN'}: {len(targets)} album(s)\n")
    backup, plan = [], []
    for key in targets:
        info = splits[key]
        collisions = looks_like_two_albums(info["paths"])
        if collisions:
            print(f"  ⛔ {key[0]}/{key[1]}: {len(collisions)} duplicated track "
                  f"number(s) — this folder holds more than one album, and the "
                  f"differing DATE is what keeps them apart.")
            print("     Unifying it would merge them. Split the folder and give "
                  "each album its own ALBUM tag instead.")
            continue
        album_date = forced_date or choose_album_date(info["dates"])
        if not album_date:
            print(f"  ⚠️  {key[0]}/{key[1]}: every track has a different year, so "
                  f"the release year cannot be inferred. Re-run with --date YYYY.")
            continue
        counts, blank, spread = describe_dates(info)
        chosen_n = counts.get(album_date, 0)
        note = ("majority" if chosen_n * 2 > sum(counts.values()) + blank
                else f"NOT the majority — only {chosen_n} track(s) already say this")
        print(f"  {key[0]}/{key[1]}")
        print(f"       currently: {spread}")
        print(f"       -> DATE={album_date}  ({note})")
        for path in info["paths"]:
            before = read_tags(path)
            if "error" in before:
                print(f"    ✗ {os.path.basename(path)} — {before['error']}")
                continue
            backup.append({"path": path, "tags": before})
            plan.append((path, album_date))
            was = before.get("date") or "(none)"
            if was[:4] != album_date[:4]:
                print(f"    {os.path.basename(path)[:52]:52} {was} -> {album_date}"
                      f"  (keeps {was} as original date)")

    if not plan:
        return 1
    if not apply_changes:
        print("\nNothing changed. Re-run with --apply.")
        return 0

    if os.path.exists(backup_path):
        sys.exit(f"refusing to overwrite an existing backup: {backup_path}")
    with open(backup_path, "w", encoding="utf-8") as fh:
        json.dump({"root": root, "mode": "dates",
                   "created": datetime.datetime.now().isoformat(),
                   "files": backup}, fh, indent=2)
    print(f"\n💾 tag backup: {backup_path}")

    changed = failed = 0
    for path, album_date in plan:
        try:
            write_album_date(path, album_date)
            changed += 1
        except Exception as err:
            failed += 1
            print(f"  ✗ {path}: {err}")
    print(f"✅ {changed} track(s) retagged" + (f", {failed} failed" if failed else ""))
    print("\nRescan Navidrome: the split albums should collapse into one each.")
    return 0


def do_list(root):
    albums = scan(root)
    groups = shattered(albums)
    total = sum(len(v) for v in groups.values())
    print(f"{len(groups)} shattered compilation(s) covering {total} tracks\n")
    for title, artists in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        tracks = sum(len(albums[(a, title)]) for a in artists)
        print(f"  {len(artists):3d} artists, {tracks:3d} tracks  {title}")
    print("\nRepair one with:  --album \"<title>\" --apply")
    return 0


def do_repair(root, titles, apply_changes, consolidate, backup_path, album_date=None):
    albums = scan(root)
    groups = shattered(albums)

    targets = []
    for title in titles:
        match = next((t for t in groups if t.lower() == title.lower()), None)
        if not match:
            print(f"⚠️  no shattered compilation called {title!r}; try --list")
            continue
        for artist in groups[match]:
            targets.extend(albums[(artist, match)])
    if not targets:
        print("nothing to do")
        return 1

    print(f"{'APPLYING' if apply_changes else 'DRY RUN'}: "
          f"{len(targets)} track(s) across {len(titles)} album(s)\n")

    backup = []
    for path in targets:
        before = read_tags(path)
        if "error" in before:
            print(f"  ✗ {os.path.relpath(path, root)} — unreadable: {before['error']}")
            continue
        backup.append({"path": path, "tags": before})
        rel = os.path.relpath(path, root)
        print(f"  {rel}")
        print(f"      artist={before['artist']!r}  albumartist={before['albumartist']!r}"
              f" -> {VARIOUS!r}, compilation=1"
              + (f", date={before.get('date') or '(none)'} -> {album_date}" if album_date else ""))

    if not apply_changes:
        print("\nNothing changed. Re-run with --apply.")
        return 0

    # The backup is written before any file is touched, so a crash midway is
    # still fully reversible.
    if os.path.exists(backup_path):
        sys.exit(f"refusing to overwrite an existing backup: {backup_path}\n"
                 "pass a different --backup path")
    with open(backup_path, "w", encoding="utf-8") as fh:
        json.dump({"root": root, "created": datetime.datetime.now().isoformat(),
                   "files": backup}, fh, indent=2)
    print(f"\n💾 tag backup: {backup_path}")

    changed = failed = 0
    for entry in backup:
        try:
            write_compilation_tags(entry["path"])
            # A compilation's tracks must also agree on DATE, or Navidrome splits
            # it by release date regardless of the album artist. The disagreement
            # lives across artist folders here, which the dates mode cannot see.
            if album_date:
                write_album_date(entry["path"], album_date)
            changed += 1
        except Exception as err:
            failed += 1
            print(f"  ✗ {entry['path']}: {err}")
    print(f"✅ {changed} track(s) retagged" + (f", {failed} failed" if failed else ""))

    if consolidate:
        moved = consolidate_files(root, titles, backup)
        print(f"📁 {moved} file(s) moved into '{VARIOUS}/<album>/'")

    print("\nRescan Navidrome to see the change.")
    return 0


def consolidate_files(root, titles, entries):
    """Optional: gather the tracks into one folder per compilation.

    Purely cosmetic — Navidrome groups on tags. Never overwrites: a name clash
    gets a numeric suffix.
    """
    moved = 0
    for entry in entries:
        path = entry["path"]
        album = os.path.basename(os.path.dirname(path))
        if not any(album.lower() == t.lower() for t in titles):
            continue
        dest_dir = os.path.join(root, VARIOUS, album)
        os.makedirs(dest_dir, exist_ok=True)
        base = os.path.basename(path)
        dest = os.path.join(dest_dir, base)
        stem, ext = os.path.splitext(base)
        n = 2
        while os.path.exists(dest):
            dest = os.path.join(dest_dir, f"{stem} ({n}){ext}")
            n += 1
        os.rename(path, dest)
        moved += 1
        old_dir = os.path.dirname(path)
        for d in (old_dir, os.path.dirname(old_dir)):
            try:
                os.rmdir(d)          # only succeeds when genuinely empty
            except OSError:
                break
    return moved


def do_restore(backup_path):
    with open(backup_path, encoding="utf-8") as fh:
        data = json.load(fh)
    files = data.get("files", [])
    print(f"restoring tags on {len(files)} file(s) from {backup_path}")
    ok = missing = failed = 0
    for entry in files:
        path = entry["path"]
        if not os.path.exists(path):
            missing += 1
            print(f"  ✗ moved or gone: {path}")
            continue
        try:
            restore_tags(path, entry["tags"])
            ok += 1
        except Exception as err:
            failed += 1
            print(f"  ✗ {path}: {err}")
    print(f"✅ {ok} restored"
          + (f", {missing} missing" if missing else "")
          + (f", {failed} failed" if failed else ""))
    if missing:
        print("   (--consolidate moved files; restore their tags by path manually)")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Repair tag faults that make Navidrome split or duplicate albums",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default="/srv/shares/media/Music",
                    help="library root ON THIS MACHINE (default: the Pi's path)")
    sub = ap.add_subparsers(dest="mode", required=True)

    p_comp = sub.add_parser("compilations",
                            help="fix various-artists compilations scattered across artist folders")
    p_comp.add_argument("--list", action="store_true", help="show what is broken")
    p_comp.add_argument("--album", action="append", default=[])
    p_comp.add_argument("--apply", action="store_true")
    p_comp.add_argument("--consolidate", action="store_true",
                        help="also move files into one folder per album (cosmetic)")
    p_comp.add_argument("--date", default=None,
                        help="also set the album's release date (per-track years "
                             "are kept as the original date)")
    p_comp.add_argument("--backup", default=None)

    p_date = sub.add_parser("dates",
                            help="unify DATE within an album so Navidrome stops splitting it")
    p_date.add_argument("--list", action="store_true", help="show what is broken")
    p_date.add_argument("--album", action="append", default=[])
    p_date.add_argument("--apply", action="store_true")
    p_date.add_argument("--date", default=None,
                        help="force this date instead of the latest year found")
    p_date.add_argument("--backup", default=None)

    p_rest = sub.add_parser("restore", help="undo a previous repair")
    p_rest.add_argument("backup", metavar="BACKUP.json")

    args = ap.parse_args()

    if not MUTAGEN_AVAILABLE:
        sys.exit("mutagen is required:  sudo apt install python3-mutagen\n"
                 "(apt, not pip — Debian 12's Python refuses plain pip installs)")

    if args.mode == "restore":
        return do_restore(args.backup)
    if not os.path.isdir(args.path):
        sys.exit(f"not a directory: {args.path}\nRun this on the Pi, or pass --path.")

    # Include the album and microseconds: a per-second stamp collided when three
    # albums were repaired in one batch, and the third backup overwrote the first
    # two — destroying the very safety net this is here to provide.
    def default_backup_name(albums):
        slug = "-".join(re.sub(r"[^A-Za-z0-9]+", "", a)[:18] for a in albums[:2]) or "repair"
        return datetime.datetime.now().strftime(f"tagbackup-{slug}-%Y%m%d-%H%M%S-%f.json")
    if args.mode == "compilations":
        if args.list or not args.album:
            return do_list(args.path)
        return do_repair(args.path, args.album, args.apply, args.consolidate,
                         args.backup or default_backup_name(args.album), args.date)
    if args.mode == "dates":
        if args.list or not args.album:
            return do_list_dates(args.path)
        return do_repair_dates(args.path, args.album, args.apply,
                               args.backup or default_backup_name(args.album), args.date)
    return 1


if __name__ == "__main__":
    sys.exit(main())
