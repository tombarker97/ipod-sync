"""Full syncs onto a real FAT32 disk image, like the iPod's SD card.

The image is called TESTPOD and is mounted inside the test sandbox (never at
/Volumes/IPOD, which a real sync job would react to). It is always detached
afterwards.
"""
import os
import shutil
import subprocess
import time
import unicodedata

import pytest

from conftest import have_rsync3, real_rsync, tree_hash, write_exe
from test_tools_unit import wall_now

pytestmark = [
    pytest.mark.fat32,
    pytest.mark.skipif(not shutil.which("hdiutil"), reason="needs macOS hdiutil"),
    pytest.mark.skipif(not have_rsync3(), reason="needs Homebrew rsync 3"),
]

TRICKY = [
    "Ben Böhmer/Breathing/01 Breathing.mp3",
    "Björk/Post/01 Army of Me.mp3",
    "R.E.M./Automatic for the People/02 Drive.mp3",          # folder ends with a dot
    "Sigur Rós/( )/01 Untitled #1.mp3",
    "Simon & Garfunkel/Bookends/03 Mrs. Robinson?.mp3",
    "AC/DC is a folder/Back in Black/01 \"Hells\" Bells.mp3".replace("AC/DC", "AC_DC"),
    "Emoji 🎧/Album: Part 1/01 Track * star.mp3",
    "Long/" + "L" * 120 + "/01 " + "n" * 150 + ".mp3",
]


def attach(image, mountpoint, readonly=False):
    os.makedirs(os.path.dirname(mountpoint), exist_ok=True)
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-noverify", "-mountpoint", mountpoint, image]
                   + (["-readonly"] if readonly else []), check=True, stdout=subprocess.DEVNULL)


def detach(mountpoint):
    if os.path.ismount(mountpoint):
        subprocess.run(["hdiutil", "detach", "-force", mountpoint], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)


@pytest.fixture
def fat(sb):
    assert not os.path.exists("/Volumes/IPOD"), "unplug the real iPod before running the FAT32 tests"
    made = []

    def make(size="64m"):
        image = os.path.join(sb.root, "testpod-%s.dmg" % size)
        subprocess.run(["hdiutil", "create", "-quiet", "-size", size, "-fs", "MS-DOS FAT32",
                        "-volname", "TESTPOD", image], check=True)
        attach(image, sb.ipod)
        made.append(sb.ipod)
        sb.make_ipod()
        sb.image = image
        return image
    sb.env["IPOD_SYNC_ALLOW_FOLDER"] = "no"      # it's a real mounted drive
    yield make
    for m in made:
        detach(m)


def no_appledouble(root):
    return [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs if f.startswith("._")]


def music_files(sb):
    return sorted(unicodedata.normalize("NFC", k).casefold()
                  for k in tree_hash(os.path.join(sb.ipod, "Music")))


def lib_files(sb):
    return sorted(unicodedata.normalize("NFC", k).casefold() for k in tree_hash(sb.library))


def test_real_and_dry_runs_on_fat32(sb, fat):
    fat()
    for rel in TRICKY:
        sb.add_track(unicodedata.normalize("NFD", rel))      # macOS-style accents
    with open(os.path.join(sb.playlists, "Tricky 🎧.m3u8"), "w") as fh:
        fh.write("\n".join(TRICKY) + "\n")
    sb.play_log(["%d:200000:200000:/<HDD0>/Music/%s" % (wall_now() - 3600, TRICKY[0])])

    ipod_before = tree_hash(sb.ipod)
    r = sb.run("--dry-run")
    assert r.returncode == 0, r.stdout
    assert tree_hash(sb.ipod) == ipod_before, "a dry run changed the iPod"

    r = sb.run("--real")
    assert r.returncode == 0, r.stdout
    assert music_files(sb) == lib_files(sb)
    assert no_appledouble(os.path.join(sb.ipod, "Music")) == []
    pl = open(os.path.join(sb.ipod, "Playlists", "Tricky 🎧.m3u8"), encoding="utf-8").read().splitlines()
    assert len(pl) == len(TRICKY)
    assert [p["title"] for p in sb.accepted()] == ["Breathing"]

    # syncing again: nothing copied (FAT's 2-second times, accents), nothing deleted
    sb.calls()  # (keep the record)
    r = sb.run("--real")
    assert "Files: 0 to copy/update" in r.stdout, r.stdout
    assert "Prune: deleted 0 files" in r.stdout
    assert len(sb.accepted()) == 1


def test_real_eject(sb, fat):
    fat()
    sb.add_track("A/B/01 a.mp3")
    os.remove(os.path.join(sb.stubs, "diskutil"))     # use the real diskutil on our test image
    r = sb.run("--real")
    assert "Ejected" in r.stdout, r.stdout
    assert not os.path.ismount(sb.ipod)
    assert sb.notifications()[-1][0] == "iPod synced - safe to unplug"


def test_ipod_full(sb, fat):
    fat("40m")
    for i in range(3):                                # an album deleted on the Mac, still on the iPod
        sb.add_track("Old/Gone/%02d old.mp3" % i, root=os.path.join(sb.ipod, "Music"), frames=2500)
    for i in range(50):
        sb.add_track("Big/Album/%02d song.mp3" % i, frames=2500)     # ~1 MB each
    sb.play_log(["%d:200000:200000:/Music/Big/Album/01 song.mp3" % (wall_now() - 3600)])
    r = sb.run("--real")
    assert "Traceback" not in r.stdout, r.stdout
    title, detail = sb.notifications()[-1]
    assert title.startswith("iPod sync problem")
    assert "full" in detail.lower(), detail
    assert len(sb.accepted()) == 1                    # plays still went to Last.fm
    assert sb.calls("diskutil") == []                 # not ejected: you're told to eject in Finder
    assert not os.path.exists(os.path.join(sb.ipod, "Music", "Old")), "old album should make room"


def test_read_only_ipod(sb, fat):
    fat()
    sb.add_track("A/B/01 a.mp3")
    sb.play_log(["%d:200000:200000:/Music/A/B/01 a.mp3" % (wall_now() - 3600)])
    detach(sb.ipod)
    attach(sb.image, sb.ipod, readonly=True)
    r = sb.run("--real")
    assert "Traceback" not in r.stdout, r.stdout
    assert "won't be counted twice" in r.stdout
    assert len(sb.accepted()) == 1
    # writable again: the play log is still there, but it isn't scrobbled twice
    detach(sb.ipod)
    attach(sb.image, sb.ipod)
    r = sb.run("--real")
    assert r.returncode == 0, r.stdout
    assert len(sb.accepted()) == 1
    assert music_files(sb) == lib_files(sb)


UNPLUG_RSYNC = r'''#!/bin/bash
# copy slowly, and pull the "iPod" out part-way through the main music copy
case " $* " in *" --itemize-changes "*) ;; *) exec "$REAL_RSYNC" "$@" ;; esac
"$REAL_RSYNC" --bwlimit=300 "$@" & pid=$!
sleep 2
hdiutil detach -force "$FAT_MOUNT" >/dev/null 2>&1
wait $pid
'''


def test_unplugged_during_copy(sb, fat):
    fat()
    for i in range(12):
        sb.add_track("A/B/%02d song.mp3" % i, frames=2500)
    sb.play_log(["%d:200000:200000:/Music/A/B/01 song.mp3" % (wall_now() - 3600)])
    write_exe(os.path.join(sb.stubs, "rsync"), UNPLUG_RSYNC)
    t = time.time()
    r = sb.run("--real", env={"FAT_MOUNT": sb.ipod, "REAL_RSYNC": real_rsync()})
    assert time.time() - t < 120
    assert not os.path.ismount(sb.ipod)
    assert "Traceback" not in r.stdout, r.stdout
    title, detail = sb.notifications()[-1]
    assert "unplugged" in detail.lower(), (title, detail)
    assert len(sb.accepted()) == 1                    # plays were collected before the copy
    assert tree_hash(sb.library)                      # library untouched
    # plug it back in: the sync finishes the job, and nothing is scrobbled twice
    os.remove(os.path.join(sb.stubs, "rsync"))
    from conftest import RSYNC_GUARD
    write_exe(os.path.join(sb.stubs, "rsync"), RSYNC_GUARD)
    attach(sb.image, sb.ipod)
    r = sb.run("--real")
    assert r.returncode == 0, r.stdout
    assert music_files(sb) == lib_files(sb)
    assert len(sb.accepted()) == 1
