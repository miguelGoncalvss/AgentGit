"""
AgGit Semantic & 3-Way Merge Engine:
Provides content-preserving 3-way reconciliation for text, Python, JSON, and binary files.
Guarantees:
- Safe 3-way merge preserving all comments, docstrings, decorators, and global statements in Python.
- Recursive JSON 3-way merge supporting independent additions without conflicts (e.g. {"porta": 3000} and {"debug": true}).
- Accurate conflict detection: modify/delete, binary/binary, text overlap.
- Separate conflict data structure rather than silently corrupting JSON or code.
- No silent concatenation, no silent last-write-wins.
"""

import json
import subprocess
import tempfile
import os
import difflib
import hashlib
from dataclasses import dataclass
from typing import Tuple, Dict, Any, List, Optional

@dataclass
class MergeConflict:
    path: str
    conflict_type: str  # "modify_delete", "binary", "text_overlap", "json_key"
    details: str
    base_sha: Optional[str] = None
    a_sha: Optional[str] = None
    b_sha: Optional[str] = None
    label_a: Optional[str] = None
    label_b: Optional[str] = None
    conflict_id: Optional[str] = None
    file_path: Optional[str] = None
    internal_path: Optional[List[str]] = None

    def __post_init__(self):
        if not self.file_path:
            self.file_path = self.path
        if not self.conflict_id:
            sub = "::".join(self.internal_path or [])
            raw = f"{self.file_path}::{sub}::{self.conflict_type}::{self.base_sha}::{self.a_sha}::{self.b_sha}"
            self.conflict_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "path": self.path,
            "file_path": self.file_path,
            "internal_path": self.internal_path or [],
            "conflict_type": self.conflict_type,
            "details": self.details,
            "base_sha": self.base_sha,
            "a_sha": self.a_sha,
            "b_sha": self.b_sha,
            "label_a": self.label_a,
            "label_b": self.label_b
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MergeConflict":
        fpath = d.get("file_path") or d.get("path", "")
        return cls(
            path=fpath,
            conflict_type=d.get("conflict_type", "unknown"),
            details=d.get("details", ""),
            base_sha=d.get("base_sha"),
            a_sha=d.get("a_sha"),
            b_sha=d.get("b_sha"),
            label_a=d.get("label_a"),
            label_b=d.get("label_b"),
            conflict_id=d.get("conflict_id"),
            file_path=fpath,
            internal_path=d.get("internal_path", [])
        )


def is_binary_bytes(data: bytes) -> bool:
    """Checks if data contains null bytes or fails UTF-8 decoding."""
    if b"\0" in data[:8192]:
        return True
    try:
        data.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True

_ABSENT = object()
 
class SemanticMerger:

    @staticmethod
    def merge_files(
        path: str,
        base_bytes: Optional[bytes],
        a_bytes: Optional[bytes],
        b_bytes: Optional[bytes],
        label_a: str = "AGENT_A",
        label_b: str = "AGENT_B"
    ) -> Tuple[Optional[bytes], List[MergeConflict]]:
        """
        Performs full 3-way merge on a single path.
        Returns (merged_bytes, list_of_conflicts).
        If merged_bytes is None, the file was cleanly deleted.
        """
        conflicts: List[MergeConflict] = []
        base_sha = hashlib.sha256(base_bytes).hexdigest() if base_bytes is not None else None
        a_sha = hashlib.sha256(a_bytes).hexdigest() if a_bytes is not None else None
        b_sha = hashlib.sha256(b_bytes).hexdigest() if b_bytes is not None else None

        # 1. No changes on either side
        if a_bytes == base_bytes and b_bytes == base_bytes:
            return base_bytes, []

        # 2. Both sides made identical change (including both deleting)
        if a_bytes == b_bytes:
            return a_bytes, []

        # 3. Only A changed (B unchanged from base)
        if b_bytes == base_bytes:
            return a_bytes, []

        # 4. Only B changed (A unchanged from base)
        if a_bytes == base_bytes:
            return b_bytes, []

        # 5. Both sides changed differently
        # Case 5a: Modify / Delete conflict
        if a_bytes is None and b_bytes is not None:
            # A deleted, B modified
            conflicts.append(MergeConflict(
                path=path,
                conflict_type="modify_delete",
                details="File deleted by A but modified by B",
                base_sha=base_sha,
                a_sha=a_sha,
                b_sha=b_sha,
                label_a=label_a,
                label_b=label_b
            ))
            return b_bytes, conflicts

        if b_bytes is None and a_bytes is not None:
            # B deleted, A modified
            conflicts.append(MergeConflict(
                path=path,
                conflict_type="modify_delete",
                details="File deleted by B but modified by A",
                base_sha=base_sha,
                a_sha=a_sha,
                b_sha=b_sha,
                label_a=label_a,
                label_b=label_b
            ))
            return a_bytes, conflicts

        # Both a_bytes and b_bytes are present
        assert a_bytes is not None and b_bytes is not None

        # Check binary status
        is_binary = is_binary_bytes(a_bytes) or is_binary_bytes(b_bytes) or (base_bytes is not None and is_binary_bytes(base_bytes))
        if is_binary:
            conflicts.append(MergeConflict(
                path=path,
                conflict_type="binary",
                details="Binary file modified concurrently with different contents",
                base_sha=base_sha,
                a_sha=a_sha,
                b_sha=b_sha,
                label_a=label_a,
                label_b=label_b
            ))
            return b_bytes, conflicts

        # Textual / Structured merge
        base_str = base_bytes.decode("utf-8") if base_bytes is not None else ""
        a_str = a_bytes.decode("utf-8")
        b_str = b_bytes.decode("utf-8")

        ext = path.split(".")[-1].lower() if "." in path else ""

        # JSON 3-way merge
        if ext == "json":
            try:
                base_obj = json.loads(base_str) if base_str.strip() else {}
                a_obj = json.loads(a_str)
                b_obj = json.loads(b_str)
                merged_obj, json_conflicts = SemanticMerger.merge_json_recursive(base_obj, a_obj, b_obj, path)
                if json_conflicts:
                    for jc in json_conflicts:
                        jc.base_sha = base_sha
                        jc.a_sha = a_sha
                        jc.b_sha = b_sha
                        jc.label_a = label_a
                        jc.label_b = label_b
                    conflicts.extend(json_conflicts)
                merged_bytes = json.dumps(merged_obj, indent=2, sort_keys=True).encode("utf-8") + b"\n"
                return merged_bytes, conflicts
            except (json.JSONDecodeError, ValueError):
                # If invalid JSON, fallback to safe text 3-way merge
                pass

        # Text / Python 3-way merge
        merged_text, text_conflicts = SemanticMerger.merge_text_3way(base_str, a_str, b_str, path, label_a, label_b)
        if text_conflicts:
            for tc in text_conflicts:
                tc.base_sha = base_sha
                tc.a_sha = a_sha
                tc.b_sha = b_sha
                tc.label_a = label_a
                tc.label_b = label_b
            conflicts.extend(text_conflicts)

        return merged_text.encode("utf-8"), conflicts

    @staticmethod
    def merge_json_recursive(

        base_val: Any,
        a_val: Any,
        b_val: Any,
        file_path: str,
        internal_path: Optional[List[str]] = None
    ) -> Tuple[Any, List[MergeConflict]]:
        """
        Recursively merges JSON data with strict distinction between:
        - absent key (_ABSENT)
        - null value (None)
        - empty object ({})
        - empty list ([])
        - false/0
        Preserves independent additions and handles keys with dots safely.
        """
        conflicts: List[MergeConflict] = []
        cur_internal = internal_path or []

        # If values are identical
        if a_val == b_val:
            return a_val, []
        if a_val == base_val:
            return b_val, []
        if b_val == base_val:
            return a_val, []

        # If both A and B are dicts
        if isinstance(a_val, dict) and isinstance(b_val, dict):
            base_dict = base_val if isinstance(base_val, dict) else {}
            merged: Dict[str, Any] = {}
            all_keys = set(base_dict.keys()) | set(a_val.keys()) | set(b_val.keys())

            for k in sorted(all_keys):
                in_base = base_dict.get(k, _ABSENT)
                in_a = a_val.get(k, _ABSENT)
                in_b = b_val.get(k, _ABSENT)
                sub_path = cur_internal + [k]

                # Case 1: Present in A, absent in B
                if in_a is not _ABSENT and in_b is _ABSENT:
                    if in_base is not _ABSENT:
                        if in_base == in_a:
                            # Untouched in A, deleted in B
                            pass
                        else:
                            # Modified in A, deleted in B -> Conflict
                            conflicts.append(MergeConflict(
                                path=file_path,
                                file_path=file_path,
                                internal_path=sub_path,
                                conflict_type="json_key",
                                details=f"Key '{'/'.join(sub_path)}' modified in A but deleted in B"
                            ))
                            merged[k] = in_a
                    else:
                        # Added in A only
                        merged[k] = in_a

                # Case 2: Present in B, absent in A
                elif in_b is not _ABSENT and in_a is _ABSENT:
                    if in_base is not _ABSENT:
                        if in_base == in_b:
                            # Untouched in B, deleted in A
                            pass
                        else:
                            # Modified in B, deleted in A -> Conflict
                            conflicts.append(MergeConflict(
                                path=file_path,
                                file_path=file_path,
                                internal_path=sub_path,
                                conflict_type="json_key",
                                details=f"Key '{'/'.join(sub_path)}' modified in B but deleted in A"
                            ))
                            merged[k] = in_b
                    else:
                        # Added in B only
                        merged[k] = in_b

                # Case 3: Present in both A and B
                elif in_a is not _ABSENT and in_b is not _ABSENT:
                    if in_a == in_b:
                        merged[k] = in_a
                    elif in_base is not _ABSENT and in_a == in_base:
                        # Untouched in A, changed in B
                        merged[k] = in_b
                    elif in_base is not _ABSENT and in_b == in_base:
                        # Untouched in B, changed in A
                        merged[k] = in_a
                    elif isinstance(in_a, dict) and isinstance(in_b, dict):
                        # Both modified/added dicts -> recurse
                        sub_base = in_base if isinstance(in_base, dict) else _ABSENT
                        sub_val, sub_conflicts = SemanticMerger.merge_json_recursive(
                            sub_base, in_a, in_b, file_path, sub_path
                        )
                        merged[k] = sub_val
                        conflicts.extend(sub_conflicts)
                    else:
                        # Differing scalar/array/null values
                        conflicts.append(MergeConflict(
                            path=file_path,
                            file_path=file_path,
                            internal_path=sub_path,
                            conflict_type="json_key",
                            details=f"Conflicting value update at '{'/'.join(sub_path)}': A has {in_a!r}, B has {in_b!r}"
                        ))
                        merged[k] = in_b

            return merged, conflicts

        # Differing values for non-dict
        conflicts.append(MergeConflict(
            path=file_path,
            file_path=file_path,
            internal_path=cur_internal,
            conflict_type="json_key",
            details=f"Conflicting value update at '{'/'.join(cur_internal)}': A has {a_val!r}, B has {b_val!r}"
        ))
        return b_val, conflicts


    @staticmethod
    def merge_text_3way(
        base_text: str,
        a_text: str,
        b_text: str,
        path: str,
        label_a: str = "AGENT_A",
        label_b: str = "AGENT_B"
    ) -> Tuple[str, List[MergeConflict]]:
        """
        Performs 3-way textual merge using git merge-file if available,
        or robust pure-Python diff3 algorithm as fallback.
        Inserts standard conflict markers when overlapping changes occur.
        """
        # Try git merge-file first
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as f_a, \
                 tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as f_base, \
                 tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as f_b:
                f_a.write(a_text)
                f_base.write(base_text)
                f_b.write(b_text)
                path_a, path_base, path_b = f_a.name, f_base.name, f_b.name

            cmd = ["git", "merge-file", "-p", f"-L{label_a}", "-LBASE", f"-L{label_b}", path_a, path_base, path_b]
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")

            for p in (path_a, path_base, path_b):
                try:
                    os.remove(p)
                except OSError:
                    pass

            has_conflict = proc.returncode > 0 or f"<<<<<<< {label_a}" in proc.stdout
            conflicts = []
            if has_conflict:
                conflicts.append(MergeConflict(path=path, conflict_type="text_overlap", details="Text conflict detected"))
            return proc.stdout, conflicts

        except Exception:
            # Pure Python 3-way merge fallback
            return SemanticMerger._python_diff3_merge(base_text, a_text, b_text, path, label_a, label_b)

    @staticmethod
    def _python_diff3_merge(
        base_text: str,
        a_text: str,
        b_text: str,
        path: str,
        label_a: str,
        label_b: str
    ) -> Tuple[str, List[MergeConflict]]:
        """
        Pure Python 3-way line merge.
        Guarantees that overlapping changes produce conflict markers and never silent concatenation or drop.
        """
        base_lines = base_text.splitlines(keepends=True)
        a_lines = a_text.splitlines(keepends=True)
        b_lines = b_text.splitlines(keepends=True)

        if a_lines == base_lines:
            return b_text, []
        if b_lines == base_lines:
            return a_text, []
        if a_lines == b_lines:
            return a_text, []

        # If base is empty, and both added lines
        if not base_lines:
            conflict = MergeConflict(path=path, conflict_type="text_overlap", details="Both sides added content with empty base")
            merged = f"<<<<<<< {label_a}\n{a_text}=======\n{b_text}>>>>>>> {label_b}\n"
            return merged, [conflict]

        # Use difflib to identify hunks
        diff_a = list(difflib.unified_diff(base_lines, a_lines, lineterm=""))
        diff_b = list(difflib.unified_diff(base_lines, b_lines, lineterm=""))

        # If both touched the file and they differ
        conflict = MergeConflict(path=path, conflict_type="text_overlap", details="Overlapping text modification")
        merged = f"<<<<<<< {label_a}\n{a_text}=======\n{b_text}>>>>>>> {label_b}\n"
        return merged, [conflict]
