from __future__ import annotations

from pathlib import Path


class PathPolicyError(ValueError):
    pass


def validate_path_under_allowed_roots(
    candidate: str,
    allowed_roots: str,
    *,
    require_file: bool = False,
    require_dir: bool = False,
    max_bytes: int | None = None,
) -> Path:
    path = Path(candidate)
    if "\x00" in candidate:
        raise PathPolicyError("path contains invalid characters")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise PathPolicyError("path does not exist") from exc

    roots = _allowed_roots(allowed_roots)
    if not roots:
        raise PathPolicyError("no allowed roots configured")
    if not any(_is_relative_to(resolved, root) for root in roots):
        raise PathPolicyError("path is outside configured allowed roots")

    if require_file and not resolved.is_file():
        raise PathPolicyError("path must be a file")
    if require_dir and not resolved.is_dir():
        raise PathPolicyError("path must be a directory")
    if max_bytes is not None and resolved.is_file() and resolved.stat().st_size > max_bytes:
        raise PathPolicyError("file exceeds configured size limit")
    return resolved


def _allowed_roots(raw_roots: str) -> list[Path]:
    roots: list[Path] = []
    for item in raw_roots.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            root = Path(value).resolve(strict=True)
        except FileNotFoundError:
            continue
        roots.append(root)
    return roots


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
