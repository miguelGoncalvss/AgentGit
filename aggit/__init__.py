"""
AgGit: Lock-Free, Multi-Agent Native Version Control System
"""

from .storage import CASStorage
from .journal import Journal
from .staging import StagingArea
from .semantic_merge import SemanticMerger
from .engine import AgGitEngine
from .git_bridge import GitBridge

__version__ = "1.0.0"
__all__ = [
    "CASStorage",
    "Journal",
    "StagingArea",
    "SemanticMerger",
    "AgGitEngine",
    "GitBridge",
]
