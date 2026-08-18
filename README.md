# cd-importer

Watches for Audio CDs inserted into an external drive on macOS, rips each track
to FLAC, tags it from MusicBrainz, and files it into a Navidrome-friendly
`Artist/Album` tree — then ejects the disc so you can feed it the next one.

Built for bulk-importing a physical CD collection. Insert a disc, walk away,
come back when it pops out.

## How it works

macOS presents an inserted Audio CD as a read-only volume of `.aiff` files plus
a hidden `.TOC.plist` describing the disc layout:

```
/Volumes/OK Computer/
├── .TOC.plist
├── 1 Airbag.aiff
├── 2 Paranoid Android.aiff
└── ...
```

The script reads those files rather than talking to the drive directly, so
macOS handles the actual disc reads. `.TOC.plist` is the interesting part — it
carries the track offsets needed to compute a MusicBrainz **disc ID**, which
identifies the exact pressing and yields correct artist, album, and per-track
titles without any guessing from the volume name.

```
📀 Audio CD: OK Computer
   🔍 Looking up 'OK Computer' (12 tracks) on MusicBrainz...
      trying disc ID rdosqrxASN.ZqF.d.5pPhHtgVyA-...
        matched via discid: Radiohead - OK Computer (1997)
   ✅ Radiohead - OK Computer (1997)
   📂 .../navidrome_music/Radiohead/OK Computer (1997)
   💿 [1/12] Airbag (4:44)
      ✅ 50s @ 5.7x, 33.6 MB — 4:44/53:21 of album
   ...
   🖼  cover.jpg
   📊 Radiohead - OK Computer: 12/12 tracks
      53:21 of audio in 4m 06s @ 13.0x — 355.6 MB written
   🚗 Ejecting OK Computer
```

## Requirements

```sh
brew install flac
```

`flac` and `metaflac` both come from that one formula. `python3` and `curl` are
already on macOS. Nothing else is needed — no `libdiscid`, no
`musicbrainzngs`, no `ffmpeg`; `mb_lookup.py` uses only the standard library.

## Usage

```sh
./cd_automic.sh
```

Leave it running and insert discs one at a time. Output lands in
`./navidrome_music` by default:

```
navidrome_music/Radiohead/OK Computer (1997)/
├── 01 - Airbag.flac
├── 02 - Paranoid Android.flac
├── ...
└── cover.jpg
```

Multi-disc releases get a `Disc N` subfolder and matching `DISCNUMBER` tags, so
Navidrome stitches them back into a single album. Feed the discs in any order —
each disc ID identifies which medium of the release it is, so disc 2 is filed as
disc 2 even if you rip it first, and both discs land under one album folder even
when they mount under the same volume name.

### Configuration

All optional, set as environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `NAVIDROME_ROOT` | `./navidrome_music` | Where the library is written |
| `POLL_INTERVAL` | `5` | Seconds between `/Volumes` checks |
| `EJECT_WHEN_DONE` | `1` | Eject after a fully successful rip |
| `FETCH_COVER_ART` | `1` | Pull front cover from the Cover Art Archive |
| `SHOW_ENCODE_PROGRESS` | `1` | Tick a live elapsed-time line per track (interactive terminals only) |
| `RIP_UNIDENTIFIED` | `0` | Rip discs MusicBrainz doesn't know anyway, into `_unidentified/` |
| `UNIDENTIFIED_LOG` | `./unidentified-discs.log` | Where unidentified discs are recorded |
| `FORCE_ARTIST` | — | Supply the artist yourself; implies "rip this disc" |
| `FORCE_ALBUM` | — | Supply the album yourself; implies "rip this disc" |
| `FORCE_DATE` | — | Release date; otherwise taken from the album's release group |

```sh
NAVIDROME_ROOT=/Volumes/Music EJECT_WHEN_DONE=0 ./cd_automic.sh
```

### Backfilling cover art

Artwork is fetched on every pass, so re-inserting a disc whose tracks are all
present but whose `cover.jpg` is missing repairs it — the tracks are verified and
skipped, and only the art is fetched. To repair albums without the discs to hand,
`./cd_automic.sh --art` sweeps the library, reading the MusicBrainz IDs back out
of the existing tags.

### Inspecting a disc without ripping

```sh
# Just the disc ID — no network access
python3 mb_lookup.py --toc "/Volumes/OK Computer/.TOC.plist" --print-discid

# Full metadata as JSON
python3 mb_lookup.py --toc "/Volumes/OK Computer/.TOC.plist" --disc-name "OK Computer"
```

## Tags written

Vorbis comments, which is what Navidrome reads for grouping:

`TITLE` · `ARTIST` · `ALBUM` · `ALBUMARTIST` · `TRACKNUMBER` · `TRACKTOTAL` ·
`DISCNUMBER` · `DISCTOTAL` · `DATE` · `MUSICBRAINZ_ALBUMID` ·
`MUSICBRAINZ_RELEASEGROUPID`

Front cover art is written as `cover.jpg` in the album folder *and* embedded in
each FLAC as a `PICTURE` block, both of which Navidrome understands.

## Notes

**Identification is by disc ID only, and a miss is honest.** The disc ID is a
SHA-1 of the disc's own table of contents, so a match is exact. MusicBrainz
offers two fuzzier alternatives — a `?toc=` similarity search and a title search
on the volume name — and both were tested and returned confidently *wrong*
releases: a 2-track TOC matched an unrelated CD single, and 14-track TOCs matched
*The Platters – Golden Greats* and a Jacques Dutronc album. Neither is used.
A mistagged album hides in a library; a quarantined one doesn't.

Discs MusicBrainz cannot identify are **not** ripped. Such a rip carries no
usable metadata — `Unknown Artist`, tracks called `Track 1`…`Track N` — and the
fix produces a properly tagged copy anyway, so the untagged one is only cleanup
waiting to happen. Instead the disc is appended to `unidentified-discs.log` with
its disc ID and a link that submits its TOC to MusicBrainz, then ejected so a
bulk session keeps moving:

```
2026-08-18 10:35  Audio CD  (11 tracks)
  disc id: jlHtxFX.aqP87gEkUTuygZF0BJ8-
  submit:  https://musicbrainz.org/cdtoc/attach?toc=1+11+139760+150+...&tracks=11&id=jlHtxFX...
```

Adding the disc there fixes every future rip of that pressing, for you and
everyone else. Re-insert it afterwards and it files itself normally. Set
`RIP_UNIDENTIFIED=1` when the audio matters more than the tags — a borrowed disc,
or one that may not survive a second read; it then lands in
`_unidentified/<disc id>/` with a `DISC-INFO.txt`, alongside a `.ndignore` so
Navidrome skips the folder.

**Discs MusicBrainz will never have.** Unofficial pressings — an album padded
out with extra tracks, on a label that never released it — are real discs that no
amount of waiting will add to MusicBrainz. Supply the metadata yourself:

```sh
FORCE_ARTIST="David Gray" FORCE_ALBUM="White Ladder" ./cd_automic.sh
```

Because that metadata describes one specific disc, the script **exits after that
disc** instead of continuing to watch — otherwise the next CD you inserted would
be filed under the same artist and album.

Either variable implies "rip this disc", so `RIP_UNIDENTIFIED` is not needed, and
the result is filed in the library proper — the identity came from you, not from a
guess. Track titles still come from the filenames, so it is worth letting the Music
app name the disc first if it can.

The album name is then looked up in MusicBrainz on its own. The *pressing* may be
unknown there while the album is not, and that album's release group supplies both
a release date (so the folder gets its `(year)`) and a cover from the Cover Art
Archive. Set `FORCE_DATE` to override the date — the release group gives the
album's original date, which is not necessarily this pressing's. To keep the
sleeve art rather than the official cover, drop your own `cover.jpg` into the album
folder; an existing one is never overwritten.

**The Music app is not involved.** macOS labels the volume `Audio CD` and names
every file `N Audio Track.aiff` until the Music app's own (Gracenote) lookup
lands, at which point it renames the volume — observed moving from
`/Volumes/Audio CD` to `/Volumes/Eyes Open` on the same `/dev/disk4` twenty-four
seconds after mount, i.e. mid-rip. None of that is needed here, since the disc ID
comes from `.TOC.plist` and the titles come from MusicBrainz; that same Snow
Patrol disc was identified correctly while still called `Audio CD`. Because the
rename can land mid-rip, the script follows the disc by its ID if the mount point
moves. Turning Music's auto-launch off avoids the situation entirely:

```sh
defaults write com.apple.digihub com.apple.digihub.cd.music.appeared -dict action 1
```

**Re-running is safe and resumes.** Each existing track is checked with
`flac --test`, which decodes it and verifies its MD5. Valid tracks are skipped;
a file truncated by an interrupted run fails the test and is re-ripped. An
interrupted import costs nothing but the time already spent.

**Rips are verified, not paranoid.** `flac --verify` decodes every track as it
encodes and fails loudly on a bad read. Because macOS exposes the CD as a
filesystem, this is the practical ceiling — true re-read-on-error ripping needs
`cdparanoia`, which doesn't work well with macOS drives.

**Finder won't show the album art on the FLAC files.** macOS's QuickLook
recognises `.flac` as audio but never parses its `PICTURE` block, so you get a
generic music-note icon. The art *is* in the files; players read it fine.

## Syncing to a server

The output is deliberately portable — no resource forks, no macOS-only
metadata, no characters that upset Linux or SMB filesystems:

```sh
rsync -av --delete --exclude '._*' --exclude '.DS_Store' \
  navidrome_music/ pi@raspberrypi.local:/path/to/music/
```

The excludes matter: browsing the folders in Finder sprinkles `.DS_Store` and
`._` AppleDouble files around, which Navidrome's scanner will complain about.
