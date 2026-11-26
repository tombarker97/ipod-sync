"""Backups of the Rockbox settings, and keeping only the last KEEP_BACKUPS of them."""
import os
import subprocess

import pytest

from conftest import have_rsync3

pytestmark = pytest.mark.skipif(not have_rsync3(), reason="needs Homebrew rsync 3")


def test_rm_guard_is_what_the_script_runs(sb):
    """The test-only `rm` that refuses paths outside the sandbox must win the PATH lookup."""
    path = sb.stubs + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    out = subprocess.run(["/bin/bash", "-c", "command -v rm"], env={"PATH": path},
                         stdout=subprocess.PIPE, text=True).stdout.strip()
    assert out == os.path.join(sb.stubs, "rm")


def stamps(n):
    return ["2026-%02d-%02d_1200" % (1 + i // 28, 1 + i % 28) for i in range(n)]


def test_keeps_last_n_backups(sb):
    sb.make_ipod()
    for s in stamps(12):
        os.makedirs(os.path.join(sb.backup, s))
    sb.config(KEEP_BACKUPS="10")
    r = sb.run("--real")
    left = sorted(os.listdir(sb.backup))
    assert len(left) == 10, r.stdout
    assert stamps(12)[:3][-1] not in left and stamps(12)[-1] in left    # the oldest went
    assert any(os.path.exists(os.path.join(sb.backup, d, "config.cfg")) for d in left)


def test_backup_folder_shared_with_other_folders(sb):
    """BACKUP pointed at a folder holding other things: only dated backups may be deleted."""
    sb.make_ipod()
    music = os.path.join(sb.home, "Music")
    decoys = ["Audio Music Apps", "GarageBand", "Logic"] + ["Project %02d" % i for i in range(12)]
    for d in decoys:
        os.makedirs(os.path.join(music, d))
    sb.config(BACKUP=music)
    r = sb.run("--real")
    for d in decoys + ["Music Library", "Playlists"]:
        assert os.path.isdir(os.path.join(music, d)), "%s was deleted!\n%s" % (d, r.stdout)


@pytest.mark.parametrize("value", ["ten", "", "0"])
def test_bad_keep_backups_never_deletes_everything(sb, value):
    sb.make_ipod()
    for s in stamps(3):
        os.makedirs(os.path.join(sb.backup, s))
    sb.config(KEEP_BACKUPS=value)
    sb.run("--real")
    assert len(os.listdir(sb.backup)) >= 3


@pytest.mark.parametrize("value", ["", "/", "relative/folder", "$HOME"])
def test_unusable_backup_setting_is_refused(sb, value):
    sb.make_ipod()
    sb.raw_config(open(sb.conf).read() + 'BACKUP="%s"\n' % value)
    r = sb.run("--real")
    # (the sandbox's rm guard fails the test if anything outside the sandbox is deleted)
    assert "BACKUP" in r.stdout
    assert not os.path.exists(os.path.join(sb.root, "rm-refused")), open(os.path.join(sb.root, "rm-refused")).read()
