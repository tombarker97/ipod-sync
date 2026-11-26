"""Files that used to grow forever: the log, history.jsonl, the ledger, dry-run previews."""
import argparse
import glob
import os

import pytest

import ipod_tools as T
from conftest import have_rsync3
from test_tools_unit import LONDON, wall_now

DAY = 86400


@pytest.mark.skipif(not have_rsync3(), reason="needs Homebrew rsync 3")
def test_big_log_is_rotated(sb):
    sb.make_ipod()
    sb.add_track("A/B/01 a.mp3")
    os.makedirs(os.path.dirname(sb.log))
    with open(sb.log, "w") as fh:
        fh.write("old line\n" * 300000)          # ~2.7 MB
    sb.run("--real")
    assert os.path.getsize(sb.log) < 100000
    assert open(sb.log + ".1").read().startswith("old line")


def test_history_keeps_last_200(sb):
    os.makedirs(sb.report_dir)
    with open(os.path.join(sb.report_dir, "history.jsonl"), "w") as fh:
        for i in range(500):
            fh.write('{"when": "2026-01-01 00:00", "n": %d}\n' % i)
    r = sb.tools("summary", "--report-dir", sb.report_dir, "--ejected")
    assert r.returncode == 0, r.stdout
    lines = open(os.path.join(sb.report_dir, "history.jsonl")).read().splitlines()
    assert len(lines) == 200 and '"n": 301' in lines[0]


def collect(sb, dry=False):
    return T.cmd_scrobble_collect(argparse.Namespace(ipod=sb.ipod, library=sb.library, data=sb.scrobbles,
                                                     tz=LONDON, dry_run=dry))


def test_ledger_drops_entries_too_old_to_upload(sb):
    sb.make_ipod()
    sb.add_track("A/B/01 a.mp3")
    os.makedirs(sb.scrobbles)
    old = ["1284636588:109349:109400:/music/balance and composure/with you.mp3",      # 2010, iPod clock unset
           "%d:200000:200000:/music/a/b/01 a.mp3" % (wall_now() - 90 * DAY),
           "legacy:A\tB\tT\t1\t200\tL\t%d\t" % (wall_now() - 90 * DAY)]
    recent = ["%d:200000:200000:/music/a/b/01 a.mp3" % (wall_now() - 5 * DAY),
              "%d:200000:200000:/music/a/b/01 a.mp3" % (wall_now() + 400 * DAY)]       # clock in the future
    with open(os.path.join(sb.scrobbles, "ledger.txt"), "w") as fh:
        fh.write("\n".join(old + recent) + "\n")
    sb.play_log(["%d:200000:200000:/Music/A/B/01 a.mp3" % (wall_now() - 3600)])
    assert collect(sb) == 0
    ledger = open(os.path.join(sb.scrobbles, "ledger.txt")).read().splitlines()
    assert not set(old) & set(ledger)
    assert set(recent) <= set(ledger) and len(ledger) == 3


def test_trimmed_ledger_cannot_cause_duplicates(sb):
    """A play forgotten by the ledger is too old for Last.fm, so it can't be uploaded again."""
    sb.make_ipod()
    sb.add_track("A/B/01 a.mp3")
    os.makedirs(sb.scrobbles)
    line = "%d:200000:200000:/Music/A/B/01 a.mp3" % (wall_now() - T.LEDGER_DAYS * DAY - DAY)
    sb.play_log([line, "1284636588:200000:200000:/Music/A/B/01 a.mp3"])   # still on the iPod somehow
    assert collect(sb) == 0
    assert glob.glob(os.path.join(sb.scrobbles, "pending", "*.log")) == []


def test_dry_run_previews_keep_last_10(sb, monkeypatch):
    sb.make_ipod()
    sb.play_log(["%d:200000:200000:/Music/A/B/01 a.mp3" % (wall_now() - 3600)])
    base = os.path.join(sb.scrobbles, "dry-run")
    for i in range(15):
        os.makedirs(os.path.join(base, "20260101-0000%02d" % i))
    collect(sb, dry=True)
    left = sorted(os.listdir(base))
    assert len(left) == 10 and left[0] == "20260101-000006"
