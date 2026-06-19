"""Tests for the in-app software updater, against an isolated local git repo
(no network, no touching the real checkout)."""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from onstep_handset import firmware


def _git(args, cwd):
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True)


def _setup(tmp_path):
    """origin (non-bare) with one commit, and a 'pi' clone of it."""
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git not available")
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init"], origin)
    (origin / "f.txt").write_text("1\n")
    _git(["add", "."], origin)
    _git(["commit", "-m", "init"], origin)
    pi = tmp_path / "pi"
    subprocess.run(["git", "clone", str(origin), str(pi)], capture_output=True)
    return origin, pi


def test_update_up_to_date(tmp_path):
    _, pi = _setup(tmp_path)
    r = firmware.update(str(pi))
    assert r.ok and not r.changed
    assert "up to date" in r.message.lower()


def test_update_applies_changes(tmp_path):
    origin, pi = _setup(tmp_path)
    # New commit on origin.
    (origin / "f.txt").write_text("2\n")
    _git(["commit", "-am", "change"], origin)

    r = firmware.update(str(pi))
    assert r.ok and r.changed

    # Second run: nothing new.
    r2 = firmware.update(str(pi))
    assert r2.ok and not r2.changed
    # The working tree actually advanced.
    assert (pi / "f.txt").read_text().strip() == "2"


def test_update_non_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    r = firmware.update(str(plain))
    assert not r.ok
    assert not r.changed
