#!/bin/bash
# shellcheck disable=SC1090,SC2088
# install.sh - set up iPod Sync on this Mac.
# Safe to re-run: it's also how you update (git pull && ./install.sh).
#   ./install.sh          ask a few questions
#   ./install.sh --yes    accept the suggested answers
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.github.tombarker97.ipod-sync"
HOME_DIR="$HOME/.ipod-sync"                       # scripts, Python env, rb-scrobbler
CONF_DIR="$HOME/.config/ipod-sync"
CONF="$CONF_DIR/config"
APP="$HOME/Applications/iPod Sync.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SHIM="$HOME/.local/bin/ipod-sync"
RB_KEY="$HOME/Library/Application Support/rb-scrobbler/rb-scrobbler.key"
YES="no"; [ "${1:-}" = "--yes" ] && YES="yes"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
note() { printf '  - %s\n' "$*"; }
die()  { printf '  \033[31m✗ %s\033[0m\n' "$*"; exit 1; }
ask() {  # ask "question" "default"  ->  $REPLY
  if [ "$YES" = "yes" ]; then REPLY="$2"; return; fi
  read -r -p "  $1 [$2]: " REPLY </dev/tty || true
  REPLY="${REPLY:-$2}"
}
yes_no() { ask "$1 (y/n)" "$2"; case "$REPLY" in y|Y|yes) return 0 ;; *) return 1 ;; esac; }

[ "$(uname)" = "Darwin" ] || die "iPod Sync runs on macOS only."

# ---------------------------------------------------------------------------
bold "1/6  Checking what's needed"
xcode-select -p >/dev/null 2>&1 \
  || die "Apple's Command Line Tools are needed. Run:  xcode-select --install  then run this installer again."
ok "Command Line Tools"
command -v brew >/dev/null 2>&1 || die "Homebrew is needed - install it from https://brew.sh, then run this installer again."
ok "Homebrew"
BREW_PREFIX="$(brew --prefix)"
if ! "$BREW_PREFIX/bin/rsync" --version 2>/dev/null | grep -q 'version 3'; then
  note "Installing rsync (the copy built into macOS is too old)"
  brew install rsync
fi
ok "rsync"
if ! command -v terminal-notifier >/dev/null 2>&1; then
  if yes_no "Install terminal-notifier? Gives clickable notifications that open the sync report" "y"; then
    brew install terminal-notifier
  fi
fi
command -v terminal-notifier >/dev/null 2>&1 && ok "terminal-notifier" || note "Using macOS's built-in notifications"

PY="/usr/bin/python3"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null \
  || die "Python 3.9 or newer is needed (it comes with the Command Line Tools)."
ok "Python $("$PY" -c 'import platform; print(platform.python_version())')"

# ---------------------------------------------------------------------------
bold "2/6  Installing the sync scripts"
mkdir -p "$HOME_DIR"
install -m 755 "$REPO/bin/ipod-sync.sh" "$HOME_DIR/ipod-sync.sh"
install -m 644 "$REPO/bin/ipod_tools.py" "$HOME_DIR/ipod_tools.py"
ok "Scripts in $HOME_DIR"
if [ ! -x "$HOME_DIR/venv/bin/python" ]; then
  "$PY" -m venv "$HOME_DIR/venv"
fi
"$HOME_DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade mutagen
ok "Python environment (mutagen, for reading tags)"
mkdir -p "$(dirname "$SHIM")"
printf '#!/bin/bash\nexec "$HOME/.ipod-sync/ipod-sync.sh" "$@"\n' > "$SHIM"
chmod 755 "$SHIM"
ok "Command: ipod-sync  (in ~/.local/bin)"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *)
  note "~/.local/bin isn't on your PATH. To use the 'ipod-sync' command, run:"
  note "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc" ;;
esac

# ---------------------------------------------------------------------------
bold "3/6  Your settings"
if [ -f "$CONF" ]; then
  ok "Keeping your existing settings: $CONF"
else
  DETECTED=""
  for v in /Volumes/*; do [ -d "$v/.rockbox" ] && DETECTED="$(basename "$v")" && break; done
  ask "iPod's name, as shown in Finder" "${DETECTED:-IPOD}";               C_NAME="$REPLY"
  ask "Music library folder (copied to the iPod)" "$HOME/Music/Music Library"; C_SRC="${REPLY%/}/"
  ask "Playlists folder (.m3u8 files)" "$HOME/Music/Playlists";           C_PL="${REPLY%/}"
  SYS_TZ="$(readlink /etc/localtime | sed 's|.*zoneinfo/||')"
  ask "Time zone the iPod's clock is set to" "${SYS_TZ:-UTC}";            C_TZ="$REPLY"
  ask "Last.fm username (optional, for a link in the report)" "";         C_LFM="$REPLY"
  mkdir -p "$CONF_DIR"
  cat > "$CONF" <<EOF
# iPod Sync settings - https://github.com/tombarker97/ipod-sync
# Edit freely; re-running install.sh never overwrites this file.

# The iPod's name as shown in Finder
IPOD_NAME="$C_NAME"
# Your music library, copied to the iPod's /Music (keep the trailing /)
SRC="$C_SRC"
# Folder of .m3u8 playlists to put on the iPod
PLAYLISTS="$C_PL"
# Time zone the iPod's clock is set to
IPOD_TZ="$C_TZ"
# Optional: your Last.fm username, adds a link to the sync report
LASTFM_USER="$C_LFM"

# What each sync does ("yes"/"no"). DRY_RUN="yes" only logs what would happen.
DRY_RUN="no"
DO_SCROBBLES="yes"
DO_FILES="yes"
DO_PLAYLISTS="yes"
EJECT="yes"

# Feedback. OPEN_REPORT: never | problems | always
SOUNDS="yes"
OPEN_REPORT="problems"

# Where things are kept on this Mac
BACKUP="\$HOME/Music/RockboxBackup"
KEEP_BACKUPS=10
SCROBBLES="\$HOME/Music/Rockbox Scrobbles"
REPORT_DIR="\$HOME/Music/iPod Sync"
EOF
  ok "Saved to $CONF"
fi
[ -d "$(. "$CONF"; echo "$SRC")" ] || note "Note: your library folder doesn't exist yet - check SRC in $CONF"

# ---------------------------------------------------------------------------
bold "3b/6  Last.fm scrobbling (rb-scrobbler)"
RB="$HOME_DIR/rb-scrobbler"
for c in "$RB" "$HOME/bin/rb-scrobbler" "$(command -v rb-scrobbler || true)"; do
  if [ -n "$c" ] && [ -x "$c" ] && [ "$c" != "$RB" ]; then ln -sf "$c" "$RB"; break; fi
done
if [ ! -x "$RB" ]; then
  ARCH="$(uname -m)"; [ "$ARCH" = "x86_64" ] && ARCH="amd64"
  URL="https://github.com/Jeselnik/rb-scrobbler/releases/latest/download/rb-scrobbler-darwin-$ARCH"
  if yes_no "Download rb-scrobbler (uploads plays to Last.fm)?" "y"; then
    if curl -fsSL -o "$RB" "$URL"; then
      chmod 755 "$RB"; xattr -d com.apple.quarantine "$RB" 2>/dev/null || true
    else
      rm -f "$RB"
      note "Couldn't download it automatically. Get 'rb-scrobbler-darwin-$ARCH' from"
      note "https://github.com/Jeselnik/rb-scrobbler/releases, save it as $RB, then re-run this installer."
    fi
  fi
fi
if [ -x "$RB" ]; then
  ok "rb-scrobbler"
  if [ -f "$RB_KEY" ]; then
    ok "Already connected to Last.fm"
  elif yes_no "Connect to Last.fm now? (opens a link to approve; your password is never handled)" "y"; then
    "$RB" -auth || true      # it complains about a missing -f afterwards; that's expected
    [ -f "$RB_KEY" ] && ok "Connected to Last.fm" || note "Not connected - run: $RB -auth"
  fi
else
  note "Skipped - plays will be collected and queued until rb-scrobbler is installed"
fi

# ---------------------------------------------------------------------------
bold "4/6  Building the iPod Sync app"
SIG="$(cat "$REPO/app/launcher.c" "$REPO/app/Info.plist" | shasum -a 256 | cut -d' ' -f1)"
if [ -x "$APP/Contents/MacOS/iPod Sync" ] && [ "$(cat "$HOME_DIR/app.sha" 2>/dev/null)" = "$SIG" ]; then
  ok "Up to date (not rebuilt, so macOS keeps its permission)"
else
  rm -rf "$APP"
  mkdir -p "$APP/Contents/MacOS"
  cp "$REPO/app/Info.plist" "$APP/Contents/Info.plist"
  clang -O2 -Wall -o "$APP/Contents/MacOS/iPod Sync" "$REPO/app/launcher.c"
  codesign --force --sign - --identifier "$LABEL" "$APP" >/dev/null 2>&1
  echo "$SIG" > "$HOME_DIR/app.sha"
  ok "Built $APP"
fi

# ---------------------------------------------------------------------------
bold "5/6  Starting automatic sync"
UID_NUM="$(id -u)"
for old in "$HOME"/Library/LaunchAgents/*.plist; do
  [ -f "$old" ] && [ "$old" != "$PLIST" ] && grep -q "ipod-sync.sh" "$old" 2>/dev/null || continue
  note "Found an older iPod sync job: $(basename "$old")"
  if yes_no "Turn it off so the two don't both run?" "y"; then
    launchctl bootout "gui/$UID_NUM" "$old" 2>/dev/null || true
    mkdir -p "$HOME_DIR/old"; mv "$old" "$HOME_DIR/old/"
    ok "Moved to $HOME_DIR/old/"
  fi
done
mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$APP/Contents/MacOS/iPod Sync</string></array>
  <key>StartOnMount</key><true/>
  <key>AssociatedBundleIdentifiers</key><string>$LABEL</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
ok "Runs whenever a drive is mounted (does nothing unless it's your iPod)"

# ---------------------------------------------------------------------------
bold "6/6  Permission to read the iPod"
IPOD_VOL="/Volumes/$(. "$CONF"; echo "$IPOD_NAME")"
if [ -d "$IPOD_VOL/.rockbox" ]; then
  note "Your iPod is plugged in - asking macOS for permission now."
  note "If you see \"iPod Sync would like to access files on a removable volume\", click Allow."
  open -W -a "$APP" --args --check || true
  ok "Done - look for an 'iPod Sync can read your iPod' notification"
else
  note "Next time you plug in the iPod, macOS will ask:"
  note "\"iPod Sync would like to access files on a removable volume\" - click Allow."
fi

bold "All set"
cat <<EOF
  Plug in your iPod and it syncs by itself: notification when it's safe to unplug,
  report at ~/Music/iPod Sync/Last sync.html.

  Try a practice run first (changes nothing):   ipod-sync --dry-run
  Settings:                                     $CONF
  Log:                                          ~/Library/Logs/ipod-sync.log

  If you previously gave bash or Python "Full Disk Access" for an older version of this
  sync, you can now remove them in System Settings > Privacy & Security > Full Disk Access.
EOF
