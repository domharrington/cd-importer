#!/usr/bin/env python3
"""
MusicBrainz lookup helper for cd_automic.sh.

Reads a macOS Audio CD .TOC.plist, computes the MusicBrainz disc ID, and
resolves it to a release.

Usage:
    mb_lookup.py --toc <toc.plist> [--disc-name <volume_name>] [--track-count N]
    mb_lookup.py --toc <toc.plist> --print-discid
    mb_lookup.py --discid <discid>

Output (JSON to stdout):
{
    "artist": "Radiohead",
    "album_artist": "Radiohead",
    "title": "OK Computer",
    "date": "1997-06-17",
    "year": "1997",
    "mbid": "0b6b4ba0-d36f-47bd-b4ea-6a5b91842d29",
    "discid": "rdosqrxASN.ZqF.d.5pPhHtgVyA-",
    "disc_number": 1,
    "disc_total": 1,
    "source": "discid",
    "tracks": [{"position": 1, "title": "Airbag", "artist": "Radiohead"}, ...]
}

On failure: prints a diagnostic to stderr and exits 1.
"""

import argparse
import base64
import hashlib
import json
import plistlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

WS = "https://musicbrainz.org/ws/2"
CONTACT = "hello@domharrington.email"
USER_AGENT = f"cd-importer/1.0 ( {CONTACT} )"

# libdiscid convention: on an enhanced CD the audio lead-out is taken to be
# the data track's start block minus this many sectors.
DATA_TRACK_GAP = 11400


# ── Disc ID ──────────────────────────────────────────────────────────────────

def read_toc(toc_path):
    """Parse a macOS .TOC.plist into (first_track, last_track, leadout, offsets).

    macOS reports absolute LBAs that ALREADY include the 150-sector pregap
    (track 1 starts at 150), which is exactly what MusicBrainz hashes. Do not
    add a pregap here.
    """
    with open(toc_path, "rb") as f:
        toc = plistlib.load(f)

    sessions = toc.get("Sessions") or []
    if not sessions:
        raise ValueError("no sessions in TOC")

    session = sessions[0]
    all_tracks = sorted(session["Track Array"], key=lambda t: t["Point"])
    audio = [t for t in all_tracks if not t.get("Data")]
    if not audio:
        raise ValueError("no audio tracks in TOC")

    leadout = int(session["Leadout Block"])

    # Enhanced CD: a trailing data track is excluded, and the audio lead-out is
    # derived from where that data track starts.
    data = [t for t in all_tracks if t.get("Data")]
    if data:
        first_data = min(int(t["Start Block"]) for t in data)
        if first_data > int(audio[-1]["Start Block"]):
            leadout = first_data - DATA_TRACK_GAP

    offsets = [int(t["Start Block"]) for t in audio]
    return int(audio[0]["Point"]), int(audio[-1]["Point"]), leadout, offsets


def compute_discid(first_track, last_track, leadout, offsets):
    """MusicBrainz disc ID: SHA-1 over fixed-width hex fields, then a base64
    variant where + / = become . _ -

    The hashed string is:
      %02X first track, %02X last track,
      then exactly 100 x %08X: [lead-out, track 1..99] (0 for absent tracks).
    """
    frames = [0] * 100
    frames[0] = leadout
    for i, off in enumerate(offsets, start=first_track):
        frames[i] = off

    h = hashlib.sha1()
    h.update(b"%02X" % first_track)
    h.update(b"%02X" % last_track)
    for frame in frames:
        h.update(b"%08X" % frame)

    # Keep the padding, translated to '-'. A MusicBrainz disc ID is always
    # 28 characters; stripping '=' produces an ID the server will reject.
    return (
        base64.b64encode(h.digest())
        .decode("ascii")
        .replace("+", ".")
        .replace("/", "_")
        .replace("=", "-")
    )


def toc_param(first_track, last_track, leadout, offsets):
    """The '?toc=' query value MusicBrainz uses for fuzzy TOC matching."""
    return "+".join(str(n) for n in [first_track, last_track, leadout, *offsets])


# ── MusicBrainz web service ──────────────────────────────────────────────────

_last_request = [0.0]


def ws_get(path, params):
    """GET a MusicBrainz JSON endpoint, respecting the 1 req/sec rate limit."""
    wait = 1.1 - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    _last_request[0] = time.monotonic()

    params = dict(params, fmt="json")
    url = f"{WS}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def credit_to_name(artist_credit):
    """Flatten a MusicBrainz artist-credit list, honouring join phrases."""
    parts = []
    for credit in artist_credit or []:
        if isinstance(credit, str):
            parts.append(credit)
            continue
        parts.append(credit.get("name") or credit.get("artist", {}).get("name", ""))
        parts.append(credit.get("joinphrase", ""))
    return "".join(parts).strip()


def score_release(release, track_count):
    """Rank candidate releases: official first, then earliest date."""
    official = (release.get("status") or "") == "Official"
    date = release.get("date") or "9999"
    matches = any(
        m.get("track-count") == track_count for m in release.get("media", [])
    )
    return (not matches, not official, date)


def medium_for_disc(release, discid, track_count):
    """Pick the medium this disc corresponds to (matters for box sets)."""
    media = release.get("media", [])
    if discid:
        for medium in media:
            if any(d.get("id") == discid for d in medium.get("discs", [])):
                return medium
    for medium in media:
        if medium.get("track-count") == track_count:
            return medium
    return media[0] if media else None


def build_result(release, discid, track_count, source):
    medium = medium_for_disc(release, discid, track_count)
    if medium is None:
        return None

    album_artist = credit_to_name(release.get("artist-credit"))
    tracks = []
    for track in medium.get("tracks", []):
        recording = track.get("recording", {})
        title = track.get("title") or recording.get("title") or ""
        artist = (
            credit_to_name(track.get("artist-credit"))
            or credit_to_name(recording.get("artist-credit"))
            or album_artist
        )
        if not title:
            continue
        tracks.append(
            {
                "position": int(track.get("position") or len(tracks) + 1),
                "title": title,
                "artist": artist,
            }
        )
    tracks.sort(key=lambda t: t["position"])
    if not tracks:
        return None

    date = release.get("date") or ""
    return {
        "artist": album_artist or "Unknown Artist",
        "album_artist": album_artist or "Unknown Artist",
        "title": release.get("title") or "",
        "date": date,
        "year": date[:4],
        "mbid": release.get("id") or "",
        "discid": discid or "",
        "disc_number": int(medium.get("position") or 1),
        "disc_total": len(release.get("media", [])) or 1,
        "source": source,
        "tracks": tracks,
    }


def pick(releases, discid, track_count, source):
    """Choose the best release from a candidate list and shape the result."""
    for release in sorted(releases, key=lambda r: score_release(r, track_count)):
        result = build_result(release, discid, track_count, source)
        if result:
            return result
    return None


def lookup_by_discid(discid, track_count):
    try:
        data = ws_get(f"discid/{discid}", {"inc": "artist-credits+recordings"})
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise
    return pick(data.get("releases", []), discid, track_count, "discid")


def lookup_by_toc(toc, track_count):
    """Fuzzy TOC match: finds releases whose disc layout is close enough even
    when this exact pressing has no disc ID attached yet."""
    try:
        data = ws_get(
            "discid/-", {"toc": toc, "inc": "artist-credits+recordings", "cdstubs": "no"}
        )
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise
    return pick(data.get("releases", []), None, track_count, "toc")


def lookup_by_title(album_name, track_count):
    query = f'release:"{album_name}"'
    if track_count:
        query += f" AND tracks:{track_count}"
    try:
        data = ws_get("release", {"query": query, "limit": "10"})
    except urllib.error.HTTPError:
        return None

    for candidate in data.get("releases", []):
        try:
            full = ws_get(
                f"release/{candidate['id']}", {"inc": "artist-credits+recordings"}
            )
        except urllib.error.HTTPError:
            continue
        result = build_result(full, None, track_count, "title")
        if result and (not track_count or len(result["tracks"]) == track_count):
            return result
    return None


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MusicBrainz lookup helper")
    parser.add_argument("--toc", help="Path to .TOC.plist")
    parser.add_argument("--disc-name", help="Volume name, for the title-search fallback")
    parser.add_argument("--discid", help="Pre-computed disc ID")
    parser.add_argument("--track-count", type=int, default=0)
    parser.add_argument(
        "--print-discid",
        action="store_true",
        help="Print the computed disc ID and exit (no network access)",
    )
    args = parser.parse_args()

    discid = args.discid
    toc = None

    if args.toc:
        try:
            first, last, leadout, offsets = read_toc(args.toc)
            toc = toc_param(first, last, leadout, offsets)
            if not discid:
                discid = compute_discid(first, last, leadout, offsets)
            if not args.track_count:
                args.track_count = len(offsets)
        except (OSError, ValueError, KeyError, plistlib.InvalidFileException) as err:
            print(f"Could not read TOC: {err}", file=sys.stderr)

    if args.print_discid:
        if not discid:
            print("No disc ID could be computed", file=sys.stderr)
            return 1
        print(discid)
        if toc:
            print(f"toc={toc}", file=sys.stderr)
        return 0

    attempts = []
    if discid:
        attempts.append(("disc ID " + discid, lambda: lookup_by_discid(discid, args.track_count)))
    if toc:
        attempts.append(("fuzzy TOC match", lambda: lookup_by_toc(toc, args.track_count)))
    if args.disc_name:
        attempts.append(
            (f"title search '{args.disc_name}'",
             lambda: lookup_by_title(args.disc_name, args.track_count))
        )

    for label, attempt in attempts:
        print(f"trying {label}...", file=sys.stderr)
        try:
            result = attempt()
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            print(f"  network error: {err}", file=sys.stderr)
            continue
        if result:
            print(
                f"  matched via {result['source']}: "
                f"{result['artist']} - {result['title']} ({result['year']})",
                file=sys.stderr,
            )
            json.dump(result, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 0
        print("  no match", file=sys.stderr)

    print("MusicBrainz lookup failed: no matching release found", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
