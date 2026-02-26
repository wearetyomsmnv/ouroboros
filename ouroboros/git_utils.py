"""
Git and startup-verification utilities for Ouroboros agent.

Standalone functions extracted from OuroborosAgent so that agent.py stays
thin and this logic can be tested or reused independently.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import subprocess
from typing import Callable, Tuple

from ouroboros.utils import append_jsonl, read_text, utc_now_iso

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Restart verification
# ---------------------------------------------------------------------------

def verify_restart(
    drive_path_fn: Callable[[str], pathlib.Path],
    repo_dir: pathlib.Path,
    git_sha: str,
) -> None:
    """Best-effort restart verification.

    Looks for ``state/pending_restart_verify.json`` on Drive.
    If found, atomically claims it, checks whether the observed git SHA matches
    the expected one, logs the result, then deletes the claim file.
    """
    try:
        pending_path = drive_path_fn("state") / "pending_restart_verify.json"
        claim_path = pending_path.with_name(
            f"pending_restart_verify.claimed.{os.getpid()}.json"
        )
        try:
            os.rename(str(pending_path), str(claim_path))
        except (FileNotFoundError, Exception):
            return
        try:
            claim_data = json.loads(read_text(claim_path))
            expected_sha = str(claim_data.get("expected_sha", "")).strip()
            ok = bool(expected_sha and expected_sha == git_sha)
            append_jsonl(
                drive_path_fn("logs") / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "restart_verify",
                    "pid": os.getpid(),
                    "ok": ok,
                    "expected_sha": expected_sha,
                    "observed_sha": git_sha,
                },
            )
        except Exception:
            log.debug("Failed to log restart verify event", exc_info=True)
        try:
            claim_path.unlink()
        except Exception:
            log.debug("Failed to delete restart verify claim file", exc_info=True)
    except Exception:
        log.debug("Restart verification failed", exc_info=True)


# ---------------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------------

def check_uncommitted_changes(
    repo_dir: pathlib.Path,
    branch_dev: str,
) -> Tuple[dict, int]:
    """Check for uncommitted changes and attempt auto-rescue commit & push.

    Returns ``(result_dict, issue_count)``.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        dirty_files = [
            line.strip()
            for line in result.stdout.strip().split("\n")
            if line.strip()
        ]
        if dirty_files:
            auto_committed = False
            try:
                # Only stage tracked files (not secrets/notebooks)
                subprocess.run(
                    ["git", "add", "-u"],
                    cwd=str(repo_dir),
                    timeout=10,
                    check=True,
                )
                subprocess.run(
                    [
                        "git",
                        "commit",
                        "-m",
                        "auto-rescue: uncommitted changes detected on startup",
                    ],
                    cwd=str(repo_dir),
                    timeout=30,
                    check=True,
                )
                # Validate branch name before using it in shell commands
                if not re.match(r"^[a-zA-Z0-9_/-]+$", branch_dev):
                    raise ValueError(f"Invalid branch name: {branch_dev}")
                # Pull with rebase before push
                subprocess.run(
                    ["git", "pull", "--rebase", "origin", branch_dev],
                    cwd=str(repo_dir),
                    timeout=60,
                    check=True,
                )
                # Push
                try:
                    subprocess.run(
                        ["git", "push", "origin", branch_dev],
                        cwd=str(repo_dir),
                        timeout=60,
                        check=True,
                    )
                    auto_committed = True
                    log.warning(
                        f"Auto-rescued {len(dirty_files)} uncommitted files on startup"
                    )
                except subprocess.CalledProcessError:
                    # Push failed — undo the commit so we don't leave a dangling local commit
                    subprocess.run(
                        ["git", "reset", "HEAD~1"],
                        cwd=str(repo_dir),
                        timeout=10,
                        check=True,
                    )
                    raise
            except Exception as exc:
                log.warning(
                    f"Failed to auto-rescue uncommitted changes: {exc}", exc_info=True
                )
            return {
                "status": "warning",
                "files": dirty_files[:20],
                "auto_committed": auto_committed,
            }, 1
        else:
            return {"status": "ok"}, 0
    except Exception as exc:
        return {"status": "error", "error": str(exc)}, 0


def check_version_sync(repo_dir: pathlib.Path) -> Tuple[dict, int]:
    """Check VERSION file sync with git tags and pyproject.toml.

    Returns ``(result_dict, issue_count)``.
    Bible P7: VERSION == latest git tag == README version.
    """
    try:
        version_file = read_text(repo_dir / "VERSION").strip()
        issue_count = 0
        result_data: dict = {"version_file": version_file}

        # --- pyproject.toml ---
        try:
            pyproject_content = read_text(repo_dir / "pyproject.toml")
            match = re.search(
                r'^version\s*=\s*["\']([^"\']+)["\']',
                pyproject_content,
                re.MULTILINE,
            )
            if match:
                pyproject_version = match.group(1)
                result_data["pyproject_version"] = pyproject_version
                if version_file != pyproject_version:
                    result_data["status"] = "warning"
                    issue_count += 1
        except Exception:
            log.debug("Failed to read pyproject.toml for version check", exc_info=True)

        # --- README.md ---
        try:
            readme_content = read_text(repo_dir / "README.md")
            readme_match = re.search(
                r"\*\*Version:\*\*\s*(\d+\.\d+\.\d+)", readme_content
            )
            if readme_match:
                readme_version = readme_match.group(1)
                result_data["readme_version"] = readme_version
                if version_file != readme_version:
                    result_data["status"] = "warning"
                    issue_count += 1
        except Exception:
            log.debug("Failed to check README.md version", exc_info=True)

        # --- git tags ---
        git_result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if git_result.returncode != 0:
            result_data["status"] = "warning"
            result_data["message"] = "no_tags"
            return result_data, issue_count

        latest_tag = git_result.stdout.strip().lstrip("v")
        result_data["latest_tag"] = latest_tag
        if version_file != latest_tag:
            result_data["status"] = "warning"
            issue_count += 1

        if issue_count == 0:
            result_data["status"] = "ok"

        return result_data, issue_count
    except Exception as exc:
        return {"status": "error", "error": str(exc)}, 0


def check_budget(
    drive_path_fn: Callable[[str], pathlib.Path],
) -> Tuple[dict, int]:
    """Check budget remaining with warning thresholds.

    Returns ``(result_dict, issue_count)``.
    """
    try:
        state_path = drive_path_fn("state") / "state.json"
        state_data = json.loads(read_text(state_path))
        total_budget_str = os.environ.get("TOTAL_BUDGET", "")

        if not total_budget_str or float(total_budget_str) == 0:
            return {"status": "unconfigured"}, 0

        total_budget = float(total_budget_str)
        spent = float(state_data.get("spent_usd", 0))
        remaining = max(0.0, total_budget - spent)

        if remaining < 10:
            status, issues = "emergency", 1
        elif remaining < 50:
            status, issues = "critical", 1
        elif remaining < 100:
            status, issues = "warning", 0
        else:
            status, issues = "ok", 0

        return {
            "status": status,
            "remaining_usd": round(remaining, 2),
            "total_usd": total_budget,
            "spent_usd": round(spent, 2),
        }, issues
    except Exception as exc:
        return {"status": "error", "error": str(exc)}, 0


# ---------------------------------------------------------------------------
# Aggregate startup verification (Bible Principle 1)
# ---------------------------------------------------------------------------

def verify_system_state(
    repo_dir: pathlib.Path,
    drive_path_fn: Callable[[str], pathlib.Path],
    branch_dev: str,
    git_sha: str,
) -> None:
    """Bible Principle 1: verify system state on every startup.

    Runs three checks:
    1. Uncommitted changes (auto-rescue commit & push)
    2. VERSION file sync with git tags / pyproject.toml / README
    3. Budget remaining (warning thresholds)

    Results are logged to ``logs/events.jsonl``.
    """
    checks: dict = {}
    issues = 0
    drive_logs = drive_path_fn("logs")

    checks["uncommitted_changes"], n = check_uncommitted_changes(repo_dir, branch_dev)
    issues += n

    checks["version_sync"], n = check_version_sync(repo_dir)
    issues += n

    checks["budget"], n = check_budget(drive_path_fn)
    issues += n

    event = {
        "ts": utc_now_iso(),
        "type": "startup_verification",
        "checks": checks,
        "issues_count": issues,
        "git_sha": git_sha,
    }
    append_jsonl(drive_logs / "events.jsonl", event)

    if issues > 0:
        log.warning(f"Startup verification found {issues} issue(s): {checks}")
