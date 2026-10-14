"""
Path validation and sanitization utilities for AgGit.
Enforces that all paths remain strictly inside the repository root,
disallows path traversal, absolute paths, symlink escapes, reserved device names,
and access to internal metadata directories (.git, .aggit).
"""

import os
from typing import Optional

class PathSecurityError(ValueError):
    """Raised when a path violates repository boundary or security constraints."""
    pass

FORBIDDEN_PREFIXES = {".git", ".aggit"}
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
}

def sanitize_repo_path(repo_dir: str, rel_path: str) -> str:
    """
    Validates and normalizes a repository-relative path.
    Returns normalized path using forward slashes (e.g. 'src/main.py').
    Raises PathSecurityError if the path attempts traversal, is absolute, escapes via symlink,
    or targets forbidden directories/reserved device names.
    """
    if not rel_path or not rel_path.strip():
        raise PathSecurityError("Path cannot be empty or whitespace only")

    # Reject null bytes
    if "\0" in rel_path:
        raise PathSecurityError("Path contains forbidden null byte")

    # Disallow Windows drive prefixes or UNC paths
    if ":" in rel_path or rel_path.startswith("\\\\") or rel_path.startswith("//"):
        raise PathSecurityError(f"Absolute or drive/UNC paths not allowed: '{rel_path}'")

    # Disallow leading slash (POSIX absolute)
    if rel_path.startswith("/") or rel_path.startswith("\\"):
        raise PathSecurityError(f"Absolute paths not allowed: '{rel_path}'")

    # Normalize with standard os.path
    normalized = os.path.normpath(rel_path).replace("\\", "/")

    # Check for path traversal components
    parts = normalized.split("/")
    if any(part == ".." for part in parts):
        raise PathSecurityError(f"Path traversal ('..') detected: '{rel_path}'")

    if parts[0] in FORBIDDEN_PREFIXES:
        raise PathSecurityError(f"Cannot target internal control directory '{parts[0]}': '{rel_path}'")

    # Check for Windows reserved device names in any component
    for part in parts:
        base_stem = part.split(".")[0].upper()
        if base_stem in WINDOWS_RESERVED:
            raise PathSecurityError(f"Reserved Windows device name forbidden: '{part}' in '{rel_path}'")

    # Double-check lexical resolved path stays within repo_dir
    abs_repo = os.path.abspath(repo_dir)
    target_abs = os.path.abspath(os.path.join(abs_repo, normalized))

    try:
        common = os.path.commonpath([abs_repo, target_abs])
        if common != abs_repo:
            raise PathSecurityError(f"Path resolves outside repository: '{rel_path}'")
    except ValueError:
        raise PathSecurityError(f"Path is on a different drive than repository: '{rel_path}'")

    # Check for symlink / junction escapes
    real_repo = os.path.realpath(abs_repo)
    if os.path.exists(target_abs) or os.path.islink(target_abs):
        real_target = os.path.realpath(target_abs)
        try:
            if os.path.commonpath([real_repo, real_target]) != real_repo:
                raise PathSecurityError(f"Path escapes repository via symlink: '{rel_path}' -> '{real_target}'")
        except ValueError:
            raise PathSecurityError(f"Symlink points to different drive: '{rel_path}'")
    else:
        # Check existing parent directories for symlink escape
        curr = os.path.dirname(target_abs)
        while curr and curr != abs_repo and os.path.commonpath([abs_repo, curr]) == abs_repo:
            if os.path.exists(curr) or os.path.islink(curr):
                real_parent = os.path.realpath(curr)
                try:
                    if os.path.commonpath([real_repo, real_parent]) != real_repo:
                        raise PathSecurityError(f"Parent directory escapes repository via symlink: '{curr}' -> '{real_parent}'")
                except ValueError:
                    raise PathSecurityError(f"Parent symlink points to different drive: '{curr}'")
                break
            parent = os.path.dirname(curr)
            if parent == curr:
                break
            curr = parent

    return normalized
