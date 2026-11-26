"""playlists-push when the playlists folder is missing or a write fails."""
import argparse
import builtins
import os

import ipod_tools as T
from test_tools_unit import touch


def push(sb, playlists=None):
    return T.cmd_playlists_push(argparse.Namespace(
        playlists=playlists or sb.playlists, ipod=sb.ipod, library=sb.library, dry_run=False))


def setup(sb, names=("One", "Two", "Three")):
    sb.make_ipod()
    touch(os.path.join(sb.ipod, "Music", "A/B/01 a.mp3"))
    for n in names:
        with open(os.path.join(sb.playlists, n + ".m3u8"), "w") as fh:
            fh.write("A/B/01 a.mp3\n")
    assert push(sb) == 0
    return os.path.join(sb.ipod, "Playlists")


def test_missing_playlists_folder_removes_nothing(sb):
    pl = setup(sb)
    r = push(sb, playlists=os.path.join(sb.root, "Unplugged Drive", "Playlists"))
    assert r == 1
    assert sorted(os.listdir(pl)) == [".mac-playlists.txt", "One.m3u8", "Three.m3u8", "Two.m3u8"]


def test_one_playlist_failing_doesnt_stop_the_rest(sb, monkeypatch, capsys):
    pl = setup(sb)
    with open(os.path.join(sb.playlists, "Four.m3u8"), "w") as fh:
        fh.write("A/B/01 a.mp3\n")
    os.remove(os.path.join(sb.playlists, "One.m3u8"))
    real = builtins.open

    def opener(p, mode="r", *a, **k):
        if str(p).endswith("Four.m3u8") and "w" in mode:
            raise OSError(22, "Invalid argument", p)
        return real(p, mode, *a, **k)
    monkeypatch.setattr(builtins, "open", opener)
    rc = push(sb)
    monkeypatch.undo()
    assert rc == 1
    assert "Four" in capsys.readouterr().out
    assert sorted(os.listdir(pl)) == [".mac-playlists.txt", "Three.m3u8", "Two.m3u8"]   # One removed as asked
    assert open(os.path.join(pl, ".mac-playlists.txt")).read().split() == ["Three.m3u8", "Two.m3u8"]


def test_ipod_unplugged_while_writing_playlists(sb, monkeypatch, capsys):
    setup(sb)
    real = builtins.open

    def opener(p, mode="r", *a, **k):
        if str(p).startswith(sb.ipod) and "w" in mode:
            raise OSError(5, "Input/output error", p)
        return real(p, mode, *a, **k)
    monkeypatch.setattr(builtins, "open", opener)
    rc = push(sb)
    assert rc == 1
    assert "couldn't" in capsys.readouterr().out.lower()


def test_no_playlists_folder_at_all_is_fine(sb):
    sb.make_ipod()
    touch(os.path.join(sb.ipod, "Music", "A/B/01 a.mp3"))
    assert push(sb, playlists=os.path.join(sb.root, "never-made")) == 0
