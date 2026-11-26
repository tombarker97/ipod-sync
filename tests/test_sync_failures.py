"""Whole-sync behaviour when Last.fm or the play log misbehaves."""
import os
import random
import time

import pytest

from conftest import have_rsync3
from test_tools_unit import wall_now

pytestmark = pytest.mark.skipif(not have_rsync3(), reason="needs Homebrew rsync 3")


def ready(sb, plays=1):
    sb.make_ipod()
    sb.add_track("A/B/01 a.mp3")
    sb.play_log(["%d:200000:200000:/Music/A/B/01 a.mp3" % (wall_now() - 3600 * (i + 1)) for i in range(plays)])


@pytest.mark.parametrize("mode", ["fail", "nokey", "missing"])
def test_lastfm_down_keeps_plays_and_still_ejects(sb, mode):
    ready(sb, plays=2)
    if mode == "nokey":
        os.remove(sb.rb_key)
    if mode == "missing":
        sb.config(RB_SCROBBLER=os.path.join(sb.root, "not-installed", "rb-scrobbler"))
    r = sb.run("--real", env={"FAKE_RB_MODE": mode})
    title, detail = sb.notifications()[-1]
    assert title == "iPod synced with warnings - safe to unplug", r.stdout
    assert "Last.fm" in detail
    assert sb.calls("diskutil")                          # the iPod was still ejected
    assert sb.accepted() == []
    if mode == "nokey":
        assert "-auth" in detail
        with open(sb.rb_key, "w") as fh:
            fh.write("fake\n")
    # next time Last.fm works: each play goes up exactly once
    sb.config(RB_SCROBBLER=sb.rb)
    sb.run("--real")
    sb.run("--real")
    assert len(sb.accepted()) == 2
    assert len({p["utc"] for p in sb.accepted()}) == 2


def test_huge_rotated_and_corrupt_logs(sb):
    sb.make_ipod()
    for i in range(20):
        sb.add_track("A/B/%02d t%d.mp3" % (i, i))
    rnd = random.Random(1)
    now = wall_now()
    lines = ["%d:200000:200000:/<HDD0>/Music/A/B/%02d t%d.mp3" % (now - 300 * n, n % 20, n % 20)
             for n in range(200000)]
    rnd.shuffle(lines)
    sb.play_log(lines[:100000], name="playback_0001.log")
    sb.play_log(lines[100000:] + ["\x00\x00garbage:::", "999:x:y:z"])
    with open(os.path.join(sb.ipod, ".rockbox", "playback.log"), "ab") as fh:
        fh.write(os.urandom(5000))
    t = time.time()
    r = sb.run("--real")
    assert time.time() - t < 30, "too slow"
    assert "read 200000 log entries" in r.stdout
    n = len(sb.accepted())
    assert 0 < n < 5000                    # only the last 14 days can go to Last.fm
    assert n == len({(p["title"], p["utc"]) for p in sb.accepted()})
