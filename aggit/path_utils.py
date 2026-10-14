"""
Path validation and sanitization utilities for AgGit.
Enforces that all paths remain strictly inside the repository root,
disallows path traversal, absolute paths, and access to internal metadata directories (.git, .aggit).
"""

import os
from typing import Optional

class PathSecurityError(ValueError):
    """Raised when a path violates repository boundary or security constraints."""
    pass

FORBIDDEN_PREFIXES = {".git", ".aggit"}

def sanitize_repo_path(repo_dir: str, rel_path: str) -> str:
    """
    Validates and normalizes a repository-relative path.
    Returns normalized path using forward slashes (e.g. 'src/main.py').
    Raises PathSecurityError if the path attempts traversal, is absolute, or targets forbidden directories.
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

    # Double-check full resolved path stays within repo_dir
    abs_repo = os.path.abspath(repo_dir)
    target_abs = os.path.abspath(os.path.join(abs_repo, normalized))
    
    # Commonpath must equal abs_repo
    try:
        common = os.path.commonpath([abs_repo, target_abs])
        if common != abs_repo:
            raise PathSecurityError(f"Path resolves outside repository: '{rel_path}'")
    except ValueError:
        raise PathSecurityError(f"Path is on a different drive than repository: '{rel_path}'")

    return normalized
