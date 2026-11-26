"""Only one sync at a time - and a crashed sync must not block all future ones."""
import os
import signal
import subprocess
import time

import pytest

from conftest import SCRIPT, have_rsync3

pytestmark = pytest.mark.skipif(not have_rsync3(), reason="needs Homebrew rsync 3")


def ready(sb):
    sb.make_ipod()
    sb.add_track("A/B/01 a.mp3")
    return os.path.join(sb.ipod, "Music", "A", "B", "01 a.mp3")


def make_lock(sb, pid=None, age=0):
    lock = sb.env["IPOD_SYNC_LOCK"]
    os.makedirs(lock)
    if pid is not None:
        with open(os.path.join(lock, "pid"), "w") as fh:
            fh.write("%d\n" % pid)
    if age:
        t = time.time() - age
        os.utime(lock, (t, t))
    return lock


def dead_pid():
    p = subprocess.Popen(["/usr/bin/true"])
    p.wait()
    return p.pid


def test_lock_removed_after_sync(sb):
    copied = ready(sb)
    sb.run("--real")
    assert os.path.exists(copied)
    assert not os.path.exists(sb.env["IPOD_SYNC_LOCK"])


@pytest.mark.parametrize("kind", ["dead pid", "pid reused by another program", "old version, 1 h old"])
def test_stale_lock_is_taken_over(sb, kind):
    copied = ready(sb)
    other = None
    if kind == "dead pid":
        make_lock(sb, dead_pid())
    elif kind.startswith("pid reused"):
        other = subprocess.Popen(["/bin/sleep", "30"])
        make_lock(sb, other.pid)
    else:
        make_lock(sb, None, age=3600)
    try:
        r = sb.run("--real")
    finally:
        if other:
            other.kill()
    assert "already running" not in r.stdout
    assert os.path.exists(copied), r.stdout
    assert not os.path.exists(sb.env["IPOD_SYNC_LOCK"])


def test_running_sync_is_respected(sb):
    copied = ready(sb)
    # a process that looks like a running sync
    other = subprocess.Popen(["/bin/bash", "-c", 'exec -a "/bin/bash %s" /bin/sleep 30' % SCRIPT])
    make_lock(sb, other.pid)
    try:
        r = sb.run("--real")
    finally:
        other.kill()
    assert "already running" in r.stdout
    assert not os.path.exists(copied)


def test_fresh_lock_without_pid_is_respected(sb):
    copied = ready(sb)
    make_lock(sb, None)                  # another sync that has only just started
    r = sb.run("--real")
    assert "already running" in r.stdout and not os.path.exists(copied)


def test_kill_9_mid_sync_then_next_sync_works(sb):
    copied = ready(sb)
    sb.play_log(["%d:200000:200000:/Music/A/B/01 a.mp3" % (time.time() - 1800)])
    env = dict(sb.env, FAKE_RB_MODE="hang", IPOD_SYNC_UPLOAD_TIMEOUT="60")
    p = subprocess.Popen(["/bin/bash", SCRIPT, "--real"], env=env, cwd=sb.root, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 30
    while not os.path.exists(os.path.join(sb.ipod, ".rockbox", "playback.log")) or \
            open(os.path.join(sb.ipod, ".rockbox", "playback.log")).read():
        assert time.time() < deadline, "sync never got to the upload step"
        time.sleep(0.2)
    time.sleep(1)                         # now stuck uploading
    os.killpg(p.pid, signal.SIGKILL)      # the whole sync dies, like a crash
    p.wait()
    assert os.path.exists(sb.env["IPOD_SYNC_LOCK"])
    r = sb.run("--real")
    assert "already running" not in r.stdout
    assert os.path.exists(copied)
    assert [x["title"] for x in sb.accepted()] == ["a"]     # the play went up on the next sync
