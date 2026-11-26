"""Finding the iPod: permissions, "IPOD 1", two iPods, leftover folders."""
import os

import pytest

from conftest import have_rsync3, tree_hash

pytestmark = pytest.mark.skipif(not have_rsync3(), reason="needs Homebrew rsync 3")


@pytest.fixture
def locked(sb):
    """The iPod is mounted but macOS won't let us read it (simulated with chmod 000)."""
    sb.make_ipod()
    os.chmod(sb.ipod, 0)
    yield sb
    os.chmod(sb.ipod, 0o755)


def test_check_reports_permission_denied(locked):
    r = locked.run("--check")
    assert r.returncode == 1
    assert "Removable Volumes" in r.stdout
    assert locked.notifications()[-1][0] == "iPod Sync can't read the iPod"


def test_automatic_run_reports_permission_denied(locked):
    r = locked.run()
    assert r.returncode == 1
    assert "Removable Volumes" in locked.logtext()
    assert locked.notifications()[-1][0] == "iPod Sync can't read the iPod"


def test_check_ok(sb):
    sb.make_ipod()
    r = sb.run("--check")
    assert r.returncode == 0 and "OK: can read" in r.stdout
    assert sb.notifications()[-1][0] == "iPod Sync can read your iPod"


def test_ipod_mounted_as_name_1(sb):
    sb.make_ipod()
    os.rename(sb.ipod, sb.ipod + " 1")        # macOS renames a second "TESTPOD"
    sb.add_track("A/B/01 a.mp3")
    r = sb.run("--real")
    assert os.path.exists(os.path.join(sb.ipod + " 1", "Music", "A", "B", "01 a.mp3")), r.stdout
    assert sb.calls("diskutil") == [["eject", sb.ipod + " 1"]]


def test_other_drive_with_the_same_name(sb):
    sb.make_ipod()
    os.rename(sb.ipod, sb.ipod + " 1")
    os.makedirs(os.path.join(sb.volumes, "TESTPOD", "Photos"))   # a USB stick also called TESTPOD
    sb.add_track("A/B/01 a.mp3")
    sb.run("--real")
    assert os.path.exists(os.path.join(sb.ipod + " 1", "Music", "A", "B", "01 a.mp3"))
    assert os.listdir(os.path.join(sb.volumes, "TESTPOD")) == ["Photos"]


def test_two_ipods_are_not_guessed_between(sb):
    sb.make_ipod()
    second = sb.ipod + " 1"
    os.makedirs(os.path.join(second, ".rockbox"))
    sb.add_track("A/B/01 a.mp3")
    before = (tree_hash(sb.ipod), tree_hash(second))
    r = sb.run("--real")
    assert r.returncode == 1
    assert (tree_hash(sb.ipod), tree_hash(second)) == before
    assert "unplug one" in sb.notifications()[-1][1]


def test_plain_folder_is_not_an_ipod(sb):
    """A leftover folder in /Volumes (not a mounted drive) must never be synced into."""
    sb.make_ipod()
    sb.add_track("A/B/01 a.mp3")
    r = sb.run("--real", env={"IPOD_SYNC_ALLOW_FOLDER": "no"})
    assert r.returncode == 0
    assert "not a mounted drive" in r.stdout
    assert not os.path.exists(os.path.join(sb.ipod, "Music"))
