"""Resolve and load the Kalshi RSA private key.

Kalshi's download is sometimes named ``kalshi_private_key.pem`` and sometimes
``kalshi_private_key`` with no extension. Callers pass a configured path
(argument or ``KALSHI_PRIVATE_KEY_PATH``); we also try the sibling filename
and the same two names in the current working directory.

If ``KALSHI_PRIVATE_KEY`` is set to a PEM body, the file is skipped.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Optional, Tuple

from cryptography.hazmat.primitives import serialization

DEFAULT_KEY_NAMES = ("kalshi_private_key.pem", "kalshi_private_key")
DEFAULT_KEY_PATH = DEFAULT_KEY_NAMES[0]


def configured_private_key_path(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    return os.environ.get("KALSHI_PRIVATE_KEY_PATH", DEFAULT_KEY_PATH)


def _sibling_name(path: Path) -> Path:
    if path.suffix.lower() == ".pem":
        return path.with_suffix("")
    return path.with_name(path.name + ".pem")


def iter_private_key_candidates(configured: str) -> List[Path]:
    """Unique candidate paths, configured location first, then CWD defaults."""
    seen: set[str] = set()
    out: List[Path] = []

    def add(path: Path) -> None:
        key = os.path.normcase(os.path.abspath(os.path.normpath(str(path.expanduser()))))
        if key in seen:
            return
        seen.add(key)
        out.append(path.expanduser())

    primary = Path(configured)
    add(primary)
    add(_sibling_name(primary))
    for name in DEFAULT_KEY_NAMES:
        add(Path(name))
    return out


def find_private_key_file(configured: str) -> Optional[Path]:
    """Return the first candidate that is a regular file (not a directory)."""
    for path in iter_private_key_candidates(configured):
        if path.is_file():
            return path
    return None


def normalize_pem_env(raw: str) -> str:
    """Turn a PEM pasted into an env var into real PEM text.

    Railway and other hosts often store a multi-line key as a single line
    with escaped ``\\n``, sometimes wrapped in quotes, and sometimes with
    real newlines or Windows ``\\r\\n``. All of those load.
    """
    pem = (raw or "").strip()
    if len(pem) >= 2 and pem[0] == pem[-1] and pem[0] in {'"', "'"}:
        pem = pem[1:-1].strip()
    pem = (
        pem.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    pem = pem.strip()
    if pem and not pem.endswith("\n"):
        pem += "\n"
    return pem


def load_kalshi_private_key(
    configured: Optional[str] = None,
    pem: Optional[str] = None,
) -> Tuple[Any, str]:
    """Load the RSA key from an explicit PEM, ``KALSHI_PRIVATE_KEY``, or a file.

    An explicit ``pem`` wins over the environment variable so one process can
    open several Kalshi accounts without swapping ``KALSHI_PRIVATE_KEY``.

    Returns:
        (private_key, source) where source is ``explicit``,
        ``KALSHI_PRIVATE_KEY``, or the file path that was read.
    """
    if pem and pem.strip():
        normalized = normalize_pem_env(pem)
        key = serialization.load_pem_private_key(normalized.encode("utf-8"), password=None)
        return key, "explicit"

    pem = os.environ.get("KALSHI_PRIVATE_KEY", "")
    if pem.strip():
        pem = normalize_pem_env(pem)
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
        return key, "KALSHI_PRIVATE_KEY"

    path = configured_private_key_path(configured)
    found = find_private_key_file(path)
    if found is None:
        tried = ", ".join(str(p) for p in iter_private_key_candidates(path))
        raise FileNotFoundError(
            f"Private key file not found (tried: {tried}). "
            "Place kalshi_private_key.pem or kalshi_private_key in the project "
            "root, or set KALSHI_PRIVATE_KEY / KALSHI_PRIVATE_KEY_PATH."
        )

    with open(found, "rb") as handle:
        key = serialization.load_pem_private_key(handle.read(), password=None)
    return key, str(found)
