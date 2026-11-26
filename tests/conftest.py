"""Shared test helpers.

SAFETY: every test runs inside a throw-away "sandbox" folder:
  * HOME is a temp folder, so ~/Music, ~/.config and ~/Library/Logs all point
    inside it - a test can't touch the real library, scrobble archive or log;
  * the settings file is a temp test.conf (IPOD_SYNC_CONFIG);
  * the fake iPod is called TESTPOD (never IPOD - the real sync job only reacts
    to /Volumes/IPOD);
  * rb-scrobbler is tests/fakes/rb-scrobbler, which never contacts Last.fm, and
    the real rb-scrobbler couldn't log in anyway because its key lives under HOME;
  * notification/sound/open/diskutil commands are stand-ins that only record
    their arguments, and `rm` refuses anything outside the sandbox.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(REPO, "bin")
SCRIPT = os.path.join(BIN, "ipod-sync.sh")
TOOLS = os.path.join(BIN, "ipod_tools.py")
FAKE_RB = os.path.join(REPO, "tests", "fakes", "rb-scrobbler")
REAL_HOME = os.path.realpath(os.path.expanduser("~"))
sys.path.insert(0, BIN)

STUBS = ["terminal-notifier", "osascript", "afplay", "open", "diskutil"]

STUB = r'''#!/bin/bash
# records its arguments; exit code from STUB_<NAME>_EXIT (default 0)
/usr/bin/python3 -c 'import json,sys; open(sys.argv[1],"a").write(json.dumps(sys.argv[2:])+"\n")' \
  "$SANDBOX_ROOT/calls.jsonl" "$(basename "$0")" "$@"
v="STUB_$(basename "$0" | tr 'a-z-' 'A-Z_')_EXIT"
exit "${!v:-0}"
'''

RM_GUARD = r'''#!/bin/bash
# refuse to delete anything outside the test sandbox
for a in "$@"; do
  case "$a" in
    -*) ;;
    "$SANDBOX_ROOT"/*) ;;
    *) echo "TEST GUARD: refused rm $a" >&2; echo "$a" >> "$SANDBOX_ROOT/rm-refused"; exit 1 ;;
  esac
done
exec /bin/rm "$@"
'''

RSYNC_GUARD = r'''#!/bin/bash
# real rsync, but only between folders inside the test sandbox (so a bad SRC
# setting can never copy the Mac's disk); extra roots in SANDBOX_EXTRA_ROOTS
for a in "$@"; do
  case "$a" in
    -*) ;;
    "$SANDBOX_ROOT"/*) ;;
    *) ok=""
       for r in ${SANDBOX_EXTRA_ROOTS:-}; do case "$a" in "$r"/*) ok=1 ;; esac; done
       [ -n "$ok" ] || { echo "TEST GUARD: refused rsync $a" >&2; echo "rsync $a" >> "$SANDBOX_ROOT/rm-refused"; exit 1; } ;;
  esac
done
exec "$REAL_RSYNC" "$@"
'''


def write_exe(path, text):
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, 0o755)


def make_mp3(path, artist="Artist", title="Title", album="Album", track="1", frames=30):
    """A tiny but valid MP3 (silent MPEG-1 Layer III frames) with ID3 tags."""
    from mutagen.easyid3 import EasyID3
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frame = b"\xff\xfb\x90\x64" + b"\x00" * 413          # 128 kbps, 44.1 kHz
    with open(path, "wb") as fh:
        fh.write(frame * frames)
    t = EasyID3()
    t.update({"artist": artist, "title": title, "album": album, "tracknumber": str(track)})
    t.save(path)


class Sandbox:
    def __init__(self, root):
        self.root = os.path.realpath(str(root))
        self.home = os.path.join(self.root, "home")
        self.volumes = os.path.join(self.root, "Volumes")
        self.stubs = os.path.join(self.root, "stubs")
        self.ipod_name = "TESTPOD"
        music = os.path.join(self.home, "Music")
        self.library = os.path.join(music, "Music Library")
        self.playlists = os.path.join(music, "Playlists")
        self.scrobbles = os.path.join(music, "Rockbox Scrobbles")
        self.backup = os.path.join(music, "RockboxBackup")
        self.report_dir = os.path.join(music, "iPod Sync")
        self.log = os.path.join(self.home, "Library", "Logs", "ipod-sync.log")
        self.conf = os.path.join(self.root, "test.conf")
        self.rb = FAKE_RB
        self.rb_key = os.path.join(self.home, "Library", "Application Support", "rb-scrobbler",
                                   "rb-scrobbler.key")
        for d in (self.home, self.volumes, self.stubs, self.library, self.playlists,
                  os.path.join(self.root, "tmp")):
            os.makedirs(d, exist_ok=True)
        for s in STUBS:
            write_exe(os.path.join(self.stubs, s), STUB)
        write_exe(os.path.join(self.stubs, "rm"), RM_GUARD)
        write_exe(os.path.join(self.stubs, "rsync"), RSYNC_GUARD)
        os.makedirs(os.path.dirname(self.rb_key))
        with open(self.rb_key, "w") as fh:
            fh.write("fake\n")
        self.env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": self.home,
            "TMPDIR": os.path.join(self.root, "tmp"),
            "LANG": "en_US.UTF-8",
            "IPOD_SYNC_CONFIG": self.conf,
            "IPOD_SYNC_PATH_PREFIX": self.stubs,
            "IPOD_SYNC_LOCK": os.path.join(self.root, "ipod-sync.lock"),
            "VOLUMES": self.volumes,
            "SANDBOX_ROOT": self.root,
            "IPOD_SYNC_MOUNT_DELAY": "0",
            "IPOD_SYNC_ALLOW_FOLDER": "yes",       # the fake iPod is a folder, not a mounted drive
            "REAL_RSYNC": real_rsync(),
            "FAKE_RB_RECORD": os.path.join(self.root, "lastfm-accepted.jsonl"),
        }
        self.settings = {}
        self.config()

    # ---- safety ----------------------------------------------------------
    def check_safe(self):
        assert self.home != REAL_HOME and not REAL_HOME.startswith(self.home)
        assert self.home.startswith(self.root)
        assert self.ipod_name.split(" ")[0] != "IPOD"
        rb = self.settings["RB_SCROBBLER"]
        assert rb == FAKE_RB or rb.startswith(self.root + "/"), "rb-scrobbler must be the fake"

    # ---- setup -------------------------------------------------------------
    @property
    def ipod(self):
        return os.path.join(self.volumes, self.ipod_name)

    def config(self, **kw):
        s = {
            "IPOD_NAME": self.ipod_name, "SRC": self.library + "/", "PLAYLISTS": self.playlists,
            "BACKUP": self.backup, "SCROBBLES": self.scrobbles, "REPORT_DIR": self.report_dir,
            "LOG": self.log, "IPOD_TZ": "Europe/London", "PYTHON": sys.executable,
            "RB_SCROBBLER": self.rb, "SOUNDS": "yes", "OPEN_REPORT": "problems",
        }
        s.update(self.settings)
        s.update(kw)
        self.settings = s
        self.check_safe()
        with open(self.conf, "w") as fh:
            for k, v in s.items():
                fh.write("%s=%s\n" % (k, shell_quote(v)))
        return self.conf

    def raw_config(self, text):
        """Write test.conf verbatim (for typo tests)."""
        with open(self.conf, "w") as fh:
            fh.write(text)

    def make_ipod(self, name=None):
        if name:
            self.ipod_name = name
            self.config(IPOD_NAME=name)
        os.makedirs(os.path.join(self.ipod, ".rockbox"), exist_ok=True)
        with open(os.path.join(self.ipod, ".rockbox", "config.cfg"), "w") as fh:
            fh.write("volume: -20\n")
        return self.ipod

    def add_track(self, rel, root=None, tags=None, **kw):
        path = os.path.join(root or self.library, rel)
        parts = rel.split("/")
        t = {"artist": parts[0], "album": parts[1] if len(parts) > 2 else "Album",
             "title": re.sub(r"^\d+[ ._-]+", "", os.path.splitext(parts[-1])[0])}
        t.update(tags or {})
        make_mp3(path, **t, **kw)
        return path

    def play_log(self, lines, name="playback.log"):
        p = os.path.join(self.ipod, ".rockbox", name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in lines))
        return p

    # ---- running -----------------------------------------------------------
    def run(self, *args, env=None, timeout=120, shell="/bin/bash"):
        self.check_safe()
        e = dict(self.env)
        e.update(env or {})
        r = subprocess.run([shell, SCRIPT, *args], env=e, cwd=self.root, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=timeout)
        logged = self.logtext() if os.path.exists(self.log) else ""
        bugs = [x for x in (r.stdout + logged).splitlines() if "ipod-sync.sh: line" in x and "unbound variable" in x]
        assert not bugs, bugs                     # set -u caught a bug in the script
        return r

    def tools(self, *args, env=None):
        self.check_safe()
        e = dict(self.env)
        e.update(env or {})
        return subprocess.run([sys.executable, TOOLS, *args], env=e, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)

    # ---- inspecting --------------------------------------------------------
    def calls(self, name=None):
        p = os.path.join(self.root, "calls.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            rows = [json.loads(line) for line in fh]
        return [r[1:] for r in rows if name is None or r[0] == name]

    def notifications(self):
        """(subtitle, message) of every notification sent."""
        out = []
        for a in self.calls("terminal-notifier"):
            out.append((a[a.index("-subtitle") + 1], a[a.index("-message") + 1]))
        return out

    def accepted(self):
        p = self.env["FAKE_RB_RECORD"]
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return [json.loads(line) for line in fh]

    def logtext(self):
        with open(self.log, encoding="utf-8", errors="replace") as fh:
            return fh.read()


def shell_quote(v):
    return "'" + str(v).replace("'", "'\\''") + "'"


def tree_hash(root, skip=(".fseventsd", ".Spotlight-V100", ".Trashes", ".TemporaryItems")):
    """{relative path: sha1} for every file under root."""
    out = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in skip]
        for f in files:
            p = os.path.join(d, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = hashlib.sha1(fh.read()).hexdigest()
    return out


@pytest.fixture
def sb(tmp_path):
    s = Sandbox(tmp_path)
    yield s
    refused = os.path.join(s.root, "rm-refused")
    assert not os.path.exists(refused), "script tried to touch files outside the sandbox: " + open(refused).read()


@pytest.fixture
def tools_mod():
    import ipod_tools
    return ipod_tools


def real_rsync():
    return shutil.which("rsync", path="/opt/homebrew/bin:/usr/local/bin") or "/usr/bin/rsync"


def have_rsync3():
    return "version 3" in subprocess.run([real_rsync(), "--version"], stdout=subprocess.PIPE,
                                         text=True).stdout
