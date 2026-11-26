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
MAC_TZ="$IPOD_TZ"
SETTINGS_ERROR=""
settings_error() { [ -n "$SETTINGS_ERROR" ] || SETTINGS_ERROR="$1"; }   # keep the first one
WARNINGS=()
warn() { WARNINGS+=("$1"); }
if [ -f "$CONFIG" ]; then
  # try the settings file in a throw-away shell first, so a typo (a missing
  # quote, a $NAME that isn't set) stops the sync instead of half-applying
  # shellcheck source=/dev/null
  SETTINGS_ERROR=$( (set -u; . "$CONFIG") 2>&1 >/dev/null ) || settings_error "$CONFIG can't be read"
  # shellcheck source=/dev/null
  [ -z "$SETTINGS_ERROR" ] && . "$CONFIG"
  KNOWN=" IPOD_NAME DRY_RUN SRC PLAYLISTS BACKUP SCROBBLES REPORT_DIR IPOD_TZ LOG DO_SCROBBLES DO_FILES
          DO_PLAYLISTS EJECT KEEP_BACKUPS SOUNDS OPEN_REPORT LASTFM_USER PYTHON RB_SCROBBLER "
  while IFS= read -r name; do
    case "$KNOWN" in *[[:space:]]"$name"[[:space:]]*) ;; *) warn "Unknown setting $name in $CONFIG (a typo?) - ignored" ;; esac
  done < <(sed -n 's/^[[:space:]]*\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$CONFIG")
fi
TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ipod_tools.py"

# ---- check the settings ---------------------------------------------------
yes_no() {   # yes_no NAME fallback - accept Yes/true/1/on and No/false/0/off
  local raw v
  eval "raw=\${$1}"
  v=$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')
  case "$v" in
    yes|y|true|1|on)  v="yes" ;;
    no|n|false|0|off) v="no" ;;
    *) warn "$1=\"$raw\" should be yes or no - using \"$2\""; v="$2" ;;
  esac
  eval "$1=\$v"
}
yes_no DRY_RUN yes                        # when unsure, change nothing
yes_no DO_SCROBBLES yes; yes_no DO_FILES yes; yes_no DO_PLAYLISTS yes; yes_no EJECT yes; yes_no SOUNDS yes
case "$KEEP_BACKUPS" in ''|*[!0-9]*|0)
  warn "KEEP_BACKUPS=\"$KEEP_BACKUPS\" isn't a number from 1 up - using 10"; KEEP_BACKUPS=10 ;;
esac
case "$OPEN_REPORT" in never|problems|always) ;; *)
  warn "OPEN_REPORT=\"$OPEN_REPORT\" should be never, problems or always - using problems"; OPEN_REPORT="problems" ;;
esac
if [ -z "$IPOD_TZ" ]; then
  IPOD_TZ="${MAC_TZ:-UTC}"
elif [ ! -f "/usr/share/zoneinfo/$IPOD_TZ" ]; then
  warn "IPOD_TZ=\"$IPOD_TZ\" isn't a time zone name (e.g. Europe/London) - using the Mac's, ${MAC_TZ:-UTC}"
  IPOD_TZ="${MAC_TZ:-UTC}"
fi
while [ "${SRC%/}" != "$SRC" ]; do SRC="${SRC%/}"; done      # trailing slashes are added back below
case "$SRC" in
  ""|"$HOME") settings_error "SRC=\"$SRC/\" would copy your whole disk or home folder - set it to your music library folder" ;;
  /*)         SRC="$SRC/" ;;              # the slash means "copy what's inside"
  *)          settings_error "SRC=\"$SRC\" must be a full path, e.g. \$HOME/Music/Music Library/" ;;
esac
case "$IPOD_NAME" in ""|.|..|*/*)
  settings_error "IPOD_NAME=\"$IPOD_NAME\" should be the iPod's name as shown in Finder" ;;
esac
# ---------------------------------------------------------------------------

# From here on, using a variable that was never set is an error (it's usually
# a typo). Note: bash 3.2 treats an empty list as "not set", hence the
# ${LIST[@]+"${LIST[@]}"} spelling below - it means "the list, if it has anything".
set -u

MANUAL="no"
case "${1:-}" in
  --dry-run) DRY_RUN="yes"; MANUAL="yes" ;;
  --real)    DRY_RUN="no";  MANUAL="yes" ;;
  --check)   CHECK="yes";   MANUAL="yes" ;;
  --export-playlists)   # one-off: copy Music.app playlists into $PLAYLISTS
    exec "$PYTHON" "$TOOLS" playlists-export --library "${SRC%/}" --playlists "$PLAYLISTS" \
         --data "$SCROBBLES" ${2+"${@:2}"} ;;
  -h|--help)
    echo "usage: ipod-sync [--dry-run | --real | --check | --export-playlists [--include-smart]]"
    echo "settings: $CONFIG    log: $LOG"; exit 0 ;;
esac

# IPOD_SYNC_PATH_PREFIX lets the tests put stand-in commands first
export PATH="${IPOD_SYNC_PATH_PREFIX:+$IPOD_SYNC_PATH_PREFIX:}/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
VOLS="${VOLUMES:-/Volumes}"
IPOD="$VOLS/$IPOD_NAME"                   # (may become "IPOD 1" - see detect)
LOCK="${IPOD_SYNC_LOCK:-/tmp/ipod-sync.lock}"
LOG_DIR="$(dirname "$LOG")"
REPORT="$REPORT_DIR/Last sync.html"
STARTED=$(date +%s)
PROBLEMS=()
problem() { PROBLEMS+=("$1"); echo "PROBLEM: $1"; }
ipod_here() { [ -d "$IPOD/.rockbox" ]; }
GONE=()
unplugged() {   # the iPod disappeared part-way (noted once)
  [ ${#GONE[@]} -gt 0 ] && return
  GONE=(--gone)
  problem "The iPod was unplugged during the sync - plug it back in to finish (nothing is lost)"
}

# notify "title" "message" [link]
#  uses terminal-notifier when installed (own icon in Notification settings,
#  banner replaces the previous one, click opens the link); else osascript.
#  The text is passed as arguments, never pasted into the script, so quotes
#  and backslashes in names can't break it.
notify() {
  if command -v terminal-notifier >/dev/null 2>&1; then
    local args=(-title "iPod" -subtitle "$1" -message "${2:- }" -group ipod-sync)
    [ -n "${3:-}" ] && args+=(-open "$3")
    terminal-notifier "${args[@]}" >/dev/null 2>&1 && return
  fi
  osascript -e 'on run argv' \
            -e 'display notification (item 2 of argv) with title "iPod" subtitle (item 1 of argv)' \
            -e 'end run' "$1" "${2:- }" >/dev/null 2>&1
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
# start a new log past ~2 MB, keeping one old copy (ipod-sync.log.1)
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG")" -gt 2000000 ]; then mv -f "$LOG" "$LOG.1"; fi
if [ "$MANUAL" = "yes" ]; then
  exec > >(tee -a "$LOG") 2>&1            # by hand: show output and log it
else
  exec >>"$LOG" 2>&1
  sleep "${IPOD_SYNC_MOUNT_DELAY:-3}"     # let the drive finish mounting
fi
if [ -n "$SETTINGS_ERROR" ]; then
  echo "Settings problem in $CONFIG: $SETTINGS_ERROR"
  sound problem; notify "iPod Sync settings need fixing" "$SETTINGS_ERROR"; exit 1
fi

# ---- 1. detect --------------------------------------------------------------
# Look for the iPod's name - or "IPOD 1", "IPOD 2"..., which is what macOS calls
# it when another drive already has that name. Only a drive with a .rockbox
# folder counts, and it must really be a mounted drive (not a leftover folder).
FOUND=(); BLOCKED=""
for v in "$VOLS/$IPOD_NAME" "$VOLS/$IPOD_NAME "[0-9]*; do
  [ -e "$v" ] || continue
  if ! ls "$v" >/dev/null 2>&1; then BLOCKED="$v"; continue; fi    # there, but we may not look inside
  [ -d "$v/.rockbox" ] || continue
  if [ "$(stat -f %d "$v")" = "$(stat -f %d "$VOLS")" ] && [ "${IPOD_SYNC_ALLOW_FOLDER:-}" != "yes" ]; then
    echo "$v is a folder on this Mac, not a mounted drive - ignored"; continue
  fi
  FOUND+=("$v")
done
if [ ${#FOUND[@]} -gt 1 ]; then
  MSG="${#FOUND[@]} drives called $IPOD_NAME with Rockbox are plugged in - unplug one and try again"
  echo "$MSG"; sound problem; notify "iPod sync didn't start" "$MSG"; exit 1
fi
if [ ${#FOUND[@]} -eq 0 ]; then
  if [ -n "$BLOCKED" ]; then
    MSG="macOS blocked access to the iPod. Allow 'iPod Sync' in System Settings > Privacy & Security > Files and Folders (Removable Volumes)."
    echo "$MSG"; sound problem; notify "iPod Sync can't read the iPod" "$MSG"; exit 1
  fi
  [ "$MANUAL" = "yes" ] && echo "iPod not mounted at $IPOD"
  exit 0
fi
IPOD="${FOUND[0]}"
DEST="$IPOD/Music/"
if [ "${CHECK:-}" = "yes" ]; then     # used by install.sh to trigger macOS's permission prompt
  echo "OK: can read $IPOD"; notify "iPod Sync can read your iPod" "Automatic sync is ready"; exit 0
fi
# one sync at a time: the lock is a folder holding the sync's process ID, so a
# lock left behind by a crash (or kill -9) is noticed instead of blocking forever
lock_is_stale() {
  local pid
  pid=$(cat "$LOCK/pid" 2>/dev/null)
  if [ -z "$pid" ]; then     # no PID: a sync that has only just started, or an older version's lock
    [ -n "$(find "$LOCK" -maxdepth 0 -mmin +2 2>/dev/null)" ]
    return
  fi
  ! ps -p "$pid" -o command= 2>/dev/null | grep -q 'ipod-sync'
}
if ! mkdir "$LOCK" 2>/dev/null; then
  if lock_is_stale; then
    echo "Taking over a lock left by a sync that didn't finish ($LOCK)"
    rm -rf "$LOCK"
    mkdir "$LOCK" 2>/dev/null || { echo "already running"; exit 0; }
  else
    echo "already running"; exit 0
  fi
fi
echo $$ > "$LOCK/pid"
RUN=$(mktemp "${TMPDIR:-/tmp}/ipod-sync-run.XXXXXX")
export IPOD_SYNC_RUN="$RUN"
trap 'rm -rf "$LOCK"; rm -f "$RUN"' EXIT

DRY_FLAG=(); [ "$DRY_RUN" = "yes" ] && DRY_FLAG=(--dry-run)
tools() { "$PYTHON" "$TOOLS" "$1" --ipod "$IPOD" --library "${SRC%/}" --data "$SCROBBLES" \
          --playlists "$PLAYLISTS" --tz "$IPOD_TZ" --rb-scrobbler "$RB_SCROBBLER" --log-dir "$LOG_DIR" ${DRY_FLAG[@]+"${DRY_FLAG[@]}"} ${2+"${@:2}"}; }

echo
echo "===== $(date '+%F %T') iPod mounted $( [ "$DRY_RUN" = "yes" ] && echo '(DRY RUN - nothing will change)')"
for w in ${WARNINGS[@]+"${WARNINGS[@]}"}; do echo "WARNING: $w"; done
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
  case "$BACKUP" in
    ""|/|"$HOME"|"$HOME/"|[!/]*)       # empty, the whole disk, your home folder, or not a full path
      problem "BACKUP=\"$BACKUP\" isn't a usable folder - Rockbox settings weren't backed up (set it to e.g. \$HOME/Music/RockboxBackup)" ;;
    *)
      STAMP="$BACKUP/$(date +%F_%H%M)"
      mkdir -p "$STAMP"
      cp "$IPOD/.rockbox/config.cfg" "$STAMP/" 2>/dev/null
      [ -d "$IPOD/Playlists" ] && rsync -a "$IPOD/Playlists/" "$STAMP/Playlists/"
      # delete old backups - only folders this script made (named like 2026-11-26_1735)
      find "$BACKUP" -mindepth 1 -maxdepth 1 -type d \
           -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9][0-9][0-9]' | sort -r |
        tail -n +$((KEEP_BACKUPS + 1)) | while IFS= read -r d; do rm -rf "$d"; done
      echo "Backup: settings and playlists saved to $STAMP" ;;
  esac
fi

# ---- 3. collect plays (before the file sync, which may delete duplicates) --
if [ "$DO_SCROBBLES" = "yes" ]; then
  tools scrobble-collect
  case $? in
    0) ;;
    3) problem "Plays were collected, but the iPod's play log couldn't be cleared (unplugged or read-only?) - they won't be counted twice" ;;
    *) problem "Couldn't read the iPod's play log - plays left on the iPod for next time" ;;
  esac
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
  COPY_ERRORS="$LOG_DIR/ipod-sync-copy-errors.txt"
  if rsync "${FLAGS[@]}" "$SRC" "$DEST" > "$CHANGES" 2> "$COPY_ERRORS"; then
    echo "Files: $(grep -c '^>f' "$CHANGES") to copy/update (list: $CHANGES)"
  else
    tail -20 "$CHANGES"; tail -20 "$COPY_ERRORS"
    if ! ipod_here; then
      unplugged
    elif grep -q 'No space left on device' "$COPY_ERRORS"; then
      problem "The iPod is full - some songs weren't copied. Remove music from your library (or make room on the iPod) and sync again"
    elif grep -q 'Read-only file system' "$COPY_ERRORS"; then
      problem "The iPod is read-only - nothing could be copied (run First Aid on it in Disk Utility)"
    else
      problem "Copying music to the iPod failed part-way (see log)"
    fi
  fi
  # remove songs that are no longer in the library - even after a failed copy
  # (on a full iPod that makes room for next time), but not once it's gone
  if [ -x "$PYTHON" ] && ipod_here; then
    tools prune || problem "Removing old songs from the iPod was skipped (see log)"
  fi
fi

# ---- 5. playlists ----------------------------------------------------------
if [ "$DO_PLAYLISTS" = "yes" ] && ipod_here; then
  tools playlists-push || problem "Playlists weren't updated (see log)"
fi

# ---- 6. report anything outside /Music -------------------------------------
[ -x "$PYTHON" ] && ipod_here && tools report

# ---- 7. tidy, flush, eject -------------------------------------------------
EJECTED=()
ipod_here || unplugged
if [ "$DRY_RUN" != "yes" ] && ipod_here; then
  # remove macOS '._' shadow files from the folders we manage
  N=$(find "$IPOD/Music" "$IPOD/Playlists" -name '._*' -type f 2>/dev/null | wc -l | tr -d ' ')
  find "$IPOD/Music" "$IPOD/Playlists" -name '._*' -type f -delete 2>/dev/null
  echo "Tidy: removed $N '._' files"
  # and half-copied files rsync leaves behind if a sync is interrupted
  # (".01 Song.mp3.a1B2c3"; the sync never copies hidden files, so these are safe to remove)
  N=$(find "$IPOD/Music" -name '.*.??????' -type f 2>/dev/null | wc -l | tr -d ' ')
  find "$IPOD/Music" -name '.*.??????' -type f -delete 2>/dev/null
  [ "$N" -gt 0 ] && echo "Tidy: removed $N half-copied files left by an interrupted sync"
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
  PROB_TEXT=$(printf '%s\n' ${PROBLEMS[@]+"${PROBLEMS[@]}"})
  WARN_TEXT=$(printf '%s\n' ${WARNINGS[@]+"${WARNINGS[@]}"})
  SUMMARY=$(tools summary --files-list "$CHANGES" --started "$STARTED" ${EJECTED[@]+"${EJECTED[@]}"} \
            ${GONE[@]+"${GONE[@]}"} --problems="$PROB_TEXT" --warnings="$WARN_TEXT" --report-dir "$REPORT_DIR" --lastfm-user "$LASTFM_USER")
  STATUS=$(sed -n 1p <<<"$SUMMARY"); TITLE=$(sed -n 2p <<<"$SUMMARY"); DETAIL=$(sed -n 3p <<<"$SUMMARY")
  REPORT_URL=$(sed -n 5p <<<"$SUMMARY")
fi
if [ -z "${TITLE:-}" ]; then              # no Python, or the summary step itself failed
  STATUS="error"; TITLE="iPod sync finished with problems"; REPORT_URL=""
  DETAIL="${PROBLEMS[0]:-The sync report could not be made - see ~/Library/Logs/ipod-sync.log}"
fi
echo "Summary: $TITLE - $DETAIL"
case "$STATUS" in
  ok|dry) sound "done" ;;
  *)      sound problem ;;
esac
notify "$TITLE" "$DETAIL" "$REPORT_URL"
if [ -f "$REPORT" ] && { [ "$OPEN_REPORT" = "always" ] || { [ "$OPEN_REPORT" = "problems" ] && { [ "$STATUS" = "warn" ] || [ "$STATUS" = "error" ]; }; }; }; then
  open "$REPORT"
fi
echo "===== $(date '+%F %T') done"
