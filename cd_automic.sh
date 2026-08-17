#!/bin/bash
# macOS Auto CD Ripper for Navidrome
#
# Watches /Volumes for mounted Audio CDs, encodes each track to FLAC, tags it
# from MusicBrainz (disc ID lookup via mb_lookup.py), files it into a
# Navidrome-friendly Artist/Album tree, then ejects the disc.
#
# macOS presents an inserted Audio CD as a read-only volume of .aiff files plus
# a .TOC.plist describing the disc layout. We read those files rather than
# talking to the drive directly, so the OS handles the actual disc reads.
#
# Requires: flac, metaflac, python3, curl. Written for the bash 3.2 that ships
# with macOS.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NAVIDROME_ROOT="${NAVIDROME_ROOT:-$SCRIPT_DIR/navidrome_music}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"
EJECT_WHEN_DONE="${EJECT_WHEN_DONE:-1}"
FETCH_COVER_ART="${FETCH_COVER_ART:-1}"
# 1 = tick a live elapsed-time line while each track encodes (interactive only);
# 0 = print nothing until the track's summary line.
SHOW_ENCODE_PROGRESS="${SHOW_ENCODE_PROGRESS:-1}"
USER_AGENT="cd-importer/1.0 ( hello@domharrington.email )"

log() { echo "$@" >&2; }

# CD audio is 44100Hz x 16-bit x 2ch = 176400 bytes per second, so an .aiff's
# size gives its playing time without having to probe the file.
BYTES_PER_SECOND=176400

file_duration() {
    local bytes
    bytes="$(stat -f%z "$1" 2>/dev/null)" || { echo 0; return; }
    echo $((bytes / BYTES_PER_SECOND))
}

# 284 -> "4:44"
fmt_mmss() {
    printf '%d:%02d' $(($1 / 60)) $(($1 % 60))
}

# 372 -> "6m 12s", 45 -> "45s"
fmt_elapsed() {
    if [ "$1" -ge 60 ]; then
        printf '%dm %02ds' $(($1 / 60)) $(($1 % 60))
    else
        printf '%ds' "$1"
    fi
}

# Encode speed relative to real time, e.g. "7.5x". Guards against a zero
# elapsed time on very short tracks.
fmt_speed() {
    local audio="$1" elapsed="$2"
    [ "$elapsed" -le 0 ] && elapsed=1
    awk -v a="$audio" -v e="$elapsed" 'BEGIN { printf "%.1fx", a / e }'
}

fmt_mb() {
    awk -v b="$1" 'BEGIN { printf "%.1f MB", b / 1048576 }'
}

# Make a string safe to use as a file or directory name while keeping it
# readable: strip path separators and characters that confuse other tools,
# collapse whitespace, and trim leading dots so nothing ends up hidden.
sanitize() {
    echo "$1" \
        | tr '/:' '--' \
        | tr -d '\\?*<>|"' \
        | sed -e 's/[[:space:]][[:space:]]*/ /g' \
              -e 's/^[[:space:].]*//' \
              -e 's/[[:space:].]*$//'
}

count_aiff() {
    find "$1" -maxdepth 1 -name "*.aiff" -type f 2>/dev/null | wc -l | tr -d ' '
}

# Number of audio tracks according to .TOC.plist, which is authoritative even
# when the filenames are unhelpful. Prints 0 if unavailable.
toc_track_count() {
    local toc_file="$1/.TOC.plist"
    [ -f "$toc_file" ] || { echo 0; return; }
    python3 -c '
import plistlib, sys
try:
    toc = plistlib.load(open(sys.argv[1], "rb"))
    tracks = toc["Sessions"][0]["Track Array"]
    print(len([t for t in tracks if not t.get("Data")]))
except Exception:
    print(0)
' "$toc_file" 2>/dev/null || echo 0
}

# Read a field out of the mb_lookup.py JSON payload.
mb_field() {
    python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get(sys.argv[1], "") or "")
except Exception:
    pass
' "$1" <<< "$2" 2>/dev/null
}

# Emit the MusicBrainz track list as "position<TAB>title<TAB>artist" lines.
mb_track_table() {
    python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for track in data.get("tracks", []):
    print("%s\t%s\t%s" % (track["position"], track["title"], track.get("artist", "")))
' <<< "$1" 2>/dev/null
}

# Fetch front cover art into $3/cover.jpg, trying the exact release first and
# then the release group. A specific pressing frequently has no cover of its
# own while the album as a whole does, so the group is a genuine second chance
# rather than a duplicate request.
fetch_cover_art() {
    local mbid="$1" rgid="$2" dest_dir="$3" kind id size url
    [ -f "$dest_dir/cover.jpg" ] && return 0
    for kind in release:$mbid release-group:$rgid; do
        id="${kind#*:}"
        [ -n "$id" ] || continue
        for size in front-1200 front-500 front; do
            url="https://coverartarchive.org/${kind%%:*}/$id/$size"
            # --retry covers the 5xx blips the Cover Art Archive throws when busy.
            if curl -fsSL --max-time 30 --retry 3 --retry-delay 2 \
                    --retry-all-errors -A "$USER_AGENT" \
                    "$url" -o "$dest_dir/cover.jpg" 2>/dev/null \
               && [ -s "$dest_dir/cover.jpg" ]; then
                return 0
            fi
        done
    done
    rm -f "$dest_dir/cover.jpg"
    return 1
}

# Write cover.jpg into every FLAC in a folder as a front-cover PICTURE block.
embed_cover_art() {
    local dest_dir="$1" flac_file
    [ -f "$dest_dir/cover.jpg" ] || return 1
    for flac_file in "$dest_dir"/*.flac; do
        [ -f "$flac_file" ] || continue
        metaflac --remove --block-type=PICTURE --dont-use-padding \
            "$flac_file" </dev/null 2>/dev/null
        metaflac --import-picture-from="3||||$dest_dir/cover.jpg" \
            "$flac_file" </dev/null 2>/dev/null
    done
}

process_disc() {
    local vol_path="$1" aiff_dir="$2" file_count="$3"
    local disc_name track_total mb_json
    disc_name="$(basename "$vol_path")"

    track_total="$(toc_track_count "$aiff_dir")"
    [ "$track_total" -eq 0 ] && track_total="$file_count"

    # ── Metadata ─────────────────────────────────────────────────────────────
    # Look up once, keeping stdout (the JSON) separate from stderr (progress).
    log "   🔍 Looking up '$disc_name' ($file_count tracks) on MusicBrainz..."
    local mb_out
    mb_out="$(mktemp)" || return 1
    python3 "$SCRIPT_DIR/mb_lookup.py" \
        --toc "$aiff_dir/.TOC.plist" \
        --disc-name "$disc_name" \
        --track-count "$track_total" \
        >"$mb_out" 2> >(sed 's/^/      /' >&2) </dev/null
    mb_json="$(cat "$mb_out")"
    rm -f "$mb_out"

    local artist album album_artist date year mbid release_group
    local disc_number disc_total
    if [ -n "$mb_json" ]; then
        artist="$(mb_field artist "$mb_json")"
        album_artist="$(mb_field album_artist "$mb_json")"
        album="$(mb_field title "$mb_json")"
        date="$(mb_field date "$mb_json")"
        year="$(mb_field year "$mb_json")"
        mbid="$(mb_field mbid "$mb_json")"
        release_group="$(mb_field release_group "$mb_json")"
        disc_number="$(mb_field disc_number "$mb_json")"
        disc_total="$(mb_field disc_total "$mb_json")"
        log "   ✅ $artist - $album ($year)"
    else
        log "   ⚠️  MusicBrainz lookup failed; falling back to disc and file names"
        artist="Unknown Artist"
        album_artist="Unknown Artist"
        album="$disc_name"
        date=""; year=""; mbid=""; release_group=""
        disc_number=1; disc_total=1
    fi
    [ -n "$album" ] || album="$disc_name"
    [ -n "$artist" ] || artist="Unknown Artist"
    [ -n "$album_artist" ] || album_artist="$artist"
    case "$disc_number" in ''|*[!0-9]*) disc_number=1 ;; esac
    case "$disc_total" in ''|*[!0-9]*) disc_total=1 ;; esac

    # ── Destination ──────────────────────────────────────────────────────────
    local artist_dir album_dir dest_dir
    artist_dir="$(sanitize "$album_artist")"
    album_dir="$(sanitize "$album")"
    [ -n "$year" ] && album_dir="$album_dir ($year)"
    if [ "$disc_total" -gt 1 ]; then
        album_dir="$album_dir/Disc $disc_number"
    fi
    dest_dir="$NAVIDROME_ROOT/$artist_dir/$album_dir"
    mkdir -p "$dest_dir" || { log "   ❌ Cannot create $dest_dir"; return 1; }
    log "   📂 $dest_dir"

    local mb_tracks=""
    [ -n "$mb_json" ] && mb_tracks="$(mb_track_table "$mb_json")"

    # ── Build a numerically sorted "track number <TAB> filename" work list ───
    # Track numbers come from the leading digits macOS puts in each filename,
    # so a missing or unreadable track can't silently shift everything after it.
    local work_list
    work_list="$(mktemp)" || return 1
    find "$aiff_dir" -maxdepth 1 -name "*.aiff" -type f -print0 2>/dev/null \
        | python3 -c '
import os, re, sys
entries = []
for path in sys.stdin.buffer.read().split(b"\0"):
    if not path:
        continue
    name = os.path.basename(path.decode("utf-8", "surrogateescape"))
    stem = name[:-5]
    match = re.match(r"^\s*(\d+)\s*[-._ ]\s*(.*)$", stem)
    if match:
        entries.append((int(match.group(1)), match.group(2).strip(), name))
    else:
        entries.append((None, stem.strip(), name))
# Files without a leading number keep disc order and fill the gaps.
used = set(n for n, _, _ in entries if n)
nxt = 1
for i, (num, title, name) in enumerate(entries):
    if num is None:
        while nxt in used:
            nxt += 1
        used.add(nxt)
        entries[i] = (nxt, title, name)
entries.sort(key=lambda e: e[0])
for num, title, name in entries:
    print("%d\t%s\t%s" % (num, title, name))
' > "$work_list"

    if [ ! -s "$work_list" ]; then
        log "   ❌ No .aiff tracks could be listed"
        rm -f "$work_list"
        return 1
    fi

    # ── Encode ───────────────────────────────────────────────────────────────
    local ripped=0 failed=0 skipped=0
    local album_start audio_total=0 bytes_total=0 audio_done=0
    album_start="$(date +%s)"

    # Total playing time of the disc, so each track line can show how far
    # through the album we are.
    local aiff_file
    for aiff_file in "$aiff_dir"/*.aiff; do
        [ -f "$aiff_file" ] || continue
        audio_total=$((audio_total + $(file_duration "$aiff_file")))
    done
    # Read the work list on fd 4, not stdin. Encoders read stdin for their own
    # purposes, and one that consumes it swallows the rest of this loop's input
    # — which is what mangled the filenames on the earlier ffmpeg-based runs.
    while IFS=$'\t' read -r track_num file_title aiff_base <&4; do
        [ -n "$aiff_base" ] || continue

        local title track_artist
        title="$(printf '%s\n' "$mb_tracks" \
            | awk -F'\t' -v n="$track_num" '$1 == n { print $2; exit }')"
        track_artist="$(printf '%s\n' "$mb_tracks" \
            | awk -F'\t' -v n="$track_num" '$1 == n { print $3; exit }')"
        # Prefer MusicBrainz, then the title macOS put in the filename, then a
        # placeholder. "Audio Track" is what macOS writes when it knows nothing.
        if [ -z "$title" ]; then
            case "$file_title" in
                ""|"Audio Track"|"Audio Track "*) title="Track $track_num" ;;
                *) title="$file_title" ;;
            esac
        fi
        [ -n "$track_artist" ] || track_artist="$artist"

        local out_name out_file
        out_name="$(printf '%02d - %s' "$track_num" "$(sanitize "$title")")"
        out_file="$dest_dir/$out_name.flac"

        local track_audio
        track_audio="$(file_duration "$aiff_dir/$aiff_base")"

        if [ -f "$out_file" ] && flac --totally-silent --test "$out_file" </dev/null 2>/dev/null; then
            log "   ⏭  [$track_num/$track_total] $title (already ripped)"
            audio_done=$((audio_done + track_audio))
            bytes_total=$((bytes_total + $(stat -f%z "$out_file" 2>/dev/null || echo 0)))
            ripped=$((ripped + 1))
            skipped=$((skipped + 1))
            continue
        fi

        log "   💿 [$track_num/$track_total] $title ($(fmt_mmss "$track_audio"))"

        # Build the tag list as an array so values containing spaces, quotes or
        # parentheses survive intact.
        local tags
        tags=(
            --tag="TITLE=$title"
            --tag="ARTIST=$track_artist"
            --tag="ALBUM=$album"
            --tag="ALBUMARTIST=$album_artist"
            --tag="TRACKNUMBER=$track_num"
            --tag="TRACKTOTAL=$track_total"
            --tag="DISCNUMBER=$disc_number"
            --tag="DISCTOTAL=$disc_total"
        )
        [ -n "$date" ] && tags[${#tags[@]}]="--tag=DATE=$date"
        [ -n "$mbid" ] && tags[${#tags[@]}]="--tag=MUSICBRAINZ_ALBUMID=$mbid"
        # Recorded so --art can find the artwork later without the disc.
        [ -n "$release_group" ] && \
            tags[${#tags[@]}]="--tag=MUSICBRAINZ_RELEASEGROUPID=$release_group"

        # --verify decodes as it encodes and fails loudly on a bad read, which
        # is the closest thing to rip verification available without cdparanoia.
        # --totally-silent suppresses flac's banner and its per-file "skipping
        # unknown chunk 'FVER'" notice, which are pure noise once per track; we
        # detect failure from the exit status and report it ourselves.
        # </dev/null matters: flac reads stdin, and without this it would eat
        # the rest of the work list this loop is reading.
        local flac_cmd
        flac_cmd=(flac --best --verify --totally-silent --force
                  "${tags[@]}" -o "$out_file" "$aiff_dir/$aiff_base")

        local track_start track_elapsed out_bytes status
        track_start="$(date +%s)"
        if [ "$SHOW_ENCODE_PROGRESS" = "1" ] && [ -t 2 ]; then
            # Encode in the background so we can tick a single, self-overwriting
            # progress line. Only when stderr is a terminal — writing \r into a
            # redirected log would just produce very long lines.
            local pid
            "${flac_cmd[@]}" </dev/null &
            pid=$!
            while kill -0 "$pid" 2>/dev/null; do
                printf '\r      ⏳ %s' "$(fmt_elapsed $(( $(date +%s) - track_start )))" >&2
                sleep 1
            done
            wait "$pid"
            status=$?
            printf '\r\033[K' >&2
        else
            "${flac_cmd[@]}" </dev/null
            status=$?
        fi

        if [ "$status" -eq 0 ]; then
            track_elapsed=$(( $(date +%s) - track_start ))
            out_bytes="$(stat -f%z "$out_file" 2>/dev/null || echo 0)"
            audio_done=$((audio_done + track_audio))
            bytes_total=$((bytes_total + out_bytes))
            ripped=$((ripped + 1))
            log "      ✅ $(fmt_elapsed "$track_elapsed") @ $(fmt_speed "$track_audio" "$track_elapsed"), $(fmt_mb "$out_bytes") — $(fmt_mmss "$audio_done")/$(fmt_mmss "$audio_total") of album"
        else
            log "      ❌ Failed: $aiff_base"
            rm -f "$out_file"
            failed=$((failed + 1))
        fi
    done 4< "$work_list"

    rm -f "$work_list"

    # ── Cover art ────────────────────────────────────────────────────────────
    # Navidrome reads cover.jpg from the album folder; embedding it as well
    # keeps the artwork with the files if they ever move.
    if [ "$FETCH_COVER_ART" = "1" ] && [ -n "$mbid" ] && [ "$ripped" -gt 0 ]; then
        if fetch_cover_art "$mbid" "$release_group" "$dest_dir"; then
            embed_cover_art "$dest_dir"
            log "   🖼  cover.jpg"
        else
            # Say so rather than finishing silently: the tracks are fine, but
            # the album will look bare in Navidrome until art is backfilled.
            log "   ⚠️  No cover art available for this release"
            log "      retry later with: $0 --art \"$dest_dir\""
        fi
    fi

    # ── Album summary ────────────────────────────────────────────────────────
    local album_elapsed summary
    album_elapsed=$(( $(date +%s) - album_start ))
    summary="$ripped/$track_total tracks"
    [ "$skipped" -gt 0 ] && summary="$summary ($skipped already present)"
    [ "$failed" -gt 0 ] && summary="$summary, $failed FAILED"
    log "   📊 $album_artist - $album: $summary"
    log "      $(fmt_mmss "$audio_total") of audio in $(fmt_elapsed "$album_elapsed") @ $(fmt_speed "$audio_total" "$album_elapsed") — $(fmt_mb "$bytes_total") written"

    [ "$failed" -eq 0 ] && [ "$ripped" -gt 0 ]
}

# Backfill cover art into an already-ripped album folder, no disc required.
# Reads the MusicBrainz IDs back out of the FLAC tags, so this works whenever
# the art fetch failed at rip time (Cover Art Archive outage, or a release that
# had no cover of its own).
backfill_art() {
    local dest_dir="$1" first mbid rgid
    if [ ! -d "$dest_dir" ]; then
        log "❌ Not a directory: $dest_dir"
        return 1
    fi

    first=""
    for first in "$dest_dir"/*.flac; do
        [ -f "$first" ] && break
    done
    if [ ! -f "$first" ]; then
        log "❌ No FLAC files in $dest_dir"
        return 1
    fi

    mbid="$(metaflac --show-tag=MUSICBRAINZ_ALBUMID "$first" 2>/dev/null | cut -d= -f2-)"
    rgid="$(metaflac --show-tag=MUSICBRAINZ_RELEASEGROUPID "$first" 2>/dev/null | cut -d= -f2-)"

    if [ -z "$mbid" ] && [ -z "$rgid" ]; then
        log "❌ No MusicBrainz IDs in the tags; cannot look up art for $dest_dir"
        return 1
    fi

    # Older rips predate the release-group tag, so resolve it on the fly.
    if [ -z "$rgid" ] && [ -n "$mbid" ]; then
        log "   🔍 Resolving release group for $mbid..."
        rgid="$(python3 "$SCRIPT_DIR/mb_lookup.py" --release-group "$mbid" 2>/dev/null)"
    fi

    log "   🔍 Fetching art (release ${mbid:-none}, group ${rgid:-none})..."
    rm -f "$dest_dir/cover.jpg"
    if fetch_cover_art "$mbid" "$rgid" "$dest_dir"; then
        embed_cover_art "$dest_dir"
        log "   🖼  cover.jpg written and embedded in $(basename "$dest_dir")"
        return 0
    fi
    log "   ❌ Still no cover art available for $(basename "$dest_dir")"
    return 1
}

# ── Main ─────────────────────────────────────────────────────────────────────

for tool in python3 flac metaflac curl; do
    type -p "$tool" >/dev/null || { log "❌ $tool is required but not installed"; exit 1; }
done

# --art [dir]: backfill artwork instead of watching for discs. With no
# directory, sweeps every album in the library that is missing cover.jpg.
if [ "${1:-}" = "--art" ]; then
    if [ -n "${2:-}" ]; then
        backfill_art "$2"
        exit $?
    fi
    log "🖼  Backfilling cover art for albums missing cover.jpg..."
    missing=0; fixed=0
    while IFS= read -r album_dir <&3; do
        [ -f "$album_dir/cover.jpg" ] && continue
        missing=$((missing + 1))
        log "📂 $album_dir"
        backfill_art "$album_dir" && fixed=$((fixed + 1))
    done 3< <(find "$NAVIDROME_ROOT" -type d -depth 2 2>/dev/null | sort)
    log "📊 $fixed of $missing albums fixed"
    exit 0
fi

mkdir -p "$NAVIDROME_ROOT" || { log "❌ Cannot write to $NAVIDROME_ROOT"; exit 1; }

log "🎬 Auto CD Ripper started — watching /Volumes"
log "   Library: $NAVIDROME_ROOT"

# Volumes we have already handled this session, so a disc that fails to eject
# doesn't get re-ripped every five seconds.
handled_vols=()

# Note the ${arr[@]+"${arr[@]}"} guard throughout: under `set -u`, bash 3.2
# treats an empty array expansion as an unbound variable.
is_handled() {
    local v
    for v in ${handled_vols[@]+"${handled_vols[@]}"}; do
        [ "$v" = "$1" ] && return 0
    done
    return 1
}

# Forget volumes that have gone away, whichever way they went — auto-ejected,
# ejected by hand, or the drive unplugged. Without this a disc that didn't eject
# cleanly would be ignored for the rest of the session if you re-inserted it.
prune_handled() {
    local kept=() v
    for v in ${handled_vols[@]+"${handled_vols[@]}"}; do
        [ -d "$v" ] && kept[${#kept[@]}]="$v"
    done
    handled_vols=(${kept[@]+"${kept[@]}"})
}

while true; do
    prune_handled

    while read -r vol_path <&3; do
        [ -d "$vol_path" ] || continue
        [ "$vol_path" = "/Volumes" ] && continue

        is_handled "$vol_path" && continue

        aiff_dir=""
        file_count="$(count_aiff "$vol_path")"
        if [ "$file_count" -gt 0 ]; then
            aiff_dir="$vol_path"
            log "📀 Audio CD: $(basename "$vol_path")"
        elif [ -d "$vol_path/CD_AUDIO" ]; then
            file_count="$(count_aiff "$vol_path/CD_AUDIO")"
            if [ "$file_count" -gt 0 ]; then
                aiff_dir="$vol_path/CD_AUDIO"
                log "📀 Audio CD: $(basename "$vol_path")/CD_AUDIO"
            fi
        fi
        [ -n "$aiff_dir" ] || continue

        handled_vols[${#handled_vols[@]}]="$vol_path"

        if process_disc "$vol_path" "$aiff_dir" "$file_count"; then
            if [ "$EJECT_WHEN_DONE" = "1" ]; then
                log "   🚗 Ejecting $(basename "$vol_path")"
                diskutil eject "$vol_path" >/dev/null 2>&1 \
                    || log "   ⚠️  Eject failed; remove the disc manually"
            fi
        else
            log "   ⚠️  Leaving disc mounted so you can retry"
        fi

        break  # one disc at a time; prune_handled clears it once it unmounts
    done 3< <(find /Volumes -maxdepth 1 -type d 2>/dev/null)

    sleep "$POLL_INTERVAL"
done
