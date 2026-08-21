"""Deployed-version resolution.

The version is deliberately not stored in the package: it comes from the
release process and is written to a VERSION file next to ``sms_bridge/`` at
deploy time — by the Dockerfile for container images and by the install steps
for host/systemd installs. A plain checkout has no such file and reports
"dev", which is the honest answer for an untagged build.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_VERSION = "dev"


def resolve_version(root: Path | str | None = None) -> str:
    """Return the version recorded at deploy time, or ``"dev"`` when unknown.

    ``root`` is the directory that holds the ``sms_bridge/`` package (the app
    root). It defaults to this package's parent so the lookup does not depend
    on the current working directory. A missing or blank file falls back to
    ``DEFAULT_VERSION`` rather than raising: a version must never take the
    bridge down at startup.
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    try:
        text = (base / "VERSION").read_text().strip()
    except OSError:
        return DEFAULT_VERSION
    return text or DEFAULT_VERSION
