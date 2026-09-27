"""
GitHub-backed persistence for the Felbry KPI system.

Streamlit Community Cloud's filesystem is EPHEMERAL: every time the app
reboots (a new deploy, a weekly inactivity sleep/wake cycle, a crash
restart), it starts from a fresh clone of the GitHub repo it's deployed
from. Anything written only to local disk -- an uploaded data file, a
freshly computed KPI JSON -- would be silently lost the next time that
happens.

To make the archive and the KPI output durable on the free tier (no
database, no paid storage), we use the app's own GitHub repo as the
persistence layer: every save (an uploaded file, or a freshly computed
KPI output) is immediately committed and pushed back to the repo, and the
app pulls the latest commit on startup and before "Run Analysis". This
means the repo itself IS the database, and it stays in sync across
however many staff members use the app from wherever.

Requires one secret, set in the Streamlit Cloud app's Settings -> Secrets
(never committed to the repo):

    GITHUB_TOKEN = "github_pat_..."   # fine-grained token, Contents: Read & write,
                                        # scoped to only this one repository

The repo slug ("org/name") and branch are read from st.secrets too (see
.streamlit/secrets.toml.example), or fall back to environment variables of
the same name for local testing outside Streamlit.
"""
from __future__ import annotations

import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class GitSyncError(Exception):
    """Raised for a sync problem a non-technical staff member needs to see
    explained in plain language, not a traceback."""


def _get_secret(name: str, default: str | None = None) -> str | None:
    try:
        import streamlit as st
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name, default)


def _run(args: list[str], cwd: str = REPO_ROOT) -> str:
    result = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise GitSyncError(
            f"Git command failed ({' '.join(args)}):\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


def is_git_repo() -> bool:
    return os.path.isdir(os.path.join(REPO_ROOT, ".git"))


def sync_configured() -> bool:
    """True once a GITHUB_TOKEN secret and repo slug are both set. When this
    is False, the app should still work for a single local/dev session, it
    just won't persist across reboots -- callers should show a clear warning
    banner rather than fail."""
    return bool(_get_secret("GITHUB_TOKEN")) and bool(_get_secret("GITHUB_REPO"))


def _authed_remote_url() -> str:
    token = _get_secret("GITHUB_TOKEN")
    repo = _get_secret("GITHUB_REPO")  # e.g. "felbry-college/kpi-system"
    if not token or not repo:
        raise GitSyncError(
            "GitHub sync isn't configured yet (missing GITHUB_TOKEN / GITHUB_REPO "
            "in the app's Secrets). Ask whoever set up the app to add them -- "
            "see the README's 'Connecting persistent storage' section."
        )
    return f"https://x-access-token:{token}@github.com/{repo}.git"


def _ensure_identity():
    # A commit needs SOME author identity; this doesn't need to be a real
    # person since every commit message already says what changed and,
    # where relevant, which signed-in viewer triggered it.
    try:
        _run(["config", "user.email"])
    except GitSyncError:
        _run(["config", "user.email", "kpi-system@felbry.local"])
        _run(["config", "user.name", "Felbry KPI System"])


def pull_latest() -> None:
    """Fetches and fast-forwards to the latest commit from GitHub, so this
    session sees any files a *different* staff session has already
    archived. Safe to call often -- it's a no-op if already up to date."""
    if not sync_configured():
        return
    branch = _get_secret("GITHUB_BRANCH", "main")
    remote = _authed_remote_url()
    _run(["fetch", remote, branch])
    # Reset the tracked data directories to match the remote exactly, but
    # never touch a staff member's not-yet-committed work outside them --
    # in practice the app only ever writes inside data_archive/, so a plain
    # fast-forward merge is enough and keeps this simple and predictable.
    _run(["merge", "--ff-only", "FETCH_HEAD"])


def commit_and_push(paths: list[str], message: str) -> bool:
    """Stages exactly the given paths (relative to the repo root), commits,
    and pushes. Returns False (and does nothing) if there's nothing new to
    commit -- e.g. re-running analysis with no new data produced an
    identical KPI file. Raises GitSyncError with a plain-language message
    on any real failure."""
    if not sync_configured():
        raise GitSyncError(
            "This change was saved on the server but NOT backed up to permanent "
            "storage yet, because GitHub sync isn't configured. It will be lost "
            "the next time the app restarts. Ask whoever set up the app to add "
            "the GITHUB_TOKEN secret (see the README)."
        )
    _ensure_identity()
    _run(["add"] + paths)
    status = _run(["status", "--porcelain"] + paths)
    if not status:
        return False  # nothing changed -- not an error
    _run(["commit", "-m", message])
    remote = _authed_remote_url()
    branch = _get_secret("GITHUB_BRANCH", "main")
    try:
        _run(["push", remote, f"HEAD:{branch}"])
    except GitSyncError:
        # Someone else (a different staff session) likely pushed in between
        # our last pull and now. Rebase our one new commit on top of theirs
        # and try exactly once more before giving up -- this is the normal,
        # expected case for two people using the app at the same time, not
        # a real error, so it shouldn't need a human to intervene.
        _run(["fetch", remote, branch])
        _run(["rebase", "FETCH_HEAD"])
        _run(["push", remote, f"HEAD:{branch}"])
    return True
