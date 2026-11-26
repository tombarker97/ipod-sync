"""install.sh / uninstall.sh / the launcher app, in a sandboxed HOME.

brew, xcode-select, launchctl, codesign, open, curl and Python are stand-ins
that only record what they were asked to do. clang is real (the app is built
into the sandbox). Nothing touches the real launchd, ~/Applications or Python.
"""
import os
import plistlib
import subprocess
import sys

import pytest

import ipod_tools as T
from conftest import FAKE_RB, REPO, have_rsync3, real_rsync, write_exe
from test_tools_unit import wall_now

LABEL = "com.github.tombarker97.ipod-sync"

RECORD = '/usr/bin/python3 -c \'import json,sys; open(sys.argv[1],"a").write(json.dumps(sys.argv[2:])+"\\n")\' ' \
         '"$SANDBOX_ROOT/calls.jsonl" "$(basename "$0")" "$@"\n'

STUBS = {
    "xcode-select": "#!/bin/bash\n" + RECORD + "echo /Library/Developer/CommandLineTools\n",
    "brew": "#!/bin/bash\n" + RECORD + '[ "$1" = --prefix ] && echo "$SANDBOX_ROOT/brew"\nexit 0\n',
    "codesign": "#!/bin/bash\n" + RECORD + 'exit "${STUB_CODESIGN_EXIT:-0}"\n',
    "open": "#!/bin/bash\n" + RECORD,
    # launchctl: optionally fail the first N "bootstrap"s (launchd is sometimes slow to let go)
    "launchctl": "#!/bin/bash\n" + RECORD + '''if [ "$1" = bootstrap ] && [ -n "${STUB_BOOTSTRAP_FAILS:-}" ]; then
  n=$(cat "$SANDBOX_ROOT/bootstraps" 2>/dev/null || echo 0); echo $((n + 1)) > "$SANDBOX_ROOT/bootstraps"
  [ "$n" -lt "$STUB_BOOTSTRAP_FAILS" ] && { echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; }
fi
exit 0
''',
    # curl: "downloads" the fake rb-scrobbler
    "curl": "#!/bin/bash\n" + RECORD + 'while [ $# -gt 0 ]; do [ "$1" = -o ] && cp "%s" "$2"; shift; done\n' % FAKE_RB,
    # Python: real, except "-m venv" makes a stand-in venv and "-m pip" does nothing
    "python3": "#!/bin/bash\n" + RECORD + '''if [ "$1 $2" = "-m venv" ]; then mkdir -p "$3/bin"; cp "$0" "$3/bin/python"; exit 0; fi
[ "$1 $2" = "-m pip" ] && exit 0
exec "%s" "$@"
''' % sys.executable,
}


@pytest.fixture
def inst(sb):
    for name, text in STUBS.items():
        write_exe(os.path.join(sb.stubs, name), text)
    os.makedirs(os.path.join(sb.root, "brew", "bin"))
    os.symlink(real_rsync(), os.path.join(sb.root, "brew", "bin", "rsync"))
    sb.agents = os.path.join(sb.home, "Library", "LaunchAgents")
    sb.app = os.path.join(sb.home, "Applications", "iPod Sync.app")
    sb.user_conf = os.path.join(sb.home, ".config", "ipod-sync", "config")
    return sb


def run_installer(sb, script="install.sh", *args, env=None):
    sb.check_safe()
    e = {"HOME": sb.home, "PATH": sb.stubs + ":/usr/bin:/bin:/usr/sbin:/sbin", "SANDBOX_ROOT": sb.root,
         "IPOD_SYNC_PY": os.path.join(sb.stubs, "python3"), "VOLUMES": sb.volumes, "TMPDIR": sb.env["TMPDIR"],
         "LANG": "en_US.UTF-8"}
    e.update(env or {})
    return subprocess.run(["/bin/bash", os.path.join(REPO, script), *args], env=e, cwd=sb.root,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          stdin=subprocess.DEVNULL, start_new_session=True, timeout=120)


def launchctl(sb):
    return [c for c in sb.calls("launchctl")]


def test_fresh_install(inst):
    sb = inst
    sb.make_ipod()
    os.remove(sb.rb_key)                          # not connected to Last.fm yet
    r = run_installer(sb, "install.sh", "--yes")
    assert r.returncode == 0, r.stdout
    assert [c for c in sb.calls("brew") if c != ["--prefix"]] == [], "installed something it didn't need"
    assert "Installing rsync" not in r.stdout
    assert "Connected to Last.fm" in r.stdout
    home = os.path.join(sb.home, ".ipod-sync")
    assert os.access(os.path.join(home, "ipod-sync.sh"), os.X_OK)
    assert os.path.exists(os.path.join(home, "ipod_tools.py"))
    assert os.access(os.path.join(sb.app, "Contents", "MacOS", "iPod Sync"), os.X_OK)
    assert os.access(os.path.join(sb.home, ".local", "bin", "ipod-sync"), os.X_OK)
    # settings: the detected iPod and sensible defaults
    conf = open(sb.user_conf).read()
    assert 'IPOD_NAME="TESTPOD"' in conf and 'DRY_RUN="no"' in conf
    # the background job runs the app, on every mount
    plist = plistlib.load(open(os.path.join(sb.agents, LABEL + ".plist"), "rb"))
    assert plist["ProgramArguments"] == [os.path.join(sb.app, "Contents", "MacOS", "iPod Sync")]
    assert plist["StartOnMount"] is True
    assert ["bootstrap", "gui/%d" % os.getuid(), os.path.join(sb.agents, LABEL + ".plist")] in launchctl(sb)
    # rb-scrobbler "downloaded" and connected; permission prompt triggered via the app
    assert os.path.exists(sb.rb_key)
    assert ["-W", "-a", sb.app, "--args", "--check"] in sb.calls("open")
    assert ["--force", "--sign", "-", "--identifier", LABEL, sb.app] in sb.calls("codesign")


def test_rerun_keeps_settings_and_doesnt_rebuild_the_app(inst):
    sb = inst
    assert run_installer(sb, "install.sh", "--yes").returncode == 0
    with open(sb.user_conf, "a") as fh:
        fh.write('LASTFM_USER="someone"\n')
    conf = open(sb.user_conf).read()
    exe = os.path.join(sb.app, "Contents", "MacOS", "iPod Sync")
    st = os.stat(exe)
    signs = len(sb.calls("codesign"))
    r = run_installer(sb, "install.sh", "--yes")
    assert r.returncode == 0, r.stdout
    assert open(sb.user_conf).read() == conf
    assert (os.stat(exe).st_ino, os.stat(exe).st_mtime) == (st.st_ino, st.st_mtime)
    assert len(sb.calls("codesign")) == signs
    assert "not rebuilt" in r.stdout
    # but a changed launcher is rebuilt
    with open(os.path.join(sb.home, ".ipod-sync", "app.sha"), "w") as fh:
        fh.write("something else\n")
    run_installer(sb, "install.sh", "--yes")
    assert len(sb.calls("codesign")) == signs + 1


def test_launchd_slow_to_let_go(inst):
    r = run_installer(inst, "install.sh", "--yes", env={"STUB_BOOTSTRAP_FAILS": "1"})
    assert r.returncode == 0, r.stdout


def test_codesign_failure_is_explained(inst):
    r = run_installer(inst, "install.sh", "--yes", env={"STUB_CODESIGN_EXIT": "1"})
    assert r.returncode != 0
    assert "sign" in r.stdout.lower().split("building the ipod sync app")[-1]


OLD_AGENT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.example.ipodsync</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>%s/bin/ipod-sync.sh</string></array>
  <key>StartOnMount</key><true/>
</dict></plist>
"""
NIGHTLY = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>com.example.sync-music</string>
  <key>ProgramArguments</key><array><string>%s/.local/bin/sync-music.sh</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>2</integer></dict>
</dict></plist>
"""


@pytest.mark.skipif(not have_rsync3(), reason="needs Homebrew rsync 3")
def test_migrating_a_manual_setup(inst):
    """Someone who ran ~/bin/ipod-sync.sh from their own launchd job switches to the app."""
    sb = inst
    os.makedirs(sb.agents)
    old = os.path.join(sb.agents, "com.example.ipodsync.plist")
    nightly = os.path.join(sb.agents, "com.example.sync-music.plist")
    open(old, "w").write(OLD_AGENT % sb.home)
    open(nightly, "w").write(NIGHTLY % sb.home)
    os.makedirs(os.path.join(sb.home, "bin"))
    my_rb = os.path.join(sb.home, "bin", "rb-scrobbler")
    write_exe(my_rb, open(FAKE_RB).read())                    # already connected (key exists)
    # their existing data: a play log that was archived and is in the ledger,
    # but is somehow still on the iPod, plus old 2010-dated ledger lines
    sb.make_ipod()
    sb.add_track("A/B/01 a.mp3")
    line = "%d:200000:200000:/<HDD0>/Music/A/B/01 a.mp3" % (wall_now() - 3600)
    sb.play_log([line])
    for sub in ("raw", "sent", "pending", "unsubmittable"):
        os.makedirs(os.path.join(sb.scrobbles, sub))
    with open(os.path.join(sb.scrobbles, "ledger.txt"), "w") as fh:
        fh.write("1284636588:109349:109400:/music/balance and composure/with you.mp3\n")
        fh.write(line.replace("/<HDD0>/Music/A/B/01 a.mp3", T.nkey("/Music/A/B/01 a.mp3")) + "\n")

    r = run_installer(sb, "install.sh", "--yes")
    assert r.returncode == 0, r.stdout
    assert ["bootout", "gui/%d" % os.getuid(), old] in launchctl(sb)
    assert not os.path.exists(old)
    assert os.path.exists(os.path.join(sb.home, ".ipod-sync", "old", "com.example.ipodsync.plist"))
    assert os.path.exists(nightly)                             # unrelated job left alone
    assert os.readlink(os.path.join(sb.home, ".ipod-sync", "rb-scrobbler")) == my_rb
    assert "Already connected to Last.fm" in r.stdout
    assert sb.calls("curl") == []                              # nothing downloaded

    # the first sync with the new setup sends nothing to Last.fm a second time
    env = dict(sb.env)
    env.pop("IPOD_SYNC_CONFIG")                                # use the installed settings
    r = subprocess.run(["/bin/bash", os.path.join(sb.home, ".ipod-sync", "ipod-sync.sh"), "--real"],
                       env=env, cwd=sb.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert "1 already processed" in r.stdout, r.stdout
    assert sb.accepted() == []
    assert os.path.exists(os.path.join(sb.ipod, "Music", "A", "B", "01 a.mp3"))


def test_uninstall_without_a_terminal(inst):
    sb = inst
    assert run_installer(sb, "install.sh", "--yes").returncode == 0
    os.makedirs(sb.library, exist_ok=True)
    r = run_installer(sb, "uninstall.sh")
    assert r.returncode == 0, r.stdout
    assert "Kept (delete by hand" in r.stdout
    assert not os.path.exists(sb.app)
    assert not os.path.exists(os.path.join(sb.agents, LABEL + ".plist"))
    assert not os.path.exists(os.path.join(sb.home, ".ipod-sync"))
    assert os.path.exists(sb.user_conf)                        # settings kept unless asked
    assert os.path.isdir(sb.library)
    assert ["bootout", "gui/%d/%s" % (os.getuid(), LABEL)] in launchctl(sb)


def test_uninstall_can_delete_settings(inst):
    sb = inst
    run_installer(sb, "install.sh", "--yes")
    r = run_installer(sb, "uninstall.sh", "--delete-settings")
    assert r.returncode == 0, r.stdout
    assert not os.path.exists(sb.user_conf)


def test_launcher_passes_arguments_and_exit_code(tmp_path):
    exe = str(tmp_path / "launcher")
    subprocess.run(["clang", "-O2", "-Wall", "-Werror", "-o", exe, os.path.join(REPO, "app", "launcher.c")],
                   check=True)
    home = tmp_path / "home"
    (home / ".ipod-sync").mkdir(parents=True)
    write_exe(str(home / ".ipod-sync" / "ipod-sync.sh"),
              '#!/bin/bash\nprintf "%s|" "$@" > "$HOME/args"\nexit 7\n')
    r = subprocess.run([exe, "--check", "two words"], env={"HOME": str(home)})
    assert r.returncode == 7
    assert (home / "args").read_text() == "--check|two words|"
