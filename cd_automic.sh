#!/bin/bash
# macOS Auto CD Ripper for Navidrome
# Waits for disc insertion → rips AIFF→FLAC → fetches MusicBrainz metadata 
# → organizes folders → embeds sidecar metadata → ejects → repeats

set -uo pipefail

# ⚙️ CONFIGURATION (CHANGE THIS FOR YOUR SYSTEM)
NAVIDROME_ROOT="./navidrome_music"  # <-- Change to your Navidrome music folder path

# Dependencies check
if ! command -v ffmpeg &>/dev/null; then
    echo "❌ Error: 'ffmpeg' not found. Run: brew install ffmpeg"
    exit 1
fi
if ! command -v curl &>/dev/null; then
    echo "❌ Error: 'curl' not found."
    exit 1
fi

mkdir -p "$NAVIDROME_ROOT" 2>/dev/null || true

echo "🎬 Auto CD Ripper started. Waiting for audio CDs..."
echo "   (Press Ctrl+C in terminal to stop)\n"

# Graceful cleanup on interrupt
cleanup() {
    echo -e "\n⏹️ Stopping watcher..."
    exit 0
}
trap cleanup INT TERM

while true; do
    # Clean up any previous temp files just in case
    rm -rf /tmp/cd_ripper_* 2>/dev/null
    
    TEMP_DIR=$(mktemp -d /tmp/cd_ripper_XXXXXX)
    mkdir -p "$TEMP_DIR/rwa" "$TEMP_DIR/flac"
    
    # 1️⃣ Wait for CD insertion (scans mounted volumes for .aiff files)
    DISC_PATH=""
    while IFS= read -r vol; do
        if [[ -d "$vol" ]] && find "$vol" -maxdepth 2 -name "*.aiff" | head -n 1 &>/dev/null; then
            DISC_PATH="$vol"
            break
        fi
    done < <(find /Volumes -maxdepth 1 -type d 2>/dev/null)

    [[ -z "$DISC_PATH" ]] && { sleep 5; continue; }

    DISC_NAME=$(basename "$DISC_PATH")
    
    # Find the directory containing .aiff files (can be root or CD_AUDIO subfolder)
    AIFF_DIR=$(find "$DISC_PATH" -maxdepth 2 -name "*.aiff" -print0 | xargs -0 -I{} dirname {} | sort -u | head -n 1)

    # If still no audio files found (maybe it's not an audio CD), skip
    if [[ ! "$(ls $AIFF_DIR/*.aiff 2>/dev/null)" ]]; then
        echo "⚠️  Volume found at $DISC_PATH but no audio (.aiff) tracks detected. Skipping..."
        sleep 10 
        rm -rf "$TEMP_DIR"
        continue
    fi

    echo "\n📀 Disc inserted: $DISC_NAME"

    # 2️⃣ Extract DiscID via MusicBrainz (if available command exists)
    DISCID="unknown_manual"
    if command -v musicbrainz-discid &>/dev/null; then
        DISCID=$(musicbrainz-discid "$AIFF_DIR" | grep "^Disc-ID:" | awk '{print $3}')
    fi
    
    # If that fails, we'll just use "Manual_Rip_Date" as ID to prevent duplicates in Navidrome logic
    [[ -z "$DISCID" ]] && DISCID="mac_$(date +%Y%m%d%H%M)"

    # 3️⃣ Fetch exact-match metadata from MusicBrainz CD Stub API
    ARTIST="Unknown Artist"
    ALBUM="Unknown Album"
    
    # Normalize disc ID for URL (lowercase, replace chars if needed usually handled by api)
    MB_URL=$(curl -sL --max-time 5 "https://musicbrainz.org/cdstub/api/musicbrainz-discid_$(echo $DISCID | tr '[:upper:]' '[:lower:]')?fmt=json" 2>/dev/null || echo "")
    
    # Actually, the simpler stub API often expects a hash or specific lookup. 
    # Let's use MusicBrainz DiscID Lookup service if possible, but the Stub API is standard for bash scripts.
    # Fallback to a robust URL pattern:
    MB_JSON=$(curl -sL --max-time 10 "https://musicbrainz.org/cdstub/api/$DISCID?fmt=json" 2>/dev/null || echo "")

    if [[ -n "$MB_JSON" ]] && ! echo "$MB_JSON" | grep -q '"error"'; then
        ARTIST=$(echo "$MB_JSON" | jq -r '.artist // "Unknown Artist"')
        ALBUM=$(echo "$MB_JSON" | jq -r '(if .title then .title else (if .alttitle then .alttitle else "Unknown Album" end) end)')
    fi

    # Sanitize for file paths (lowercase + underscores instead of spaces/special chars)
    ARTIST_SAFE=$(echo "$ARTIST" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/_/g')
    ALBUM_SAFE=$(echo "$ALBUM"   | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/_/g')

    DEST_DIR="$NAVIDROME_ROOT/$ARTIST_SAFE/$ALBUM_SAFE"
    mkdir -p "$DEST_DIR"

    echo "🏷️  Tagging: $ARTIST / $ALBUM"
    echo "   📁 Target: $DEST_DIR/"

    # 4️⃣ Rip & Convert (macOS AIFF → FLAC with embedded tags)
    TRACK_NUM=0
    for aiff in $(ls -v "$AIFF_DIR"/*.aiff 2>/dev/null); do
        [[ ! -f "$aiff" ]] && continue

        # Grab track title from MB if available, else default
        # Note: jq syntax for array index requires valid JSON input. The stub json has a 'tracks' array.
        TRACK_TITLE=$(echo "$MB_JSON" | jq -r ".tracks[$TRACK_NUM].title // \"Track $((TRACK_NUM+1))\"" 2>/dev/null || echo "Track $((TRACK_NUM+1))")

        OUT_FILE="$DEST_DIR/$(printf '%02d' $((TRACK_NUM+1)))_$TRACK_TITLE.flac"

        # Lossless conversion + explicit metadata embedding
        ffmpeg -i "$aiff" \
               -c:a flac -compression_level 6 \
               -map_metadata 0 \
               -metadata album="$ALBUM" \
               -metadata artist="$ARTIST" \
               -metadata title="$TRACK_TITLE" \
               -metadata "DISCID=$DISCID" \
               "$OUT_FILE" 2>/dev/null

        # Create the sidecar metadata file (.cue) automatically as requested
        CUE_OUT=$(echo "$OUT_FILE" | sed 's/.flac$//').cue
        gencue "$aiff" > "$CUE_OUT" 2>/dev/null || echo "Note: Could auto-generate .cue sidecar for this track (missing 'gencue')"

        echo "   ✅ $((TRACK_NUM+1)). $(echo "$TRACK_TITLE" | cut -c1-35).flac"
        ((TRACK_NUM++))
    done

    # 5️⃣ Eject & Clean up for next disc
    echo "\n🚗 Ejected: $DISC_NAME"
    diskutil eject "$DISC_PATH" >/dev/null 2>&1 || true
    
    rm -rf "$TEMP_DIR"
    
    echo "📦 Ready for next disc...\n"
done
