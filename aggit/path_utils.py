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

def is_path_contained(target_path: str, base_dir: str) -> bool:
    """
    Checks whether target_path is strictly within or equal to base_dir,
    accounting for symlinks, junctions, case-insensitivity on Windows,
    and cross-drive paths.
    """
    try:
        real_t = os.path.realpath(os.path.abspath(target_path))
        real_b = os.path.realpath(os.path.abspath(base_dir))
        norm_t = os.path.normcase(real_t)
        norm_b = os.path.normcase(real_b)
        return os.path.commonpath([norm_b, norm_t]) == norm_b
    except (ValueError, OSError):
        return False

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

    # Double-check lexical and canonical path stays within repo_dir
    abs_repo = os.path.abspath(repo_dir)
    target_abs = os.path.abspath(os.path.join(abs_repo, normalized))

    if not is_path_contained(target_abs, abs_repo):
        raise PathSecurityError(f"Path resolves outside repository: '{rel_path}'")

    # Check for intermediate symlink / junction escapes
    curr = target_abs
    base_norm = os.path.normcase(abs_repo)
    while True:
        if os.path.islink(curr):
            real_parent = os.path.realpath(curr)
            if not is_path_contained(real_parent, abs_repo):
                raise PathSecurityError(f"Path escapes repository via symlink: '{curr}' -> '{real_parent}'")
        parent = os.path.dirname(curr)
        if parent == curr or os.path.normcase(os.path.abspath(curr)) == base_norm:
            break
        curr = parent

    return normalized
