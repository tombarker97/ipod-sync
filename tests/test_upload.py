"""scrobble-upload against the fake rb-scrobbler (never the real Last.fm)."""
import argparse
import collections
import glob
import os
import time

import ipod_tools as T

HOUR = 3600


def make_pending(sb, n, age=HOUR, name="scrobbler-20261126-120000.log", ignore=()):
    os.makedirs(os.path.join(sb.scrobbles, "pending"), exist_ok=True)
    now = int(time.time())
    plays = []
    for i in range(n):
        title = "Song %03d%s" % (i, " IGNOREME" if i in ignore else "")
        plays.append({"artist": "Artist", "album": "Album", "title": title, "track": "1",
                      "length_ms": 200000, "utc": now - age - i * 60, "mbid": ""})
    T.write_as(os.path.join(sb.scrobbles, "pending", name), plays)
    return plays


def upload(sb, mode="ok", *extra, env=None):
    e = {"FAKE_RB_MODE": mode, "IPOD_SYNC_UPLOAD_TIMEOUT": "5"}
    e.update(env or {})
    return sb.tools("scrobble-upload", "--data", sb.scrobbles, "--rb-scrobbler", sb.rb, *extra, env=e)


def pending_count(sb):
    n = 0
    for f in glob.glob(os.path.join(sb.scrobbles, "pending", "*.log")):
        n += sum(1 for line in open(f) if not line.startswith("#"))
    return n


def unsubmittable(sb):
    rows = []
    for f in glob.glob(os.path.join(sb.scrobbles, "unsubmittable", "*.log")):
        rows += [line.split("\t")[2] for line in open(f) if not line.startswith("#")]
    return sorted(rows)


def assert_no_duplicates(sb):
    seen = collections.Counter((p["title"], p["utc"]) for p in sb.accepted())
    dupes = {k: v for k, v in seen.items() if v > 1}
    assert not dupes, "plays sent to Last.fm more than once: %s" % dupes


def test_upload_all_ok(sb):
    make_pending(sb, 120)
    r = upload(sb)
    assert r.returncode == 0, r.stdout
    assert len(sb.accepted()) == 120
    assert pending_count(sb) == 0
    assert_no_duplicates(sb)
    assert os.listdir(os.path.join(sb.scrobbles, "sent"))


def test_upload_offline_then_retry(sb):
    make_pending(sb, 70)
    r = upload(sb, "fail")
    assert r.returncode == 2 and pending_count(sb) == 70
    assert "offline" in r.stdout or "Last.fm" in r.stdout
    r = upload(sb)
    assert r.returncode == 0, r.stdout
    assert len(sb.accepted()) == 70 and pending_count(sb) == 0
    assert_no_duplicates(sb)


def test_upload_partial_failure_does_not_resend_accepted_plays(sb):
    make_pending(sb, 120)
    r = upload(sb, "partial:1")          # batch 1 (50 plays) accepted, then the network drops
    assert r.returncode == 2, r.stdout
    assert len(sb.accepted()) == 50
    assert pending_count(sb) == 70
    r = upload(sb)
    assert r.returncode == 0, r.stdout
    assert len(sb.accepted()) == 120
    assert_no_duplicates(sb)


def test_upload_ignored_play_is_not_retried_forever(sb):
    make_pending(sb, 60, ignore={3})
    r = upload(sb, "ignore")
    for _ in range(3):                   # later syncs
        upload(sb, "ignore")
    assert_no_duplicates(sb)
    assert len(sb.accepted()) == 59
    assert pending_count(sb) == 0
    assert unsubmittable(sb) == ["Song 003 IGNOREME"]
    assert "rejected" in r.stdout


def test_upload_missing_rb_scrobbler(sb):
    make_pending(sb, 3)
    r = sb.tools("scrobble-upload", "--data", sb.scrobbles, "--rb-scrobbler", os.path.join(sb.root, "nope"))
    assert r.returncode == 2 and pending_count(sb) == 3
    assert "not found" in r.stdout


def test_upload_not_connected_suggests_auth(sb):
    make_pending(sb, 3)
    os.remove(sb.rb_key)
    r = upload(sb)
    assert r.returncode == 2 and pending_count(sb) == 3
    assert "-auth" in r.stdout


def test_upload_hang_times_out_and_keeps_plays(sb):
    make_pending(sb, 3)
    t = time.time()
    r = upload(sb, "hang")
    assert time.time() - t < 30
    assert r.returncode == 2, r.stdout
    assert pending_count(sb) == 3
    assert not glob.glob(os.path.join(sb.scrobbles, "pending", ".uploading*"))


def test_upload_dry_run_sends_nothing(sb):
    make_pending(sb, 3)
    r = upload(sb, "ok", "--dry-run")
    assert r.returncode == 0 and sb.accepted() == [] and pending_count(sb) == 3


def test_expired_plays_are_appended_not_overwritten(sb, monkeypatch):
    make_pending(sb, 2, age=HOUR)
    now = time.time()
    args = argparse.Namespace(data=sb.scrobbles, rb_scrobbler=sb.rb, dry_run=False)
    monkeypatch.setenv("FAKE_RB_MODE", "fail")
    monkeypatch.setenv("IPOD_SYNC_UPLOAD_TIMEOUT", "5")
    # day 14: the older play has expired, the other one is still pending
    monkeypatch.setattr(T.time, "time", lambda: now - HOUR + T.MAX_AGE_S - 30)
    T.cmd_scrobble_upload(args)
    assert unsubmittable(sb) == ["Song 001"]
    # day 15: the second one expires too - the first must still be there
    monkeypatch.setattr(T.time, "time", lambda: now + T.MAX_AGE_S + HOUR)
    T.cmd_scrobble_upload(args)
    assert unsubmittable(sb) == ["Song 000", "Song 001"]


def test_rejected_plays_are_noted_in_report(sb):
    make_pending(sb, 5, ignore={1})
    runf = os.path.join(sb.root, "run.json")
    upload(sb, "ignore", env={"IPOD_SYNC_RUN": runf})
    sb.tools("summary", "--report-dir", sb.report_dir, "--ejected", env={"IPOD_SYNC_RUN": runf})
    html = open(os.path.join(sb.report_dir, "Last sync.html"), encoding="utf-8").read()
    assert "Last.fm rejected 1 play" in html
