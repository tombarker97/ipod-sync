# Running the tests

```bash
brew install shellcheck rsync
/usr/bin/python3 -m venv .venv
.venv/bin/pip install -r tests/requirements.txt
.venv/bin/pytest            # everything, ~3 minutes
.venv/bin/pytest -m "not fat32"   # skip the disk-image tests
shellcheck bin/ipod-sync.sh install.sh uninstall.sh
.venv/bin/ruff check bin tests
```

Use macOS's own `/usr/bin/python3` and `/bin/bash` (3.2) – that's what people running the sync have.

## They can't touch your real music or Last.fm

Every test runs in a throw-away sandbox (see `tests/conftest.py`):

- `HOME` is a temp folder, so `~/Music`, `~/.config` and `~/Library/Logs` are all inside it.
- The fake iPod is called **TESTPOD** – never `IPOD`, which a real sync job would react to. The
  FAT32 tests mount their disk image inside the sandbox, and refuse to run if `/Volumes/IPOD` is
  mounted.
- `rb-scrobbler` is `tests/fakes/rb-scrobbler`, which never contacts Last.fm. It copies the real
  tool's output and exit codes and records what it "accepted". The real rb-scrobbler couldn't log in
  anyway: its key lives under `HOME`.
- Notifications, sounds, `open` and `diskutil` are stand-ins that only record their arguments.
- `rm` and `rsync` are wrapped: anything outside the sandbox is refused and fails the test.

## What's where

| File | Covers |
|---|---|
| `test_tools_unit.py` | play-log parsing, the Last.fm rules, clock changes, the ledger, renamed files, prune, playlists, the report |
| `test_upload.py` | uploads that fail, partly fail, are rejected, hang – never a duplicate |
| `test_sync_folder.py` | whole syncs against a fake iPod folder; a dry run changes nothing |
| `test_sync_fat32.py` | whole syncs onto a FAT32 disk image: odd names, full, read-only, unplugged |
| `test_config.py`, `test_detect.py`, `test_lock.py`, `test_backup.py` | settings mistakes, finding the iPod, the lock, backups |
| `test_install.py` | install, re-install, migrating a manual setup, uninstall, the launcher |
