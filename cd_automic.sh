#!/bin/bash
# macOS Auto CD Ripper for Navidrome
# Auto-tags using MusicBrainz via python musicbrainzngs library.
# Falls back to volume-name-based metadata if MusicBrainz lookup fails.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NAVIDROME_ROOT="./navidrome_music"
mkdir -p "$NAVIDROME_ROOT" 2>/dev/null || NAVIDROME_ROOT="/private/tmp/navidrome_music"

# ── Helpers ──────────────────────────────────────────────────────────────────

# Count .aiff files in a directory (strips wc leading whitespace)
count_aiff() {
    find "$1" -maxdepth 1 -name "*.aiff" -type f 2>/dev/null | wc -l | tr -d ' '
}

# Get track count from .TOC.plist (more reliable than counting .aiff files)
get_track_count_from_toc() {
    local toc_file="$1/.TOC.plist"
    if [ -f "$toc_file" ]; then
        python3 -c "
import plistlib
with open('$toc_file', 'rb') as f:
    toc = plistlib.load(f)
tracks = toc['Sessions'][0]['Track Array']
print(len(tracks))
" 2>/dev/null || echo "0"
    else
        echo "0"
    fi
}

# Query MusicBrainz using the mb_lookup.py helper.
# Outputs JSON to stdout on success, prints error to stderr and exits 1 on failure.
lookup_musicbrainz() {
    local toc_dir="$1"
    local disc_name="$2"
    local track_count="$3"
    
    local toc_file="$toc_dir/.TOC.plist"
    
    python3 "$SCRIPT_DIR/mb_lookup.py" \
        --toc "$toc_file" \
        --disc-name "$disc_name" \
        --track-count "$track_count" 2>/dev/null
}

# Tag a single FLAC file with the given metadata using metaflac.
tag_flac() {
    local flac_file="$1"
    local album="$2"
    local artist="$3"
    local track_number="$4"
    local track_title="$5"
    local track_total="${6:-}"
    
    local tag_data=""
    tag_data="ALBUM=${album}"
    tag_data+=$'\n'
    tag_data+="ARTIST=${artist}"
    tag_data+=$'\n'
    tag_data+="TRACKNUMBER=${track_number}"
    tag_data+=$'\n'
    tag_data+="TITLE=${track_title}"
    tag_data+=$'\n'
    
    if [ -n "$track_total" ]; then
        tag_data+=$'\n'
        tag_data+="TRACKTOTAL=${track_total}"
    fi
    
    metaflac --remove-all-tags --import-tags-from=- "$flac_file" <<< "$tag_data" 2>/dev/null
}

# ── Main Loop ────────────────────────────────────────────────────────────────

echo "🎬 Auto CD Ripper started! Watching /Volumes..." >&2

# Verify dependencies
type -p python3 >/dev/null || { echo "❌ Python3 required but not found" >&2; exit 1; }
type -p metaflac >/dev/null || { echo "❌ metaflac required but not found" >&2; exit 1; }
type -p ffmpeg >/dev/null || { echo "❌ ffmpeg required but not found" >&2; exit 1; }

while true; do
    while read -r vol_path; do
        [ -d "$vol_path" ] || continue
        
        DISC_NAME=$(basename "$vol_path")
        AIFF_DIR=""
        
        # Check if it's an Audio CD by looking for .aiff files
        file_count=$(count_aiff "$vol_path")
        
        if [[ $file_count -gt 0 ]]; then
            AIFF_DIR="$vol_path"
            echo "📀 Found Audio CD at root: $DISC_NAME" >&2
        elif [ -d "$vol_path/CD_AUDIO" ]; then
            file_count=$(count_aiff "$vol_path/CD_AUDIO")
            if [[ $file_count -gt 0 ]]; then
                AIFF_DIR="$vol_path/CD_AUDIO"
                echo "📀 Found Audio CD in folder: $DISC_NAME/CD_AUDIO" >&2
            fi
        else
            continue
        fi
        
        [ -z "$AIFF_DIR" ] && continue
        
        # ── Metadata Resolution ──────────────────────────────────────────────
        ARTIST="Unknown Artist"
        ALBUM=$(echo "$DISC_NAME" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/_/g')
        ARTIST_SAFE=$(echo "$ARTIST" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/_/g')
        MB_TITLE=""
        MB_DATE=""
        
        # Get track count from .TOC.plist (more reliable)
        toc_track_count=$(get_track_count_from_toc "$AIFF_DIR")
        [ "$toc_track_count" -eq 0 ] && toc_track_count=$file_count
        
        # Try MusicBrainz lookup
        if [[ $file_count -gt 0 ]]; then
            echo "   🔍 Looking up '$DISC_NAME' ($file_count tracks) on MusicBrainz..." >&2
            mb_json=$(lookup_musicbrainz "$AIFF_DIR" "$DISC_NAME" "$toc_track_count" 2>/dev/null) || true
            
            if [ -n "$mb_json" ]; then
                ARTIST=$(echo "$mb_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('artist','Unknown Artist'))")
                MB_TITLE=$(echo "$mb_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('title',''))")
                MB_DATE=$(echo "$mb_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('date',''))")
                ALBUM=$(echo "$MB_TITLE" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/_/g')
                ARTIST_SAFE=$(echo "$ARTIST" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/_/g')
                echo "   ✅ Found: $ARTIST - $MB_TITLE ($MB_DATE)" >&2
            else
                echo "   ⚠️  MusicBrainz lookup failed, using volume name as album" >&2
            fi
        fi
        
        # ── Rip & Convert ────────────────────────────────────────────────────
        DEST_DIR="$NAVIDROME_ROOT/$ARTIST_SAFE/$ALBUM"
        mkdir -p "$DEST_DIR"
        
        # Build track listing from MB JSON if available
        track_listing=""
        if [ -n "$mb_json" ] && [ -n "$MB_TITLE" ]; then
            track_listing=$(echo "$mb_json" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for t in d.get('tracks', []):
    print(f\"{t['position']}\t{t['title']}\")
" 2>/dev/null) || true
        fi
        
        # Collect .aiff files sorted naturally (1, 2, ..., 10, 11 ...)
        tmp_filelist=$(mktemp)
        find "$AIFF_DIR" -maxdepth 1 -name "*.aiff" -type f -print0 | \
            xargs -0 -I{} basename {} | \
            sort -V > "$tmp_filelist"
        
        track_num=0
        while IFS= read -r aiff_base; do
            track_num=$((track_num + 1))
            RAW_NAME="${aiff_base%.aiff}"
            SAFE_NAME=$(echo "$RAW_NAME" | tr '[:space:]' '_' | sed 's/__/_/g')
            
            OUT_FILE="$DEST_DIR/${SAFE_NAME}.flac"
            
            # Determine track title from MusicBrainz or fallback
            if [ -n "$track_listing" ]; then
                TRACK_TITLE=$(echo "$track_listing" | awk -F'\t' -v n="$track_num" '$1 == n {print $2}')
                [ -z "$TRACK_TITLE" ] && TRACK_TITLE="Unknown Track $track_num"
            else
                TRACK_TITLE="Track $track_num"
            fi
            
            # Progress indicator
            echo "   [${track_num}/${toc_track_count}] Rip: $TRACK_TITLE..." >&2
            
            # Rip and convert with ffmpeg (show progress via stderr)
            if ffmpeg -i "$AIFF_DIR/$aiff_base" \
                      -c:a flac -compression_level 6 \
                      -metadata title="$TRACK_TITLE" \
                      -metadata album="$MB_TITLE" \
                      -metadata artist="$ARTIST" \
                      -metadata date="$MB_DATE" \
                      -metadata comment="CD: $DISC_NAME" \
                      -y \
                      "$OUT_FILE" 2>&1 | sed 's/^/      /' >&2; then
                
                # Tag with metaflac for proper Vorbis comments (track number, total)
                if [ -n "$track_listing" ]; then
                    tag_flac "$OUT_FILE" "$MB_TITLE" "$ARTIST" "$track_num" "$TRACK_TITLE" "$toc_track_count" 2>/dev/null
                fi
                
                echo "   ✅ ${SAFE_NAME}.flac" >&2
            else
                echo "   ❌ Failed to rip: $aiff_base" >&2
            fi
        done < "$tmp_filelist"
        
        rm -f "$tmp_filelist"
        
        # Eject
        echo "   🚗 Ejecting: $DISC_NAME" >&2
        diskutil eject "$vol_path" >/dev/null 2>&1 &
        
        break  # Process one disc at a time
    done < <(find /Volumes -maxdepth 1 -type d 2>/dev/null)
    
    sleep 5
done
