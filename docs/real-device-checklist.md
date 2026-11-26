# Real-device checklist

A step-by-step check on a real Mac and iPod, after the automated tests pass. It's written for
switching from a **manual setup** (your own launchd job running `~/bin/ipod-sync.sh`, with Full Disk
Access for bash or Python) to the app. For a fresh install, skip the "manual setup" parts.

Tick each step before going on. If one fails, stop and look at `~/Library/Logs/ipod-sync.log`.

---

## 0. Before you start (iPod unplugged)

- [ ] Back up the old setup and your scrobble data (replace the date):

  ```bash
  B=~/ipod-sync-backup-2026-11-26; mkdir -p "$B"
  cp -Rp ~/bin ~/Library/LaunchAgents/*ipodsync*.plist "$HOME/Music/Rockbox Scrobbles" "$B"/
  ```

- [ ] Note what the old job is called: `launchctl list | grep -i ipod` (e.g. `com.example.ipodsync`).
- [ ] Nothing waiting to upload: `ls ~/Music/Rockbox\ Scrobbles/pending/` is empty (if not, that's
      fine – the new version picks it up).

## 1. Install

- [ ] `cd ipod-sync && ./install.sh`. Answer with your iPod's name, library folder, playlists folder
      and time zone (e.g. `Europe/London`).
- [ ] It says **Found an older iPod sync job: …** – answer **y**. Only that job should be listed;
      other launchd jobs are left alone.
- [ ] It says **Already connected to Last.fm** (it reuses `~/bin/rb-scrobbler` and your login).
- [ ] `launchctl list | grep -i ipod` shows only `com.github.tombarker97.ipod-sync`.
- [ ] `ls ~/.ipod-sync/old/` contains the old job.
- [ ] For a careful first run, set `DRY_RUN="yes"` in `~/.config/ipod-sync/config` – the next
      plug-in then changes nothing.

## 2. The permission prompt

- [ ] Plug in the iPod (don't touch its buttons). macOS asks:
      **"iPod Sync" would like to access files on a removable volume.**
      Check it names **iPod Sync** – not bash, Terminal or Python. Click **Allow**.
- [ ] **System Settings → Privacy & Security → Files and Folders → iPod Sync**: *Removable Volumes* is on.
- [ ] `open -a "iPod Sync" --args --check` shows **iPod Sync can read your iPod**.
      (Use this rather than `ipod-sync --check` in Terminal, which tests *Terminal's* access, not the app's.)
- [ ] *Optional – the "denied" path:* turn *Removable Volumes* off for iPod Sync, run the `open -a`
      command again, and check the notification says **iPod Sync can't read the iPod** (not "not mounted").
      Turn it back on.

## 3. Remove Full Disk Access from bash and Python

- [ ] **System Settings → Privacy & Security → Full Disk Access**: remove `bash`, `python3`/`Python`
      (and any `ipod-sync` entries) you added for the old setup.
- [ ] Unplug and plug in the iPod. The (dry-run) sync still runs: `tail -20 ~/Library/Logs/ipod-sync.log`
      shows `iPod mounted (DRY RUN - nothing will change)` and a summary, with no "blocked access".

## 4. A real sync on plug-in

- [ ] Set `DRY_RUN="no"`. Plug in the iPod.
- [ ] Notifications, in order: **Syncing… don't unplug** → **Safe to unplug** → a summary like
      *iPod synced – safe to unplug: +2 songs · 5 plays scrobbled*. Clicking it opens the report.
- [ ] The iPod is ejected (gone from Finder) before the summary.
- [ ] `~/Music/iPod Sync/Last sync.html` looks right: plays, albums added/removed, playlists, recent syncs.
- [ ] Plug in again: the summary says **Already up to date · no new plays** and `Files: 0 to copy/update`
      is in the log (nothing re-copied).

## 5. One real scrobble

- [ ] The iPod's clock is right (**Settings → General Settings → System → Time & Date**).
- [ ] Play one track all the way through (it must be longer than 30 seconds). Note the time.
- [ ] Plug in. The summary says **1 play scrobbled**.
- [ ] On last.fm, your recent tracks show it **once, at the time you listened** (not the sync time).
- [ ] Plug in again: no second copy appears on Last.fm.

## 6. Later

- [ ] After the next clock change (28 March 2027 in the UK), check the first sync's log for
      `Files: N to copy/update`. FAT cards store local times, so if N is your whole library, it was
      re-copied once because of the change – please open an issue.

## If you need to go back

```bash
./uninstall.sh --keep-settings
cp ~/ipod-sync-backup-2026-11-26/com.example.ipodsync.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.ipodsync.plist
```

Then give bash/Python Full Disk Access again. Your music, scrobble archive and backups are never
touched by uninstalling.
