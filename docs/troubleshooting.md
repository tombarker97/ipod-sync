# Troubleshooting

Start with the log – every sync writes to it:

```bash
tail -40 ~/Library/Logs/ipod-sync.log
```

---

### Nothing happens when I plug in the iPod

- Check the background job is running: `launchctl list | grep ipod-sync` should show a line with `com.github.tombarker97.ipod-sync`. If not, run `./install.sh` again.
- Check `IPOD_NAME` in `~/.config/ipod-sync/config` matches the iPod's name in Finder exactly.
- If the iPod doesn't appear in Finder at all, see *"USB keypad mode"* below.
- No notifications? Check **System Settings → Notifications → terminal-notifier** (or **Script Editor** if you didn't install terminal-notifier) is allowed, and that a Focus mode isn't hiding them. The sync still runs either way – check the report.

### "iPod Sync can't read the iPod"

macOS hasn't given the app permission to read external drives.

1. Go to **System Settings → Privacy & Security → Files and Folders → iPod Sync** and turn on **Removable Volumes**.
2. If iPod Sync isn't listed there, plug in the iPod and run `open -a "iPod Sync" --args --check` – macOS should ask, and you click **Allow**.
3. If no prompt ever appears, add `~/Applications/iPod Sync.app` to **Full Disk Access** instead. That's broader, but it applies to this app only.

Running `./install.sh` again doesn't rebuild the app unless it has changed, so macOS keeps the permission.

### The iPod shows "USB keypad mode" and doesn't appear in Finder

Rockbox connected as a USB keyboard instead of a drive.

- Plug in **without touching the click wheel or buttons** – holding a button while connecting means "just charge".
- Check **Settings → General Settings → System → USB Mode** is **Mass Storage**.
- If the disk-activity icon in the corner has been on for a long time, Rockbox is stuck writing its database and can't hand the drive over – see the next section.

### "Database not ready", or the database never finishes / shows old artists

Rockbox's database is separate from your files, and it can get stuck – especially on iPods with SD-card adapters, or if the iPod switched off mid-rebuild. To start clean, on the iPod:

1. Restart it: hold **Menu + Select**.
2. **Settings → General Settings → Database**: set **Auto Update** and **Load to RAM** to **No**.
3. **Settings → General Settings → File View → Show Files → All**, then in **Files → .rockbox**, delete every file starting `database_` and ending `.tcd`. Keep `database_changelog.txt` (your saved play counts).
4. Turn off **Idle Poweroff** (so it can't switch off halfway), then **Database → Initialize Now**, and leave it alone on a charger until it finishes.

If it still won't build, browse by **Files** instead – your library is already organised as Artist/Album, and playlists work without the database.

After a big retagging session, run **Initialize Now** again – **Update Now** finds new and deleted files but doesn't reliably re-read changed tags.

### My plays aren't on Last.fm

- Scrobbles are filed **at the time you listened**, so look in your Last.fm history for that day, not at the top.
- Open the report (`~/Music/iPod Sync/Last sync.html`) – it lists what was sent and how many plays were held back.
- **Held back** means older than 14 days (Last.fm won't take them) or a date that can't be right (the iPod's clock wasn't set). They're kept in `~/Music/Rockbox Scrobbles/unsubmittable/`.
- Check **Settings → Playback Settings → Logging** is on.
- Upload failed? Plays wait in `~/Music/Rockbox Scrobbles/pending/` and retry next sync. If it says you're not connected, run `~/.ipod-sync/rb-scrobbler -auth`.

### Times on Last.fm are an hour (or more) out

Set the iPod's clock (**Settings → General Settings → System → Time & Date**) and make sure `IPOD_TZ` in your config is the time zone that clock is set to, e.g. `Europe/London`. Clock changes (summer/winter time) are handled automatically.

### "Removing old songs from the iPod was skipped"

As a safety net, the sync refuses to delete more than half of what's on the iPod in one go – that usually means a setting is wrong (e.g. `SRC` points at the wrong folder). Check the list of what it wanted to delete:

```bash
open ~/Library/Logs/ipod-sync-prune.txt
```

If the list is right (for example the iPod is full of an old copy of your library in different folder names), delete those files yourself in Finder, or run this once:

```bash
~/.ipod-sync/venv/bin/python ~/.ipod-sync/ipod_tools.py prune --ipod /Volumes/IPOD \
  --library ~/"Music/Music Library" --force
```

### Songs with accented names (Björk, Böhmer…)

macOS and the iPod's card can store accented letters in two different ways. That's why the sync removes songs with its own name-aware check rather than rsync's `--delete`, which would otherwise delete and re-copy those songs on every sync. Nothing to do – just don't add `--delete` back.

### Lots of `._` files on the iPod

macOS creates hidden `._` files on non-Apple drives. The sync deletes them from `/Music` and `/Playlists` every time. To stop Finder adding `.DS_Store` files to USB drives as well:

```bash
defaults write com.apple.desktopservices DSDontWriteUSBStores -bool true
```

### Still stuck?

Run a practice sync and look at the output:

```bash
ipod-sync --dry-run
```

Then [open an issue](https://github.com/tombarker97/ipod-sync/issues) with that output, your iPod model and your Rockbox version.
