"""In-app software update: git pull + (if needed) dependency install.

Triggered from the settings menu. We deliberately do NOT restart the service
here -- the caller exits the process and systemd's ``Restart=always`` relaunches
it with the new code. That avoids needing passwordless sudo for systemctl.

(This updates the *handset* software, not the OnStep mount firmware.)
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass

from .config import _ROOT

log = logging.getLogger(__name__)


@dataclass
class UpdateResult:
    ok: bool          # the pull ran without error
    changed: bool     # new commits were actually applied
    message: str      # short human-readable summary / error


def _git(args: list[str], cwd: str, timeout: float = 60.0):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=timeout)


def update(repo_dir: str | None = None) -> UpdateResult:
    """Fast-forward the repo to origin and install deps if requirements changed."""
    repo = repo_dir or _ROOT
    try:
        head = _git(["rev-parse", "HEAD"], repo)
        if head.returncode != 0:
            return UpdateResult(False, False, "not a git repo")
        before = head.stdout.strip()

        pull = _git(["pull", "--ff-only"], repo)
        if pull.returncode != 0:
            msg = (pull.stderr or pull.stdout).strip().splitlines()
            return UpdateResult(False, False, msg[-1] if msg else "pull failed")

        after = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        changed = before != after
        if not changed:
            return UpdateResult(True, False, "Already up to date")

        # Refresh dependencies only if requirements.txt was among the changes.
        diff = _git(["diff", "--name-only", before, after], repo).stdout
        if "requirements.txt" in diff:
            pip = os.path.join(repo, ".venv", "bin", "pip")
            if os.path.exists(pip):
                log.info("requirements changed; updating dependencies")
                _run([pip, "install", "-r", "requirements.txt"], repo, timeout=300)
        return UpdateResult(True, True, "Updated")
    except (subprocess.SubprocessError, OSError) as exc:
        return UpdateResult(False, False, str(exc))


def _run(cmd: list[str], cwd: str, timeout: float):
    subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def under_systemd() -> bool:
    """True if we were launched as a systemd service (so exiting -> relaunch)."""
    return bool(os.environ.get("INVOCATION_ID"))
