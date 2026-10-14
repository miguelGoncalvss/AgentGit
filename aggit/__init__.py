"""
AgGit: Lock-Free, Multi-Agent Native Version Control System
"""

from .storage import CASStorage
from .journal import Journal, JournalCorruptionError
from .staging import StagingArea
from .semantic_merge import SemanticMerger, MergeConflict
from .schema import TransactionRecord, ChangeRecord
from .path_utils import sanitize_repo_path, PathSecurityError
from .engine import AgGitEngine
from .git_bridge import GitBridge, ExportPlan, ExportResult

__version__ = "2.0.0"
__all__ = [
    "CASStorage",
    "Journal",
    "JournalCorruptionError",
    "StagingArea",
    "SemanticMerger",
    "MergeConflict",
    "TransactionRecord",
    "ChangeRecord",
    "sanitize_repo_path",
    "PathSecurityError",
    "AgGitEngine",
    "GitBridge",
    "ExportPlan",
    "ExportResult",
]
