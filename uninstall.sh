#!/bin/bash
# uninstall.sh - remove iPod Sync from this Mac.
# Your music, playlists, scrobble archive, backups and reports are NOT touched.
#   ./uninstall.sh                     asks whether to delete your settings too
#   ./uninstall.sh --keep-settings     or --delete-settings: don't ask
set -uo pipefail

LABEL="com.github.tombarker97.ipod-sync"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP="$HOME/Applications/iPod Sync.app"
HOME_DIR="$HOME/.ipod-sync"
CONF_DIR="$HOME/.config/ipod-sync"

ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
yes_no() {   # no terminal to ask in = "no"
  local r=""
  read -r -p "  $1 (y/n) [n]: " r </dev/tty 2>/dev/null || true
  [ "$r" = "y" ] || [ "$r" = "Y" ]
}

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
rm -f "$PLIST";               ok "Automatic sync turned off"
rm -rf "$APP";                ok "Removed $APP"
rm -f "$HOME/.local/bin/ipod-sync"
rm -rf "$HOME_DIR";           ok "Removed scripts and Python environment ($HOME_DIR)"
case "${1:-}" in
  --delete-settings) DELETE_SETTINGS="yes" ;;
  --keep-settings)   DELETE_SETTINGS="no" ;;
  *) DELETE_SETTINGS="no"; yes_no "Also delete your settings ($CONF_DIR)?" && DELETE_SETTINGS="yes" ;;
esac
if [ "$DELETE_SETTINGS" = "yes" ]; then rm -rf "$CONF_DIR"; ok "Settings removed"; else ok "Settings kept: $CONF_DIR"; fi

cat <<'EOF'

  Kept (delete by hand if you want):
    ~/Music/Rockbox Scrobbles   play-log archive and anything not yet sent to Last.fm
    ~/Music/RockboxBackup       Rockbox settings + playlist backups
    ~/Music/iPod Sync           sync reports
    ~/Library/Logs/ipod-sync*.  logs
    ~/Library/Application Support/rb-scrobbler   your Last.fm login for rb-scrobbler

  You can also remove "iPod Sync" from System Settings > Privacy & Security > Files and Folders.
EOF
