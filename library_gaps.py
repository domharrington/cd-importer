#!/usr/bin/env python3
"""
Find albums in the library that are missing tracks, from the tags on disk.

Track numbers give this away without asking anything external: an album holding
tracks 1, 3 and 7 is missing four, whatever MusicBrainz thinks. Where a file
carries a track total (`3/12`) the answer is exact; otherwise the highest track
number present is used as the ceiling, which under-reports rather than invents.

Used by spotify_buylist.py so a pruned album still reaches the buy list even if
it was never streamed — the two sources answer different questions:

    streaming history   "you play this and don't own it"
    track-number gaps   "you own part of this and the rest is missing"

    ./library_gaps.py                      # human-readable report
    ./library_gaps.py --json               # machine-readable, for the buy list

── Compilations are excluded, and must be ──────────────────────────────────────

A various-artists compilation whose tracks carry the *track* artist looks like
dozens of one-track albums, each "missing" 20-plus tracks. Left in, they drown
the real signal completely: a first pass on this library returned 285 "albums
with gaps", of which the overwhelming majority were four compilations. Three
tests exclude them — the compilation flag, a various-artists album artist, and
an album title appearing under more than one album artist.
"""

import argparse
import json
import subprocess
import sys

import localconfig

# Runs on the music host, where the files are. Kept to the standard library plus
# mutagen (`sudo apt install python3-mutagen`) so nothing needs installing.
REMOTE_SCANNER = r'''
import collections, json, os, re, sys
try:
    from mutagen import File
except ImportError:
    print(json.dumps({"error": "mutagen not installed: sudo apt install python3-mutagen"}))
    raise SystemExit(0)

ROOT = sys.argv[1]
AUDIO = (".mp3", ".m4a", ".mp4", ".flac", ".wma", ".ogg", ".opus", ".wav")
VARIOUS = {"various artists", "various", "va", "original soundtrack",
           "soundtrack", "compilations"}

def tag(t, key):
    if not t or key not in t:
        return ""
    v = t[key]
    v = v[0] if isinstance(v, list) else v
    return str(v).strip()

def number(text):
    """'3/12' -> (3, 12); '03' -> (3, None)."""
    m = re.match(r"\s*(\d+)\s*(?:/\s*(\d+))?", str(text or ""))
    if not m:
        return None, None
    return int(m.group(1)), (int(m.group(2)) if m.group(2) else None)

rows, unreadable = [], 0
for dirpath, _, filenames in os.walk(ROOT):
    for fn in filenames:
        if fn.startswith("._") or not fn.lower().endswith(AUDIO):
            continue
        path = os.path.join(dirpath, fn)
        try:
            easy, raw = File(path, easy=True), File(path)
        except Exception:
            unreadable += 1
            continue
        if easy is None:
            unreadable += 1
            continue
        t = easy.tags or {}
        album = tag(t, "album")
        if not album:
            continue
        n, total = number(tag(t, "tracknumber"))
        disc, _ = number(tag(t, "discnumber"))
        rt = raw.tags or {}
        compilation = bool(
            (rt.get("TCMP") if hasattr(rt, "get") else None)
            or (rt.get("cpil") if hasattr(rt, "get") else None)
            or tag(t, "compilation")
        )
        rows.append({
            "album": album,
            "artist": tag(t, "albumartist") or tag(t, "artist"),
            "n": n, "total": total, "disc": disc or 1,
            "compilation": compilation,
            "dir": os.path.relpath(dirpath, ROOT),
        })

# An album title carried by several album artists is a compilation whose tracks
# were never re-tagged, not many albums that happen to share a title.
artists_per_title = collections.defaultdict(set)
for r in rows:
    artists_per_title[r["album"].lower()].add(r["artist"].lower())

groups = collections.defaultdict(list)
excluded = 0
for r in rows:
    if (r["compilation"] or r["artist"].lower() in VARIOUS
            or len(artists_per_title[r["album"].lower()]) > 1):
        excluded += 1
        continue
    groups[(r["artist"].lower(), r["album"].lower(), r["disc"])].append(r)

gaps, dupes = [], []
for ts in groups.values():
    nums = [x["n"] for x in ts if x["n"]]
    if not nums or len(ts) < 2:
        continue
    totals = [x["total"] for x in ts if x["total"]]
    total = max(set(totals), key=totals.count) if totals else None
    ceiling = max(max(nums), total or 0)
    unique = set(nums)
    missing = [n for n in range(1, ceiling + 1) if n not in unique]

    # A track number used twice means two files for one track. Counting files as
    # "tracks held" hid this: an album showing 12 files of 12 could be 11 real
    # tracks plus a duplicate, which reads as complete when it is not.
    repeated = sorted(n for n, c in collections.Counter(nums).items() if c > 1)
    if repeated:
        dupes.append({
            "artist": ts[0]["artist"], "album": ts[0]["album"],
            "disc": ts[0]["disc"], "numbers": repeated,
            "files": len(ts), "unique": len(unique), "dir": ts[0]["dir"],
        })
    if not missing:
        continue
    gaps.append({
        "artist": ts[0]["artist"], "album": ts[0]["album"], "disc": ts[0]["disc"],
        "have": len(unique), "files": len(ts), "duplicates": repeated,
        "expected": ceiling, "missing": missing,
        "exact": bool(total), "dir": ts[0]["dir"],
    })

print(json.dumps({
    "gaps": gaps,
    "duplicates": dupes,
    "scanned": len(rows),
    "excluded_compilation_tracks": excluded,
    "albums_examined": len(groups),
    "unreadable": unreadable,
}))
'''


def scan(host, path):
    """Run the tag scan on the music host and return its findings."""
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, "python3", "-", path],
        input=REMOTE_SCANNER, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"remote scan failed: {proc.stderr.strip()[:400]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.exit(f"unexpected output from the remote scan:\n{proc.stdout[:400]}")
    if "error" in data:
        sys.exit(data["error"])
    return data


def main():
    ap = argparse.ArgumentParser(
        description="Find albums missing tracks, from track numbers in the tags")
    ap.add_argument("--host", default=localconfig.HOST)
    ap.add_argument("--path", default=localconfig.MUSIC_PATH)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--min-missing", type=int, default=1)
    args = ap.parse_args()

    data = scan(args.host, args.path)
    gaps = [g for g in data["gaps"] if len(g["missing"]) >= args.min_missing]

    if args.json:
        json.dump(gaps, sys.stdout, indent=1)
        print()
        return 0

    print(f"scanned {data['scanned']} tagged files on {args.host}", file=sys.stderr)
    print(f"  {data['excluded_compilation_tracks']} excluded as compilation tracks",
          file=sys.stderr)
    print(f"  {data['albums_examined']} single-artist album-discs examined\n",
          file=sys.stderr)

    gaps.sort(key=lambda g: -len(g["missing"]))
    print(f"{len(gaps)} albums missing tracks\n")
    print(f"{'have':>5}{'of':>5}  {'missing':<26} album")
    for g in gaps:
        disc = f" [disc {g['disc']}]" if g["disc"] != 1 else ""
        miss = (str(g["missing"]) if len(g["missing"]) <= 8
                else f"{g['missing'][:8]}+{len(g['missing']) - 8}")
        flag = "" if g["exact"] else "  ~"
        print(f"{g['have']:>5}{g['expected']:>5}  {miss:<26} "
              f"{g['artist']} — {g['album']}{disc}{flag}")
    print("\n~ = no track total in the tags, so the ceiling is the highest track "
          "held; the real album may be longer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
