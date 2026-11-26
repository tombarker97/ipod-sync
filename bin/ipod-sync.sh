#!/bin/bash
# iPod (Rockbox) auto-sync - run by launchd whenever a drive is mounted,
# or by hand:  ipod-sync --dry-run  |  ipod-sync --real  |  ipod-sync --check
# https://github.com/tombarker97/ipod-sync
#
#  1. detect the iPod           5. write playlists to the iPod
#  2. back up Rockbox settings  6. report stray music outside /Music
#  3. collect plays for Last.fm 7. tidy, flush and eject
#  4. mirror the library        8. upload plays to Last.fm (iPod not needed)
#  9. summary notification + report page (~/Music/iPod Sync/Last sync.html)

# ---- settings -------------------------------------------------------------
# Defaults. Don't edit here: put your own values in ~/.config/ipod-sync/config
# (created by install.sh) so updates never overwrite them.
IPOD_NAME="IPOD"                          # the iPod's name as shown in Finder
DRY_RUN="no"                              # "yes" = change nothing, only log
SRC="$HOME/Music/Music Library/"          # your library; trailing slash matters
PLAYLISTS="$HOME/Music/Playlists"         # .m3u8 files to put on the iPod
BACKUP="$HOME/Music/RockboxBackup"        # Rockbox settings + playlists
SCROBBLES="$HOME/Music/Rockbox Scrobbles" # play logs, Last.fm queue, archive
REPORT_DIR="$HOME/Music/iPod Sync"        # report page + sync history
IPOD_TZ="$(readlink /etc/localtime 2>/dev/null | sed 's|.*zoneinfo/||')"
LOG="$HOME/Library/Logs/ipod-sync.log"

DO_SCROBBLES="yes"
DO_FILES="yes"
DO_PLAYLISTS="yes"
EJECT="yes"
KEEP_BACKUPS=10

SOUNDS="yes"                              # start / done / problem sounds
OPEN_REPORT="problems"                    # open the report page: never | problems | always
LASTFM_USER=""                            # Last.fm username, for a link in the report

PYTHON="$HOME/.ipod-sync/venv/bin/python"
RB_SCROBBLER="$HOME/.ipod-sync/rb-scrobbler"

CONFIG="${IPOD_SYNC_CONFIG:-$HOME/.config/ipod-sync/config}"
# shellcheck source=/dev/null
[ -f "$CONFIG" ] && . "$CONFIG"
TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ipod_tools.py"
IPOD_TZ="${IPOD_TZ:-UTC}"
# ---------------------------------------------------------------------------

MANUAL="no"
case "$1" in
  --dry-run) DRY_RUN="yes"; MANUAL="yes" ;;
  --real)    DRY_RUN="no";  MANUAL="yes" ;;
  --check)   CHECK="yes";   MANUAL="yes" ;;
  --export-playlists)   # one-off: copy Music.app playlists into $PLAYLISTS
    exec "$PYTHON" "$TOOLS" playlists-export --library "${SRC%/}" --playlists "$PLAYLISTS" \
         --data "$SCROBBLES" "${@:2}" ;;
  -h|--help)
    echo "usage: ipod-sync [--dry-run | --real | --check | --export-playlists [--include-smart]]"
    echo "settings: $CONFIG    log: $LOG"; exit 0 ;;
esac

# IPOD_SYNC_PATH_PREFIX lets the tests put stand-in commands first
export PATH="${IPOD_SYNC_PATH_PREFIX:+$IPOD_SYNC_PATH_PREFIX:}/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
IPOD="${VOLUMES:-/Volumes}/$IPOD_NAME"
LOCK="${IPOD_SYNC_LOCK:-/tmp/ipod-sync.lock}"
LOG_DIR="$(dirname "$LOG")"
DEST="$IPOD/Music/"
REPORT="$REPORT_DIR/Last sync.html"
STARTED=$(date +%s)
PROBLEMS=()
problem() { PROBLEMS+=("$1"); echo "PROBLEM: $1"; }

# notify "title" "message" [open-report]
#  uses terminal-notifier when installed (own icon in Notification settings,
#  banner replaces the previous one, click opens the report); else osascript
notify() {
  if command -v terminal-notifier >/dev/null 2>&1; then
    local args=(-title "iPod" -subtitle "$1" -message "${2:- }" -group ipod-sync)
    [ -n "$3" ] && [ -f "$REPORT" ] && args+=(-open "file://${REPORT// /%20}")
    terminal-notifier "${args[@]}" >/dev/null 2>&1 && return
  fi
  osascript -e "display notification \"${2//\"/}\" with title \"iPod\" subtitle \"${1//\"/}\"" 2>/dev/null
}
sound() {   # start | done | problem
  [ "$SOUNDS" = "yes" ] || return
  case "$1" in
    start)   f=/System/Library/Sounds/Tink.aiff ;;
    done)    f=/System/Library/Sounds/Glass.aiff ;;
    problem) f=/System/Library/Sounds/Basso.aiff ;;
  esac
  [ -f "$f" ] && (afplay "$f" >/dev/null 2>&1 &)
}

mkdir -p "$LOG_DIR" "$REPORT_DIR"
if [ "$MANUAL" = "yes" ]; then
  exec > >(tee -a "$LOG") 2>&1            # by hand: show output and log it
else
  exec >>"$LOG" 2>&1
  sleep 3                                 # let the drive finish mounting
fi

# ---- 1. detect --------------------------------------------------------------
[ -d "$IPOD/.rockbox" ] || { [ "$MANUAL" = "yes" ] && echo "iPod not mounted at $IPOD"; exit 0; }
if ! ls "$IPOD/.rockbox" >/dev/null 2>&1; then
  MSG="macOS blocked access to the iPod. Allow 'iPod Sync' in System Settings > Privacy & Security > Files and Folders (Removable Volumes)."
  echo "$MSG"; sound problem; notify "iPod Sync can't read the iPod" "$MSG"; exit 1
fi
if [ "${CHECK:-}" = "yes" ]; then     # used by install.sh to trigger macOS's permission prompt
  echo "OK: can read $IPOD"; notify "iPod Sync can read your iPod" "Automatic sync is ready"; exit 0
fi
mkdir "$LOCK" 2>/dev/null || { echo "already running"; exit 0; }
RUN=$(mktemp "${TMPDIR:-/tmp}/ipod-sync-run.XXXXXX")
export IPOD_SYNC_RUN="$RUN"
trap 'rmdir "$LOCK"; rm -f "$RUN"' EXIT

DRY_FLAG=(); [ "$DRY_RUN" = "yes" ] && DRY_FLAG=(--dry-run)
tools() { "$PYTHON" "$TOOLS" "$1" --ipod "$IPOD" --library "${SRC%/}" --data "$SCROBBLES" \
          --playlists "$PLAYLISTS" --tz "$IPOD_TZ" --rb-scrobbler "$RB_SCROBBLER" --log-dir "$LOG_DIR" "${DRY_FLAG[@]}" "${@:2}"; }

echo
echo "===== $(date '+%F %T') iPod mounted $( [ "$DRY_RUN" = "yes" ] && echo '(DRY RUN - nothing will change)')"
fail() { echo "$1"; sound problem; notify "iPod sync couldn't start" "$1"; exit 1; }
rsync --version 2>/dev/null | grep -q 'version 3' || fail "Homebrew rsync not found - run: brew install rsync"
[ -d "$SRC" ] || fail "Music Library folder not found: $SRC"
if [ ! -x "$PYTHON" ] || [ ! -f "$TOOLS" ]; then
  problem "Python helper missing - scrobbles, playlists and the report were skipped"
  DO_SCROBBLES="no"; DO_PLAYLISTS="no"
fi
sound start
if [ "$DRY_RUN" = "yes" ]; then notify "Dry run started…" "Nothing will be changed"
else notify "Syncing… don't unplug" "Collecting plays, copying music, updating playlists"; fi

# ---- 2. back up Rockbox settings (last 10 kept) ---------------------------
if [ "$DRY_RUN" != "yes" ]; then
  STAMP="$BACKUP/$(date +%F_%H%M)"
  mkdir -p "$STAMP"
  cp "$IPOD/.rockbox/config.cfg" "$STAMP/" 2>/dev/null
  [ -d "$IPOD/Playlists" ] && rsync -a "$IPOD/Playlists/" "$STAMP/Playlists/"
  ls -1d "$BACKUP"/*/ 2>/dev/null | sort -r | tail -n +$((KEEP_BACKUPS + 1)) | while IFS= read -r d; do rm -rf "$d"; done
  echo "Backup: settings and playlists saved to $STAMP"
fi

# ---- 3. collect plays (before the file sync, which may delete duplicates) --
if [ "$DO_SCROBBLES" = "yes" ]; then
  tools scrobble-collect || problem "Couldn't read the iPod's play log - plays left on the iPod for next time"
fi

# ---- 4. mirror the library onto /Music ------------------------------------
CHANGES="$LOG_DIR/ipod-sync-files.txt"     # full per-file list
: > "$CHANGES"
if [ "$DO_FILES" = "yes" ]; then
  # no --delete: rsync compares accented names byte-for-byte and would delete
  # files it should keep; the prune step below does deletions safely instead
  FLAGS=(-rt -8 --modify-window=2 --exclude='.*' --itemize-changes)
  [ "$DRY_RUN" = "yes" ] && FLAGS+=(--dry-run)
  [ "$DRY_RUN" = "yes" ] || mkdir -p "$DEST"
  if rsync "${FLAGS[@]}" "$SRC" "$DEST" > "$CHANGES"; then
    echo "Files: $(grep -c '^>f' "$CHANGES") to copy/update (list: $CHANGES)"
    if [ -x "$PYTHON" ]; then tools prune || problem "Removing old songs from the iPod was skipped (see log)"; fi
  else
    tail -20 "$CHANGES"; problem "Copying music to the iPod failed part-way (see log)"
  fi
fi

# ---- 5. playlists ----------------------------------------------------------
if [ "$DO_PLAYLISTS" = "yes" ]; then
  tools playlists-push || problem "Playlists weren't updated (see log)"
fi

# ---- 6. report anything outside /Music -------------------------------------
[ -x "$PYTHON" ] && tools report

# ---- 7. tidy, flush, eject -------------------------------------------------
EJECTED=()
if [ "$DRY_RUN" != "yes" ]; then
  # remove macOS '._' shadow files from the folders we manage
  N=$(find "$IPOD/Music" "$IPOD/Playlists" -name '._*' -type f 2>/dev/null | wc -l | tr -d ' ')
  find "$IPOD/Music" "$IPOD/Playlists" -name '._*' -type f -delete 2>/dev/null
  echo "Tidy: removed $N '._' files"
  sync
  if [ ${#PROBLEMS[@]} -eq 0 ] && [ "$EJECT" = "yes" ]; then
    if diskutil eject "$IPOD" >/dev/null; then
      EJECTED=(--ejected); echo "Ejected"
      notify "Safe to unplug" "Music synced - uploading plays to Last.fm…"
    else
      problem "Couldn't eject the iPod - eject it in Finder before unplugging"
    fi
  fi
fi

# ---- 8. upload plays to Last.fm (doesn't need the iPod) ---------------------
if [ "$DO_SCROBBLES" = "yes" ]; then
  tools scrobble-upload   # failures are recorded for the summary
fi

# ---- 9. summary notification + report page -----------------------------------
if [ -x "$PYTHON" ]; then
  PROB_TEXT=$(printf '%s\n' "${PROBLEMS[@]}")
  SUMMARY=$(tools summary --files-list "$CHANGES" --started "$STARTED" "${EJECTED[@]}" \
            --problems "$PROB_TEXT" --report-dir "$REPORT_DIR" --lastfm-user "$LASTFM_USER")
  STATUS=$(sed -n 1p <<<"$SUMMARY"); TITLE=$(sed -n 2p <<<"$SUMMARY"); DETAIL=$(sed -n 3p <<<"$SUMMARY")
else
  STATUS="error"; TITLE="iPod sync finished with problems"; DETAIL="${PROBLEMS[0]}"
fi
echo "Summary: $TITLE - $DETAIL"
case "$STATUS" in
  ok|dry) sound "done" ;;
  *)      sound problem ;;
esac
notify "$TITLE" "$DETAIL" open
if [ -f "$REPORT" ] && { [ "$OPEN_REPORT" = "always" ] || { [ "$OPEN_REPORT" = "problems" ] && { [ "$STATUS" = "warn" ] || [ "$STATUS" = "error" ]; }; }; }; then
  open "$REPORT"
fi
echo "===== $(date '+%F %T') done"
