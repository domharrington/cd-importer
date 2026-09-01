#!/usr/bin/env python3
"""
Give every artist folder on the Pi an artist.jpg for Navidrome to display.

Navidrome's default ArtistArtPriority is "artist.*, album/artist.*, external", so
an artist.jpg in the artist folder is the first thing it looks for. The library is
bind-mounted into the container read-only, so Navidrome cannot write these itself —
we place them over SSH.

Images come from Deezer's search API, which needs no key and returns 1000x1000
artwork. The artist name must match after normalisation before an image is
accepted: a loose search for "Texas" happily returns "Little Texas", and a wrong
image is worse than none because it looks deliberate.

Usage:
    ./fetch_artist_art.py                    # dry run: report what it would fetch
    ./fetch_artist_art.py --apply            # actually place the images
    ./fetch_artist_art.py --apply --only Adele --only Coldplay
"""

import argparse
import collections
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

DEEZER_SEARCH = "https://api.deezer.com/search/artist"
USER_AGENT = "cd-importer/1.0 ( hello@domharrington.email )"
IMAGE_NAME = "artist.jpg"
MIN_BYTES = 5_000          # anything smaller is an error page
# Deezer builds artist image URLs from an MD5 of the image. When an artist has no
# picture it uses the MD5 of the empty string and serves a grey silhouette at
# whatever size you ask for — so a placeholder is a valid 1000x1000 JPEG, and
# checking dimensions or size cannot tell it apart from a real photo.
NO_PICTURE_MD5 = "d41d8cd98f00b204e9800998ecf8427e"
# ...but that is only one of three forms. Some artists get an empty path segment
# (".../artist//1000x1000...") and others get a perfectly plausible MD5 that
# still serves the same generic image. Since the URL cannot be trusted, the
# placeholder is identified by the bytes: fetch it once from a known-empty URL
# and reject any download that matches.
PLACEHOLDER_URL = "https://cdn-images.dzcdn.net/images/artist//1000x1000-000000-80-0-0.jpg"
_placeholder_hashes = set()


def learn_placeholder_hashes():
    """Hash Deezer's generic artist image so real photos can be told from it."""
    for url in (PLACEHOLDER_URL,
                PLACEHOLDER_URL.replace("artist//", f"artist/{NO_PICTURE_MD5}/")):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                _placeholder_hashes.add(hashlib.sha256(resp.read()).hexdigest())
        except (OSError, http.client.HTTPException):
            continue
    return _placeholder_hashes
REQUEST_INTERVAL = 0.6     # Deezer allows ~50 requests / 5s; stay well under

# Folders that are not artists, so have no artist image to find.
SKIP_FOLDERS = {
    "compilations", "various artists", "various", "va",
    "soundtracks", "soundtrack", "unknown artist", "unknown",
}


def normalise(name):
    """Casefold, strip accents and drop punctuation, for comparing artist names."""
    decomposed = unicodedata.normalize("NFKD", name or "")
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    plain = plain.lower()
    plain = re.sub(r"^(the|a)\s+", "", plain)        # "The Killers" == "Killers"
    plain = plain.replace("&", " and ")              # Deezer answered "Mark Ronson
                                                     # & The Business Intl" to a
                                                     # search for "... And ..."
    return re.sub(r"[^a-z0-9]+", "", plain)


def primary_artist(name):
    """The lead artist, with featured credits stripped.

    Many folders are named after a track credit rather than an artist —
    "Lethal Bizzle (Ft. Kate Nash)", "Wideboys Feat. Clare Evers" — which no
    image source will ever match. The lead artist is who the image should show,
    and searching for that still requires an exact match on the trimmed name, so
    this widens what we can find without loosening what we accept.
    """
    trimmed = re.split(
        r"\s*(?:\(?\b(?:feat|ft|featuring|with|vs|versus|presents|pres)\b\.?)",
        name, maxsplit=1, flags=re.IGNORECASE)[0]
    trimmed = trimmed.split("(")[0]
    trimmed = trimmed.strip(" -&,")
    return trimmed if trimmed and normalise(trimmed) != normalise(name) else ""


class Remote:
    """The Pi, over SSH. Kept in one place so a dry run cannot write by accident."""

    def __init__(self, host, path, apply_changes):
        self.host = host
        self.path = path.rstrip("/")
        self.apply_changes = apply_changes

    def _ssh(self, command, stdin=None):
        return subprocess.run(
            ["ssh", "-o", "BatchMode=yes", self.host, command],
            input=stdin, capture_output=True,
        )

    def artist_folders(self):
        proc = self._ssh(f"find {self.path!r} -mindepth 1 -maxdepth 1 -type d -print0")
        if proc.returncode != 0:
            sys.exit(f"ssh failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
        out = []
        for raw in proc.stdout.split(b"\0"):
            if raw:
                out.append(os.path.basename(raw.decode("utf-8", "surrogateescape")))
        return sorted(out)

    def existing_images(self):
        proc = self._ssh(
            f"find {self.path!r} -mindepth 2 -maxdepth 2 -type f "
            r"\( -iname 'artist.jpg' -o -iname 'artist.png' -o -iname 'artist.jpeg' \) -print0"
        )
        have = set()
        for raw in proc.stdout.split(b"\0"):
            if raw:
                p = raw.decode("utf-8", "surrogateescape")
                have.add(os.path.basename(os.path.dirname(p)))
        return have

    def write_image(self, artist, data):
        """Stream the image to the Pi. No-op unless --apply was given."""
        if not self.apply_changes:
            return True
        target = f"{self.path}/{artist}/{IMAGE_NAME}"
        # cat > file via stdin avoids quoting a second path through scp.
        proc = self._ssh(f"cat > {target!r}", stdin=data)
        return proc.returncode == 0


def deezer_lookup(name):
    """Best Deezer match for an artist name, or None if nothing usable is found.

    Two subtleties, both learned the hard way after this wrote 49 grey
    silhouettes:

      - Deezer's search returns several records for one name: stub entries with
        no albums and no picture, plus the real artist. Taking the first exact
        match gave a 486-fan, 0-album "Radiohead" stub rather than the real one
        with four million fans. So rank the exact matches by popularity.

      - An artist with no picture still gets a picture URL, built from the MD5 of
        the empty string. Those must be rejected explicitly; no amount of
        checking the bytes will reveal them, since the placeholder is a
        perfectly valid JPEG of the requested size.
    """
    url = f"{DEEZER_SEARCH}?{urllib.parse.urlencode({'q': name, 'limit': 10})}"
    data = None
    for attempt in range(3):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.load(resp)
            break
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as err:
            # Deezer drops connections when it has had enough of us; retrying is
            # the right response, and OSError covers URLError/timeouts too.
            if attempt == 2:
                return None, f"lookup failed ({err})"
            time.sleep(3 * (attempt + 1))

    wanted = normalise(name)
    exact = [i for i in data.get("data", []) if normalise(i.get("name")) == wanted]
    if not exact:
        got = ", ".join(i.get("name", "?") for i in data.get("data", [])[:3])
        return None, f"no exact name match{f' (saw: {got})' if got else ''}"

    withart = [i for i in exact
               if NO_PICTURE_MD5 not in (i.get("picture_xl") or i.get("picture_big") or "")]
    if not withart:
        return None, f"matched {len(exact)} record(s) but Deezer has no picture"

    best = max(withart, key=lambda i: (i.get("nb_fan") or 0, i.get("nb_album") or 0))
    picture = best.get("picture_xl") or best.get("picture_big")
    return {"name": best.get("name"), "url": picture,
            "fans": best.get("nb_fan") or 0}, None


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as err:
        return None, f"download failed ({err})"
    if len(data) < MIN_BYTES:
        return None, f"image too small ({len(data)} bytes)"
    if not data.startswith(b"\xff\xd8"):
        return None, "not a JPEG"
    if hashlib.sha256(data).hexdigest() in _placeholder_hashes:
        # A valid JPEG of the right dimensions, and still just a grey silhouette.
        return None, "Deezer has no picture (generic placeholder)"
    return data, None


def main():
    ap = argparse.ArgumentParser(description="Fetch artist.jpg for each artist folder")
    ap.add_argument("--host", default="raspberrypi.local")
    ap.add_argument("--path", default="/srv/shares/media/Music")
    ap.add_argument("--apply", action="store_true",
                    help="actually write images (default is a dry run)")
    ap.add_argument("--only", action="append", default=[],
                    help="limit to these artist folders (repeatable)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N artists")
    args = ap.parse_args()

    remote = Remote(args.host, args.path, args.apply)
    if not args.apply:
        print("DRY RUN — no files will be written. Re-run with --apply.\n", file=sys.stderr)

    known = learn_placeholder_hashes()
    print(f"learned {len(known)} placeholder image hash(es) to reject",
          file=sys.stderr)

    folders = remote.artist_folders()
    have = remote.existing_images()
    todo = [f for f in folders
            if f not in have and normalise(f) not in {normalise(s) for s in SKIP_FOLDERS}]
    if args.only:
        wanted = {normalise(o) for o in args.only}
        todo = [f for f in todo if normalise(f) in wanted]
    if args.limit:
        todo = todo[:args.limit]

    print(f"{len(folders)} artist folders, {len(have)} already have an image, "
          f"{len(todo)} to fetch", file=sys.stderr)

    stats = collections.Counter()
    misses = []
    for i, artist in enumerate(todo, 1):
        if i > 1:
            time.sleep(REQUEST_INTERVAL)
        match, err = deezer_lookup(artist)
        if not match:
            # Retry on the lead artist alone, if the folder name is a credit.
            lead = primary_artist(artist)
            if lead:
                time.sleep(REQUEST_INTERVAL)
                match, lead_err = deezer_lookup(lead)
                if match:
                    err = None
                    print(f"  [{i}/{len(todo)}] ~ {artist} -> matched lead artist "
                          f"{lead!r}", file=sys.stderr)
                else:
                    err = f"{err}; lead {lead!r}: {lead_err}"
        if not match:
            stats["no match"] += 1
            misses.append((artist, err))
            print(f"  [{i}/{len(todo)}] ✗ {artist} — {err}", file=sys.stderr)
            continue
        data, err = download(match["url"])
        if not data:
            stats["download failed"] += 1
            misses.append((artist, err))
            print(f"  [{i}/{len(todo)}] ✗ {artist} — {err}", file=sys.stderr)
            continue
        if remote.write_image(artist, data):
            stats["fetched"] += 1
            verb = "wrote" if args.apply else "would write"
            print(f"  [{i}/{len(todo)}] ✓ {artist} — {verb} {IMAGE_NAME} "
                  f"({len(data) // 1024} KB)", file=sys.stderr)
        else:
            stats["write failed"] += 1
            misses.append((artist, "ssh write failed"))
            print(f"  [{i}/{len(todo)}] ✗ {artist} — could not write to the Pi",
                  file=sys.stderr)

    print("\nsummary:", file=sys.stderr)
    for k, v in stats.most_common():
        print(f"  {k}: {v}", file=sys.stderr)
    if misses:
        print(f"\n{len(misses)} artist(s) need manual attention:", file=sys.stderr)
        for artist, why in misses:
            print(f"  {artist} — {why}", file=sys.stderr)
    if not args.apply and stats["fetched"]:
        print("\nNothing was written. Re-run with --apply.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
