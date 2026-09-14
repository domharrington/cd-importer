#!/usr/bin/env python3
"""
Rank albums to buy, from Spotify's extended streaming history.

Reads the export Spotify emails you (request it at spotify.com/account/privacy —
it takes up to 30 days), compares it against the library on the music host, and
writes buy-list.md. Nothing is modified anywhere; the library is read over SSH,
for the same reason audit_library.py does it that way — the SMB mount cannot
traverse some accented paths.

    ./spotify_buylist.py --history ~/Downloads/'Spotify Extended Streaming History'
    ./spotify_buylist.py --half-life 2 --top 60

Set SPOTIFY_HISTORY in .env.local to skip the --history flag.

── Why the export rather than the Web API ──────────────────────────────────────

The API caps "top tracks" at 50 per time range, which is a ranking of a sample.
The export is every play since the account was created, with ms_played on each —
so an album's score can be actual hours listened rather than a proxy for it.

── Scoring ─────────────────────────────────────────────────────────────────────

An album scores the hours you have played it, with older plays discounted by an
exponential half-life (default 3 years). Something you wore out in 2011 and never
returned to should not outrank something you play every week now, but it should
not vanish either — a half-life decays rather than truncates, which is why this
is not simply "the last two years of data".

Both the weighted score and the raw hours are printed, so a ranking that looks
wrong can be checked against the unweighted number.

── Data traps this export contains ─────────────────────────────────────────────

Three things that silently produce wrong answers, all handled here:

  * The `_1.json` files are NOT duplicates of their base year — they are
    additional records. Deduping is still needed, but on
    (ts, track_uri, ms_played); dropping the `_1` files loses real plays.
  * `skipped` is False on every record from 2017-2022, which reads as "never
    skipped anything for six years". It is not used. `reason_end == "fwdbtn"`
    is the real signal.
  * `ts` is when the track STOPPED, in UTC — not when it started, and not local
    time. Only the date is used here, so it matters little, but any time-of-day
    analysis has to convert, and conn_country is not constant.
"""

import argparse
import collections
import datetime
import glob
import json
import math
import os
import re
import sys
import unicodedata

import library_gaps
import localconfig
from audit_library import Library, list_remote_files

HERE = os.path.dirname(os.path.abspath(__file__))
# A play shorter than this is a skip, not a listen, and should not count toward
# an album's score. 30s is the threshold Spotify itself uses to count a stream.
MIN_PLAY_MS = 30_000


def norm(text):
    """Match names across punctuation, case, accents and edition suffixes.

    Deliberately aggressive: "OK Computer", "OK Computer (Remastered)" and
    "OK Computer [Deluxe Edition]" are one album for our purposes. A false merge
    only costs an album a place in the ranking, whereas a false split puts
    something on the buy list that is already on the shelf.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[\(\[].*?[\)\]]", " ", text)
    text = re.sub(
        r"\b(remaster(ed)?|deluxe|expanded|special|anniversary|edition|version|"
        r"bonus track[s]?|explicit|mono|stereo)\b", " ", text)
    text = re.sub(r"\b(19|20)\d{2}\b", " ", text)
    text = re.sub(r"^(the|a|an)\s+", "", text)
    return re.sub(r"[^a-z0-9]+", "", text)


# ── Reading the export ───────────────────────────────────────────────────────

def load_history(path):
    """Every music play in the export, deduplicated.

    Returns (plays, stats). Exits with something actionable if the directory is
    not what it should be.
    """
    if not os.path.isdir(path):
        sys.exit(f"not a directory: {path}")
    files = sorted(glob.glob(os.path.join(path, "Streaming_History_Audio_*.json")))
    if not files:
        sys.exit(
            f"no Streaming_History_Audio_*.json in {path}\n"
            "  This wants the *extended* streaming history export, not the\n"
            "  account data one. Request it at spotify.com/account/privacy."
        )

    raw = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                raw.extend(json.load(fh))
        except (OSError, json.JSONDecodeError) as err:
            sys.exit(f"could not read {os.path.basename(f)}: {err}")

    seen, uniq = set(), []
    for r in raw:
        k = (r.get("ts"), r.get("spotify_track_uri"), r.get("ms_played"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)

    plays = [r for r in uniq if r.get("master_metadata_track_name")
             and r.get("master_metadata_album_album_name")]
    stats = {
        "files": len(files),
        "raw": len(raw),
        "unique": len(uniq),
        "music": len(plays),
        "first": min((r["ts"] for r in plays), default="")[:10],
        "last": max((r["ts"] for r in plays), default="")[:10],
    }
    return plays, stats


# ── Scoring ──────────────────────────────────────────────────────────────────

class Album:
    def __init__(self, artist, name):
        self.artist = artist
        self.name = name
        self.key = (norm(artist), norm(name))
        self.ms = 0             # raw listening time
        self.weighted = 0.0     # recency-weighted listening time
        self.recent_ms = 0      # last two calendar years
        self.plays = 0
        self.skips = 0
        self.tracks = set()     # distinct track TITLES seen (see below)
        self.years = set()
        self.owned_tracks = 0

    @property
    def hours(self): return self.ms / 3_600_000

    @property
    def recent_hours(self): return self.recent_ms / 3_600_000

    @property
    def score(self): return self.weighted / 3_600_000

    @property
    def skip_rate(self):
        return self.skips / self.plays if self.plays else 0.0


def build_albums(plays, half_life_years, now=None):
    """Aggregate plays into albums, discounting older listening."""
    now = now or datetime.date.today()
    recent_cutoff = str(now.year - 1)
    albums = {}
    for r in plays:
        ms = r.get("ms_played") or 0
        artist = r["master_metadata_album_artist_name"]
        name = r["master_metadata_album_album_name"]
        if not artist:
            continue
        key = (norm(artist), norm(name))
        a = albums.get(key)
        if a is None:
            a = albums[key] = Album(artist, name)

        year = r["ts"][:4]
        a.years.add(year)
        # reason_end is the trustworthy skip signal; `skipped` is unreliable.
        if r.get("reason_end") == "fwdbtn":
            a.skips += 1
        a.plays += 1
        if ms < MIN_PLAY_MS:
            continue

        a.ms += ms
        # Track *titles*, not URIs. The same song on a reissue, a remaster and a
        # deluxe edition carries three different URIs, which inflated the count
        # to 63 "tracks" for a 12-track album and flagged complete albums as
        # incomplete.
        a.tracks.add(norm(r["master_metadata_track_name"]))
        if year >= recent_cutoff:
            a.recent_ms += ms

        try:
            age = (now - datetime.date.fromisoformat(r["ts"][:10])).days / 365.25
        except ValueError:
            age = 0.0
        a.weighted += ms * math.pow(0.5, max(age, 0.0) / half_life_years)
    return albums


# ── Library comparison ───────────────────────────────────────────────────────

def owned_indexes(lib):
    """(artist, album) -> track count, plus an album-name-only fallback.

    The fallback catches compilations: a soundtrack filed under
    `Various Artists/Garden State` will never match the Spotify artist credit,
    and without this it would be recommended for purchase despite being owned.
    """
    exact = collections.Counter()
    by_name = collections.Counter()
    for (artist, album), files in lib.albums.items():
        exact[(norm(artist), norm(album))] += len(files)
        by_name[norm(album)] += len(files)
    return exact, by_name


def classify(albums, exact, by_name, tolerance):
    buy, complete, have = [], [], []
    for a in albums.values():
        a.owned_tracks = exact.get(a.key) or by_name.get(norm(a.name), 0)
        # Distinct tracks heard is a LOWER BOUND on the album's real length --
        # the export carries no track total -- so this only ever under-reports
        # incompleteness, never invents it.
        expected = len(a.tracks)
        if a.owned_tracks == 0:
            buy.append(a)
        elif expected and a.owned_tracks < expected - tolerance:
            complete.append(a)
        else:
            have.append(a)
    rank = lambda a: -a.score
    return sorted(buy, key=rank), sorted(complete, key=rank), sorted(have, key=rank)


# ── Report ───────────────────────────────────────────────────────────────────

def write_report(buy, complete, have, gaps, args, stats, host, path,
                 total_hours, covered):
    L = []
    w = L.append
    w("# Albums to buy\n")
    w(f"From {stats['music']:,} music plays ({stats['first']} to {stats['last']}), "
      f"checked against `{host}:{path}`.\n")
    w(f"Ranked by hours played, with older listening discounted on a "
      f"{args.half_life:g}-year half-life. Raw hours are shown alongside so the "
      f"weighting can be sanity-checked.\n")

    w("## Coverage\n")
    w("| | |")
    w("|---|---|")
    w(f"| total listening | {total_hours:,.0f} hours |")
    w(f"| covered by the library | {100*covered/total_hours:.1f}% |")
    w(f"| albums played but not owned | {len(buy):,} |")
    w(f"| albums with confirmed missing tracks | {len(gaps):,} |")
    w(f"| albums possibly incomplete | {len(complete):,} |")
    w("")

    w("## Buy — not in the library\n")
    shown = buy[:args.top]
    w(f"Top {len(shown)} of {len(buy):,}.\n")
    w("| # | score | hours | since | plays | skip | artist — album |")
    w("|---|---|---|---|---|---|---|")
    for i, a in enumerate(shown, 1):
        w(f"| {i} | {a.score:.1f} | {a.hours:.1f} | {a.recent_hours:.1f} | "
          f"{a.plays} | {100*a.skip_rate:.0f}% | {a.artist} — {a.name} |")
    w("")
    w("`score` is recency-weighted hours; `hours` is the raw total; `since` is "
      "the last two calendar years. A high skip rate on a high-scoring album "
      "suggests you play one track from it, not the album.\n")

    w("## Complete — owned, but missing tracks\n")
    w("Albums already in the library that are missing part of themselves. "
      "Buying the disc fills the gaps *and* upgrades what is there to lossless, "
      "so these are often better value than a new album.\n")

    if gaps:
        w("### Confirmed missing — gaps in the track numbering\n")
        w("Hard evidence from the tags on disk: an album holding tracks 1, 3 and "
          "7 is missing four, whatever it was played. Independent of Spotify, so "
          "an album pruned years ago and never streamed since still appears.\n")
        w(f"**{len(gaps)} albums.**\n")
        w("| hrs | have | of | missing | artist — album |")
        w("|---|---|---|---|---|")
        for g in gaps:
            miss = (", ".join(str(n) for n in g["missing"])
                    if len(g["missing"]) <= 10
                    else ", ".join(str(n) for n in g["missing"][:10])
                         + f" +{len(g['missing']) - 10}")
            disc = f" [disc {g['disc']}]" if g["disc"] != 1 else ""
            approx = "" if g["exact"] else "~"
            hrs = f"{g['hours']:.1f}" if g["hours"] else "—"
            # `have` is distinct track numbers, not files: an album with a
            # duplicated track has more files than tracks, and counting files
            # made incomplete albums look complete.
            dup = (f" +{g['files'] - g['have']} dup"
                   if g.get("files", g["have"]) > g["have"] else "")
            w(f"| {hrs} | {g['have']}{dup} | {approx}{g['expected']} | {miss} | "
              f"{g['artist']} — {g['album']}{disc} |")
        w("")
        w("`hrs` is lifetime Spotify listening where the album could be matched; "
          "`—` means you own it but have not streamed it, which is not a reason "
          "to skip it. A `~` on the total means the tags carry no track count, "
          "so the real album may be longer still.\n")

    if complete:
        w("### Possibly incomplete — fewer tracks than you have played\n")
        w("Weaker evidence, from the streaming history: you have played more "
          "distinct track titles from this album than the library holds. Titles "
          "rather than Spotify URIs, since one song across a reissue, a remaster "
          "and a deluxe edition has three URIs and would otherwise treble the "
          "apparent album length. A lower bound — it cannot see tracks you never "
          "played.\n")
        w(f"**{len(complete)}.**\n")
        w("| score | artist — album | have | heard |")
        w("|---|---|---|---|")
        for a in complete[:args.top]:
            w(f"| {a.score:.1f} | {a.artist} — {a.name} | {a.owned_tracks} | "
              f"{len(a.tracks)} |")
        w("")

    if not gaps and not complete:
        w("None.\n")

    w("## Already owned\n")
    w(f"**{len(have):,}** albums matched the library. Listed so a bad match is "
      "visible — a wrong entry here means something is missing from the buy "
      "list above.\n")
    w("<details><summary>top 60 by score</summary>\n")
    for a in have[:60]:
        w(f"- {a.score:.1f} — {a.artist} — {a.name} ({a.owned_tracks} tracks)")
    w("\n</details>\n")

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser(
        description="Rank albums to buy from Spotify's extended streaming history")
    ap.add_argument("--history", default=os.environ.get("SPOTIFY_HISTORY", ""),
                    help="the unpacked export directory (or set SPOTIFY_HISTORY)")
    ap.add_argument("--host", default=localconfig.HOST)
    ap.add_argument("--path", default=localconfig.MUSIC_PATH)
    ap.add_argument("--out", default="buy-list.md")
    ap.add_argument("--top", type=int, default=100, help="rows per section")
    ap.add_argument("--half-life", type=float, default=3.0,
                    help="years after which a play counts half (default 3)")
    ap.add_argument("--no-gaps", action="store_true",
                    help="skip the on-disk track-number scan (it reads every "
                         "file's tags on the music host, which takes a minute)")
    ap.add_argument("--tolerance", type=int, default=1,
                    help="tracks an owned album may lack before it counts as "
                         "incomplete (default 1, since editions differ)")
    args = ap.parse_args()

    if not args.history:
        sys.exit("pass --history <export dir>, or set SPOTIFY_HISTORY in .env.local")
    if args.half_life <= 0:
        sys.exit("--half-life must be greater than 0")

    plays, stats = load_history(args.history)
    print(f"{stats['files']} files, {stats['raw']:,} records -> "
          f"{stats['unique']:,} unique, {stats['music']:,} music plays "
          f"({stats['first']} to {stats['last']})", file=sys.stderr)

    albums = build_albums(plays, args.half_life)
    print(f"{len(albums):,} distinct albums", file=sys.stderr)

    print(f"reading {args.host}:{args.path} over ssh...", file=sys.stderr)
    lib = Library(list_remote_files(args.host, args.path))
    print(f"  {len(lib.albums)} album folders, {lib.track_count()} tracks",
          file=sys.stderr)

    exact, by_name = owned_indexes(lib)
    buy, complete, have = classify(albums, exact, by_name, args.tolerance)

    # Albums missing tracks according to their own track numbering. A separate
    # question from anything the streaming history can answer: an album pruned
    # years ago and never played since leaves no trace in the history at all.
    gaps = []
    if not args.no_gaps:
        print("scanning track numbers on the music host...", file=sys.stderr)
        data = library_gaps.scan(args.host, args.path)
        for g in data["gaps"]:
            match = albums.get((norm(g["artist"]), norm(g["album"])))
            g["hours"] = match.hours if match else 0.0
            g["score"] = match.score if match else 0.0
            gaps.append(g)
        # Ones you actually listen to first, then by how much is missing.
        gaps.sort(key=lambda g: (-g["score"], -len(g["missing"])))
        print(f"  {data['albums_examined']} albums examined, "
              f"{len(gaps)} missing tracks "
              f"({data['excluded_compilation_tracks']} compilation tracks "
              f"excluded)", file=sys.stderr)

    total_ms = sum(a.ms for a in albums.values())
    covered_ms = sum(a.ms for a in albums.values() if a.owned_tracks)
    total_hours = total_ms / 3_600_000

    write_report(buy, complete, have, gaps, args, stats, args.host, args.path,
                 total_hours, covered_ms / 3_600_000)

    print(f"\nwrote {args.out}", file=sys.stderr)
    print(f"  library covers {100*covered_ms/total_ms:.1f}% of "
          f"{total_hours:,.0f} listening hours", file=sys.stderr)
    print(f"  {len(buy):,} to buy, {len(gaps):,} with confirmed missing tracks, "
          f"{len(complete):,} possibly incomplete, {len(have):,} owned",
          file=sys.stderr)
    if buy:
        top = buy[0]
        print(f"  top pick: {top.artist} — {top.name} "
              f"({top.hours:.0f}h all time, {top.recent_hours:.0f}h recent)",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
