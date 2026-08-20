#!/bin/bash
# Push freshly ripped FLACs to the Navidrome library on the Raspberry Pi.
#
# Transfers over SSH rather than through the SMB mount, for three reasons:
#   - writing over SMB is what created the 3,050 ._* AppleDouble files already
#     on the Pi; over SSH the files land on ext4 directly and none are made
#   - the SMB mount cannot traverse some accented paths (Låpsley,
#     José González), so it is not a trustworthy view of the library
#   - it is faster, resumable, and lets us verify the result afterwards
#
# Requires: rsync, ssh (key auth to $PI_HOST). Written for the bash 3.2 that
# ships with macOS.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

PI_HOST="${PI_HOST:-raspberrypi.local}"
PI_PATH="${PI_PATH:-/srv/shares/media/Music}"
LOCAL_ROOT="${LOCAL_ROOT:-$SCRIPT_DIR/navidrome_music}"

# Nothing is written or deleted unless DRY_RUN=0. Deliberately the default: this
# script deletes files on the Pi.
DRY_RUN="${DRY_RUN:-1}"

# What to do with a lossy copy of an album we now have as FLAC. Leaving both in
# place is not viable — they share ALBUM and ALBUMARTIST, so Navidrome merges
# them into one album listing every track twice.
#
#   move   - relocate it out of the library, into $SUPERSEDED_PATH (default)
#   delete - remove the files permanently
#   keep   - leave it alone and accept the duplicate listing
#
# 'move' is the default because the Pi is the only copy of this music, and
# because a lossy album folder can hold tracks the CD rip does not: singles and
# bonus tracks get filed alongside album tracks with numbering that does not
# match the disc. The dry run lists any such track before anything happens.
DUPLICATE_STRATEGY="${DUPLICATE_STRATEGY:-move}"

# Deliberately a sibling of the library, not inside it, so Navidrome stops
# indexing what lands here.
SUPERSEDED_PATH="${SUPERSEDED_PATH:-$(dirname "$PI_PATH")/_superseded}"

# Skip the confirmation prompt (for unattended runs). Ignored while DRY_RUN=1.
FORCE="${FORCE:-0}"

# Restart the Navidrome container afterwards to trigger a library scan. Off by
# default so this script never bounces a running service unasked.
RESCAN="${RESCAN:-0}"
NAVIDROME_CONTAINER="${NAVIDROME_CONTAINER:-navidrome-navidrome-1}"

SSH_OPTS="${SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=10}"
LOSSY_EXTS="mp3 m4a mp4 wma ogg"

log() { echo "$@" >&2; }
die() { log "❌ $*"; exit 1; }

remote() { ssh $SSH_OPTS "$PI_HOST" "$@"; }

# Compare artist/album names ignoring case, punctuation and the "(year)" suffix
# our own folders carry but the existing library's do not.
normalise() {
    # A leading "The" is dropped as well: the Pi had this album under
    # "Fratellis" while our rip filed it under "The Fratellis", so the duplicate
    # went unnoticed and both a lossy and a FLAC copy ended up in the library.
    printf '%s' "$1" \
        | sed -E 's/[[:space:]]*\([0-9]{4}\)[[:space:]]*$//' \
        | tr '[:upper:]' '[:lower:]' \
        | sed -E 's/^(the|a|an)[[:space:]]+//' \
        | sed -E 's/[^a-z0-9]//g'
}

# ── Preflight ────────────────────────────────────────────────────────────────

for tool in rsync ssh python3; do
    type -p "$tool" >/dev/null || die "$tool is required but not installed"
done
[ -d "$LOCAL_ROOT" ] || die "local library not found: $LOCAL_ROOT"

# Recent macOS ships openrsync, which supports neither --iconv nor --info=. Work
# out what this rsync can actually do rather than assuming GNU rsync 3.x.
RSYNC_FLAGS="-av --progress --partial"
ICONV_FLAG=""
if rsync --help 2>&1 | grep -q -- '--iconv'; then
    HAVE_ICONV=1
else
    HAVE_ICONV=0
fi

# --iconv only matters for decomposed (NFD) filenames, which macOS produces but
# MusicBrainz-derived names do not. Check rather than convert blindly: names that
# are already composed need no conversion, and converting twice would mangle them.
nfd_count="$(python3 - "$LOCAL_ROOT" <<'PY'
import os, sys, unicodedata
n = 0
for root, dirs, files in os.walk(sys.argv[1]):
    for name in dirs + files:
        p = os.path.join(root, name)
        if unicodedata.normalize("NFC", p) != p:
            n += 1
print(n)
PY
)"
if [ "${nfd_count:-0}" -gt 0 ]; then
    if [ "$HAVE_ICONV" = "1" ]; then
        ICONV_FLAG="--iconv=UTF-8-MAC,UTF-8"
        RSYNC_FLAGS="$RSYNC_FLAGS $ICONV_FLAG"
        log "   $nfd_count decomposed filename(s) — converting to NFC in transit"
    else
        die "$nfd_count filename(s) are in macOS decomposed (NFD) form and this
   rsync ($(rsync --version 2>/dev/null | head -1)) cannot convert them.
   They would arrive on the Pi as duplicate folders. Install GNU rsync:
       brew install rsync
   then re-run."
    fi
fi

log "🚚 Deploy to $PI_HOST:$PI_PATH"
[ "$DRY_RUN" = "1" ] && log "   (dry run — nothing will be written or deleted)"

remote true 2>/dev/null || die "cannot ssh to $PI_HOST (key auth working?)"
remote "[ -d '$PI_PATH' ]" || die "remote path does not exist: $PI_PATH"
remote "[ -w '$PI_PATH' ]" || die "remote path is not writable by this user: $PI_PATH"

local_kb="$(du -sk "$LOCAL_ROOT" | awk '{print $1}')"
free_kb="$(remote "df -Pk '$PI_PATH' | awk 'NR==2 {print \$4}'")"
log "   local: $((local_kb / 1024)) MB    free on Pi: $((free_kb / 1024)) MB"
[ "$free_kb" -gt "$local_kb" ] || die "not enough free space on the Pi"

# ── Find albums that already exist there in a lossy format ───────────────────

remote_albums="$(mktemp)"
local_albums="$(mktemp)"
dupes="$(mktemp)"
trap 'rm -f "$remote_albums" "$local_albums" "$dupes"' EXIT

# Artist/Album live at depth 2 in both trees.
remote "find '$PI_PATH' -mindepth 2 -maxdepth 2 -type d" 2>/dev/null \
    | sed "s|^$PI_PATH/||" > "$remote_albums"
[ -s "$remote_albums" ] || die "no albums found on the Pi — is $PI_PATH right?"

find "$LOCAL_ROOT" -mindepth 2 -maxdepth 2 -type d 2>/dev/null \
    | sed "s|^$LOCAL_ROOT/||" > "$local_albums"

log "   $(wc -l < "$local_albums" | tr -d ' ') local albums, $(wc -l < "$remote_albums" | tr -d ' ') already on the Pi"

# Build "normalised-key<TAB>real-remote-path" once, then match each local album.
remote_keys="$(mktemp)"
trap 'rm -f "$remote_albums" "$local_albums" "$dupes" "$remote_keys" "${remote_targets:-}"' EXIT
while IFS= read -r ra; do
    rartist="${ra%%/*}"; ralbum="${ra#*/}"
    printf '%s|%s\t%s\n' "$(normalise "$rartist")" "$(normalise "$ralbum")" "$ra" >> "$remote_keys"
done < "$remote_albums"

while IFS= read -r la; do
    [ -n "$la" ] || continue
    lartist="${la%%/*}"; lalbum="${la#*/}"
    key="$(normalise "$lartist")|$(normalise "$lalbum")"
    match="$(awk -F'\t' -v k="$key" '$1 == k {print $2; exit}' "$remote_keys")"
    [ -n "$match" ] && printf '%s\t%s\n' "$la" "$match" >> "$dupes"
done < "$local_albums"

dupe_count=$(wc -l < "$dupes" 2>/dev/null | tr -d ' ')
dupe_count=${dupe_count:-0}

if [ "$dupe_count" -gt 0 ]; then
    log ""
    # Several local albums can supersede one remote album — a 2-disc special
    # edition ripped as two folders maps to a single lossy folder. Group by the
    # remote path so it is reported and moved exactly once.
    remote_targets="$(mktemp)"
    cut -f2 "$dupes" | sort -u > "$remote_targets"
    target_count=$(grep -c . < "$remote_targets" || true)
    log "♻️  $target_count remote album(s) superseded by $dupe_count local album(s):"
    orphan_total=0
    lossy_tmp="$(mktemp)"
    # fd 3: ssh and python3 inside this loop both read stdin, and would otherwise
    # swallow the rest of the list and end the loop after one album.
    while IFS= read -r ra <&3; do
        locals_for_target="$(awk -F'\t' -v r="$ra" '$2 == r {print $1}' "$dupes")"
        log "     $ra"
        printf '%s\n' "$locals_for_target" | sed 's/^/       ← /' >&2

        remote "find '$PI_PATH/$ra' -maxdepth 1 -type f ! -name '._*' ! -name '.DS_Store' ! -name '*.flac' ! -name '*.jpg' -exec basename {} \\;" \
            </dev/null 2>/dev/null > "$lossy_tmp"
        n=$(grep -c . < "$lossy_tmp" || true)
        log "       ↳ $n lossy track(s)"

        # Compare against the FLACs in *every* local album mapping here.
        local_dirs=""
        while IFS= read -r la; do
            [ -n "$la" ] && local_dirs="$local_dirs
$LOCAL_ROOT/$la"
        done <<LOCALS
$locals_for_target
LOCALS
        orphans="$(printf '%s' "$local_dirs" | python3 -c '
import os, re, sys
def norm(name):
    stem = os.path.splitext(name)[0]
    stem = re.sub(r"^\s*\d+\s*[-._ ]\s*", "", stem)    # leading track number
    stem = re.sub(r"\((?:19|20)\d{2}\)", "", stem)      # "(2000)" style suffix
    stem = stem.lower().replace("&", "and")               # "Me & Mr" == "Me And Mr"
    return re.sub(r"[^a-z0-9]", "", stem)

listing = sys.argv[1]
dirs = [d for d in sys.stdin.read().splitlines() if d.strip()]
have = set()
for d in dirs:
    for root, _, files in os.walk(d):
        for f in files:
            if f.lower().endswith(".flac"):
                have.add(norm(f))
with open(listing, encoding="utf-8", errors="replace") as fh:
    for line in fh:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        key = norm(line)
        # A subtitle on either side still means the same recording, so treat a
        # prefix match as a match: "Everythings Not Lost" vs
        # "Everythings Not Lost Life Is for Living".
        if any(key == h or key.startswith(h) or h.startswith(key) for h in have):
            continue
        print(line)
' "$lossy_tmp")"

        if [ -n "$orphans" ]; then
            cnt=$(printf '%s\n' "$orphans" | grep -c . || true)
            orphan_total=$((orphan_total + cnt))
            log "       ⚠️  $cnt lossy track(s) with NO FLAC counterpart:"
            printf '%s\n' "$orphans" | sed 's/^/            /' >&2
        fi
    done 3< "$remote_targets"
    rm -f "$lossy_tmp"

    log ""
    log "   strategy: DUPLICATE_STRATEGY=$DUPLICATE_STRATEGY"
    case "$DUPLICATE_STRATEGY" in
        move)   log "      lossy copies move to $SUPERSEDED_PATH (reversible)" ;;
        delete) log "      lossy copies are DELETED PERMANENTLY" ;;
        keep)   log "      lossy copies stay put; Navidrome will list tracks twice" ;;
        *)      die "DUPLICATE_STRATEGY must be move, delete or keep" ;;
    esac
    if [ "$orphan_total" -gt 0 ]; then
        log "   ⚠️  $orphan_total lossy track(s) across these albums exist nowhere else."
        if [ "$DUPLICATE_STRATEGY" = "delete" ]; then
            log "      With delete, they are gone for good — and the Pi is the only copy."
            log "      Use DUPLICATE_STRATEGY=move instead unless you have checked each one."
        fi
    fi
fi

# ── Confirm ──────────────────────────────────────────────────────────────────

if [ "$DRY_RUN" = "1" ]; then
    log ""
    log "🔎 rsync dry run (no changes):"
    rsync -a --dry-run --itemize-changes \
        $ICONV_FLAG \
        --exclude='.*' \
        "$LOCAL_ROOT"/ "$PI_HOST:$PI_PATH"/ 2>&1 | sed 's/^/      /'
    log ""
    log "   Nothing was changed. Re-run with DRY_RUN=0 to apply."
    exit 0
fi

if [ "$FORCE" != "1" ]; then
    log ""
    if [ "$dupe_count" -gt 0 ]; then
        case "$DUPLICATE_STRATEGY" in
            move)   log "⚠️  This will MOVE the lossy albums above to $SUPERSEDED_PATH." ;;
            delete) log "⚠️  This will PERMANENTLY DELETE the lossy tracks above."
                    log "    The Pi is the only copy of this music." ;;
        esac
    fi
    printf '   Continue? [y/N] ' >&2
    read -r reply </dev/tty || reply=""
    case "$reply" in
        y|Y|yes|YES) ;;
        *) log "   aborted"; exit 1 ;;
    esac
fi

# ── Replace the lossy copies ─────────────────────────────────────────────────

if [ "$dupe_count" -gt 0 ] && [ "$DUPLICATE_STRATEGY" != "keep" ]; then
    while IFS= read -r ra <&3; do
        if [ "$DUPLICATE_STRATEGY" = "move" ]; then
            log "   📦 moving lossy $ra aside"
            # mv the whole album folder in one step, so nothing is half-moved.
            remote "mkdir -p '$SUPERSEDED_PATH/$(dirname "$ra")' && mv '$PI_PATH/$ra' '$SUPERSEDED_PATH/$ra'" \
                || log "      ⚠️  move failed; leaving $ra in place"
        else
            log "   🗑  deleting lossy $ra"
            # Only audio files and macOS debris, then remove the folder if that
            # leaves it empty. Anything unexpected is left alone rather than
            # silently destroyed.
            for e in $LOSSY_EXTS; do
                remote "find '$PI_PATH/$ra' -maxdepth 1 -type f -name '*.$e' -delete" 2>/dev/null
            done
            remote "find '$PI_PATH/$ra' -maxdepth 1 -type f \\( -name '._*' -o -name '.DS_Store' \\) -delete" 2>/dev/null
            remote "rmdir '$PI_PATH/$ra' 2>/dev/null" || true
        fi
        # Tidy the artist folder only if it is now genuinely empty.
        remote "rmdir \"\$(dirname '$PI_PATH/$ra')\" 2>/dev/null" || true
    done 3< "$remote_targets"
fi

# ── Transfer ─────────────────────────────────────────────────────────────────
#
# NOTE: --delete must NEVER be added here. The Pi holds ~4,000 tracks that do
# not exist locally; a delete pass would destroy the entire existing library.
#
# --iconv converts macOS's decomposed (NFD) filenames to the composed (NFC)
# form Linux expects. Without it, "Låpsley" arrives as a second, separate
# directory alongside the existing one.

log ""
log "📤 Transferring..."
rsync $RSYNC_FLAGS \
    --exclude='.*' \
    "$LOCAL_ROOT"/ "$PI_HOST:$PI_PATH"/ 2>&1 | sed 's/^/   /'
rsync_status=${PIPESTATUS[0]}
[ "$rsync_status" -eq 0 ] || die "rsync failed with status $rsync_status"

# ── Verify ───────────────────────────────────────────────────────────────────

log ""
log "🔍 Verifying..."
local_flacs="$(find "$LOCAL_ROOT" -name '*.flac' -type f | wc -l | tr -d ' ')"
remote_flacs="$(remote "find '$PI_PATH' -name '*.flac' -type f ! -name '._*' | wc -l" | tr -d ' ')"
log "   FLAC tracks — local: $local_flacs   on Pi: $remote_flacs"
if [ "$local_flacs" != "$remote_flacs" ]; then
    log "   ⚠️  counts differ. The Pi may hold FLACs from an earlier run, or the"
    log "      transfer was incomplete — re-run to reconcile."
fi

# Piped in via stdin rather than quoted inline — nested quoting through ssh is
# where scripts like this quietly break.
mixed="$(ssh $SSH_OPTS "$PI_HOST" 'bash -s' <<EOS 2>/dev/null
find '$PI_PATH' -mindepth 2 -maxdepth 2 -type d | while IFS= read -r d; do
    flac=\$(find "\$d" -maxdepth 1 -type f -name '*.flac' ! -name '._*' | head -1)
    [ -n "\$flac" ] || continue
    lossy=\$(find "\$d" -maxdepth 1 -type f ! -name '._*' ! -name '.DS_Store' ! -name '*.flac' ! -name '*.jpg' | head -1)
    [ -n "\$lossy" ] && printf '%s\n' "\$d"
done
EOS
)"
if [ -n "$mixed" ]; then
    log "   ⚠️  albums containing both FLAC and lossy files:"
    printf '%s\n' "$mixed" | sed "s|^$PI_PATH/|      |"
else
    log "   ✅ no album mixes FLAC and lossy files"
fi

# ── Rescan ───────────────────────────────────────────────────────────────────

if [ "$RESCAN" = "1" ]; then
    log ""
    log "🔄 Restarting $NAVIDROME_CONTAINER to trigger a scan"
    remote "docker restart '$NAVIDROME_CONTAINER'" >/dev/null 2>&1 \
        && log "   ✅ restarted" \
        || log "   ⚠️  could not restart the container; scan will happen on schedule"
else
    log ""
    log "ℹ️  Navidrome will pick these up on its next scheduled scan."
    log "   To force one now: RESCAN=1 $0   (restarts the container)"
fi

log ""
log "✅ Done."
