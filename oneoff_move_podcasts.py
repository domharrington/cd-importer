#!/usr/bin/env python3
"""
One-off: move spoken-word content out of the music library.

Podcasts and audiobooks were sitting among the music, each episode becoming its
own one-track album — a chunk of the library's single-track albums and half its
remaining duplicate album rows.

They go to _superseded/podcasts/ rather than being deleted: episodes are
re-downloadable, but the Daniel Defoe audiobook is less so, and the Pi is the
only copy of this music.

DJ mixes are deliberately left alone. They are also single long files that look
like one-track albums, but they are music and worth keeping browsable.

The artists to move are listed explicitly rather than detected. A heuristic on
track length and genre found these, but it also flagged Flight Of The Conchords
(a comedy *album*, tagged "books & spoken") and a 34-minute Incubus track, so
the final call is a human one and belongs in the code where it can be read.

Name the folders on the command line, or list them one per line in a
spoken-artists.txt beside the script (which git ignores — a subscription list is
nobody else's business):

    ssh my-pi 'python3 oneoff_move_podcasts.py --artist "Some Podcast Co"'
    ssh my-pi 'python3 oneoff_move_podcasts.py --apply'
    ssh my-pi 'python3 oneoff_move_podcasts.py --restore'
"""

import argparse
import os
import shutil
import sys

# Defaults only. These scripts get copied to the music host and run there, so
# they stay single-file — set MUSIC_PATH/SUPERSEDED_PATH in the environment, or
# pass the paths as arguments.
MUSIC_PATH = os.environ.get("MUSIC_PATH", "/srv/music")
SUPERSEDED_PATH = os.environ.get("SUPERSEDED_PATH", "/srv/_superseded")

MUSIC = MUSIC_PATH
DEST = os.path.join(SUPERSEDED_PATH, "podcasts")
ARTIST_LIST_FILE = "spoken-artists.txt"


def spoken_artists(cli_names):
    """Artist folders that are spoken word, not music.

    From --artist flags, else ARTIST_LIST_FILE beside the script. Kept out of
    the source because it is a list of what someone listens to, not code.
    """
    if cli_names:
        return list(cli_names)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ARTIST_LIST_FILE)
    if not os.path.isfile(path):
        sys.exit(
            f"no artists given: pass --artist, or list them one per line in {ARTIST_LIST_FILE}"
        )
    with open(path, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]

AUDIO = (".mp3", ".m4a", ".mp4", ".flac")


def audio_stats(directory):
    tracks = size = 0
    for root, _, names in os.walk(directory):
        for n in names:
            if n.startswith("._") or not n.lower().endswith(AUDIO):
                continue
            tracks += 1
            size += os.path.getsize(os.path.join(root, n))
    return tracks, size


def find_folders(music, wanted):
    """Match the configured names against what is actually on disk."""
    lookup = {n.lower(): n for n in wanted}
    found = {}
    for name in sorted(os.listdir(music)):
        if not os.path.isdir(os.path.join(music, name)):
            continue
        if name.lower() in lookup:
            found[name] = os.path.join(music, name)
    missing = set(lookup.values()) - {lookup[n.lower()] for n in found}
    return found, sorted(missing)


def move(src, dest_root, apply_changes):
    dest = os.path.join(dest_root, os.path.basename(src))
    if os.path.exists(dest):
        print(f"     ! already at {dest} — skipping")
        return False
    if apply_changes:
        os.makedirs(dest_root, exist_ok=True)
        shutil.move(src, dest)
    return True


def do_move(music, dest, artists, apply_changes):
    found, missing = find_folders(music, artists)
    if missing:
        print(f"not found on disk (already moved?): {', '.join(missing)}\n")
    if not found:
        print("nothing to move")
        return 1

    total_tracks = total_size = moved = 0
    for name, path in found.items():
        tracks, size = audio_stats(path)
        albums = [a for a in sorted(os.listdir(path))
                  if os.path.isdir(os.path.join(path, a))]
        print(f"  {name}")
        print(f"     {tracks} track(s), {size // 1048576} MB, "
              f"{len(albums)} album folder(s): {', '.join(albums[:3])}")
        if move(path, dest, apply_changes):
            moved += 1
            total_tracks += tracks
            total_size += size

    verb = "moved" if apply_changes else "would move"
    print(f"\n{verb} {moved} artist folder(s), {total_tracks} track(s), "
          f"{total_size // 1048576} MB -> {dest}")
    if not apply_changes:
        print("Nothing changed. Re-run with --apply.")
    else:
        print("Rescan Navidrome. Undo with --restore.")
    return 0


def do_restore(music, dest):
    """Put everything back, for when a podcast turns out to be wanted."""
    if not os.path.isdir(dest):
        sys.exit(f"nothing to restore: {dest} does not exist")
    restored = 0
    for name in sorted(os.listdir(dest)):
        src = os.path.join(dest, name)
        target = os.path.join(music, name)
        if os.path.exists(target):
            print(f"  ! {target} exists — skipping")
            continue
        shutil.move(src, target)
        print(f"  restored {name}")
        restored += 1
    try:
        os.rmdir(dest)
    except OSError:
        pass
    print(f"restored {restored} folder(s)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--music", default=MUSIC)
    ap.add_argument("--dest", default=DEST)
    ap.add_argument("--apply", action="store_true", help="actually move (default: dry run)")
    ap.add_argument("--restore", action="store_true", help="move everything back")
    ap.add_argument("--artist", action="append", default=[], metavar="NAME",
                    help=f"spoken-word artist folder to move; repeatable. "
                         f"Defaults to the names in {ARTIST_LIST_FILE}.")
    args = ap.parse_args()

    if not os.path.isdir(args.music):
        sys.exit(f"no library at {args.music} — run this on the Pi")
    if args.restore:
        return do_restore(args.music, args.dest)
    if not args.apply:
        print("DRY RUN — nothing will move. Re-run with --apply.\n")
    return do_move(args.music, args.dest, spoken_artists(args.artist), args.apply)


if __name__ == "__main__":
    sys.exit(main())
