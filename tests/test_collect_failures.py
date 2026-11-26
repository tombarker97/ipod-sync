"""Play collection when things go wrong part-way (unplugged, read-only, bad copy)."""
import argparse
import glob
import os
import shutil

import pytest

import ipod_tools as T
from test_tools_unit import LONDON, wall_now


def args(sb):
    return argparse.Namespace(ipod=sb.ipod, library=sb.library, data=sb.scrobbles, tz=LONDON, dry_run=False)


def later_sync(sb):
    """Give files from this run older names, as if the next sync is another day."""
    for f in glob.glob(os.path.join(sb.scrobbles, "*", "*-20*")):
        os.rename(f, os.path.join(os.path.dirname(f), "earlier-" + os.path.basename(f)))


def pending(sb):
    n = 0
    for f in glob.glob(os.path.join(sb.scrobbles, "pending", "*.log")):
        n += sum(1 for line in open(f) if not line.startswith("#"))
    return n


@pytest.fixture
def ipod_with_plays(sb):
    sb.make_ipod()
    sb.add_track("A/B/01 a.mp3")
    sb.add_track("A/B/02 b.mp3")
    t = wall_now() - 7200
    sb.play_log(["%d:200000:200000:/Music/A/B/01 a.mp3" % t], name="playback_0001.log")
    sb.play_log(["%d:200000:200000:/Music/A/B/02 b.mp3" % (t + 600)])
    return sb


def test_bad_archive_copy_then_retry_gives_no_duplicates(ipod_with_plays, monkeypatch):
    sb = ipod_with_plays
    real = shutil.copy2

    def short_copy(src, dst):
        real(src, dst)
        with open(dst, "a") as fh:
            fh.write("x")                 # copy doesn't match
    monkeypatch.setattr(T.shutil, "copy2", short_copy)
    assert T.cmd_scrobble_collect(args(sb)) == 1
    monkeypatch.setattr(T.shutil, "copy2", real)
    later_sync(sb)
    assert T.cmd_scrobble_collect(args(sb)) == 0
    assert pending(sb) == 2


@pytest.mark.parametrize("step", ["remove rotated log", "truncate current log"])
def test_ipod_gone_while_clearing_logs(ipod_with_plays, monkeypatch, capsys, step):
    sb = ipod_with_plays
    real_remove, real_open = os.remove, open

    def remove(p, *a, **k):
        if step == "remove rotated log" and p.startswith(sb.ipod):
            raise OSError(5, "Input/output error", p)        # what an unplugged drive gives
        return real_remove(p, *a, **k)

    def opener(p, mode="r", *a, **k):
        if step == "truncate current log" and str(p).startswith(sb.ipod) and "w" in mode:
            raise PermissionError(30, "Read-only file system", p)
        return real_open(p, mode, *a, **k)
    monkeypatch.setattr(T.os, "remove", remove)
    monkeypatch.setattr("builtins.open", opener)
    rc = T.cmd_scrobble_collect(args(sb))
    monkeypatch.undo()
    out = capsys.readouterr().out
    assert rc == 3, out
    assert "won't be counted twice" in out
    assert pending(sb) == 2
    # the logs are still on the iPod next time: nothing is collected twice
    later_sync(sb)
    assert T.cmd_scrobble_collect(args(sb)) == 0
    assert pending(sb) == 2


def test_ipod_gone_before_archiving(ipod_with_plays, monkeypatch):
    sb = ipod_with_plays

    def gone(src, dst):
        raise OSError(5, "Input/output error", src)
    monkeypatch.setattr(T.shutil, "copy2", gone)
    assert T.cmd_scrobble_collect(args(sb)) == 1
    assert pending(sb) == 0 and not os.path.exists(os.path.join(sb.scrobbles, "ledger.txt"))
    monkeypatch.undo()
    later_sync(sb)
    assert T.cmd_scrobble_collect(args(sb)) == 0
    assert pending(sb) == 2
