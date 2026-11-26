"""Full ipod-sync.sh runs (under macOS's /bin/bash 3.2) against a fake iPod folder."""
import glob
import os
import shutil
import subprocess
import time

import pytest

from conftest import have_rsync3, tree_hash
from test_tools_unit import wall_now

pytestmark = pytest.mark.skipif(not have_rsync3(), reason="needs Homebrew rsync 3")


def setup_library(sb):
    sb.make_ipod()
    sb.add_track("Ben Böhmer/Breathing/01 Breathing.mp3")
    sb.add_track("KETTAMA/Archangel/01 Archangel.mp3")
    sb.add_track("Björk/Post/01 Army of Me.mp3")
    # the iPod already has two of them, plus an old album that was deleted on the Mac
    for rel in ["Ben Böhmer/Breathing/01 Breathing.mp3", "Björk/Post/01 Army of Me.mp3"]:
        shutil.copy2(os.path.join(sb.library, rel), sb.add_track(rel, root=os.path.join(sb.ipod, "Music")))
    sb.add_track("Old Band/Gone/01 Gone.mp3", root=os.path.join(sb.ipod, "Music"))
    with open(os.path.join(sb.playlists, "Mix.m3u8"), "w") as fh:
        fh.write("KETTAMA/Archangel/01 Archangel.mp3\nBjörk/Post/01 Army of Me.mp3\n")
    sb.play_log(["%d:200000:200000:/<HDD0>/Music/Old Band/Gone/01 Gone.mp3" % (wall_now() - 3600)])


def snapshot(sb):
    return {
        "ipod": tree_hash(sb.ipod),
        "library": tree_hash(sb.library),
        "playlists": tree_hash(sb.playlists),
        "scrobbles": {k: v for k, v in tree_hash(sb.scrobbles).items() if not k.startswith("dry-run/")}
        if os.path.isdir(sb.scrobbles) else {},
        "backup": tree_hash(sb.backup) if os.path.isdir(sb.backup) else {},
    }


def test_not_mounted_does_nothing(sb):
    r = sb.run("--dry-run")
    assert r.returncode == 0
    assert "iPod not mounted" in r.stdout
    assert sb.notifications() == []


def test_bash_is_3_2():
    out = subprocess.run(["/bin/bash", "--version"], stdout=subprocess.PIPE, text=True).stdout
    assert "version 3.2" in out, "tests are meant to run on macOS's own bash"


def test_dry_run_changes_nothing(sb):
    setup_library(sb)
    before = snapshot(sb)
    r = sb.run("--dry-run")
    assert r.returncode == 0, r.stdout
    assert "DRY RUN" in r.stdout
    assert "1 to copy/update" in r.stdout and "1 files would be deleted" in r.stdout
    assert snapshot(sb) == before
    assert sb.accepted() == []
    assert sb.calls("diskutil") == []
    assert sb.notifications()[-1][0] == "iPod dry run finished"
    assert os.path.exists(os.path.join(sb.report_dir, "Last sync.html"))


def test_real_run_then_nothing_to_do(sb):
    setup_library(sb)
    r = sb.run("--real")
    assert r.returncode == 0, r.stdout
    music = os.path.join(sb.ipod, "Music")
    assert sorted(tree_hash(music)) == sorted(tree_hash(sb.library))      # mirrored, old album pruned
    assert open(os.path.join(sb.ipod, "Playlists", "Mix.m3u8")).read() == \
        "/Music/KETTAMA/Archangel/01 Archangel.mp3\n/Music/Björk/Post/01 Army of Me.mp3\n"
    assert open(os.path.join(sb.ipod, ".rockbox", "playback.log")).read() == ""
    assert [p["title"] for p in sb.accepted()] == ["Gone"]
    assert len(glob.glob(os.path.join(sb.backup, "*", "config.cfg"))) == 1
    assert sb.calls("diskutil") == [["eject", sb.ipod]]
    assert sb.notifications()[-1] == ("iPod synced - safe to unplug", "+1 song · −1 song · 1 play scrobbled")

    before = tree_hash(music)
    r = sb.run("--real")
    assert r.returncode == 0, r.stdout
    assert tree_hash(music) == before
    assert len(sb.accepted()) == 1
    assert sb.notifications()[-1] == ("iPod synced - safe to unplug", "Already up to date · no new plays")


def test_missing_library_never_touches_ipod(sb):
    setup_library(sb)
    sb.config(SRC=os.path.join(sb.root, "Unplugged Drive", "Music Library") + "/")
    before = tree_hash(sb.ipod)
    r = sb.run("--real")
    assert r.returncode == 1
    assert tree_hash(sb.ipod) == before
    assert "Music Library folder not found" in sb.notifications()[-1][1]


def test_python_helper_missing_still_copies_music(sb):
    setup_library(sb)
    sb.config(PYTHON=os.path.join(sb.root, "no-python"))
    r = sb.run("--real")
    assert os.path.exists(os.path.join(sb.ipod, "Music", "KETTAMA", "Archangel", "01 Archangel.mp3"))
    assert "Python helper missing" in r.stdout
    title, detail = sb.notifications()[-1]
    assert title == "iPod sync finished with problems" and "Python helper missing" in detail
    assert sb.accepted() == []


def test_slow_launchd_start_path(sb):
    """No arguments = started by launchd: output only goes to the log."""
    setup_library(sb)
    t = time.time()
    r = sb.run()
    assert r.returncode == 0 and r.stdout == ""
    assert "safe to unplug" in sb.logtext()
    assert time.time() - t < 60
