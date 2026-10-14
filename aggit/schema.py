"""
Schema definitions and serialization contracts for AgGit transactions and change records.
Supports schema_version=2 while transparently upgrading legacy schema_version=1 records.
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
import uuid

CURRENT_SCHEMA_VERSION = 2

@dataclass
class ChangeRecord:
    action: str  # "add", "modify", "delete"
    blob_sha: Optional[str] = None  # None for delete
    old_sha: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {"action": self.action}
        if self.blob_sha is not None:
            d["blob_sha"] = self.blob_sha
        if self.old_sha is not None:
            d["old_sha"] = self.old_sha
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChangeRecord":
        return cls(
            action=data.get("action", "modify"),
            blob_sha=data.get("blob_sha"),
            old_sha=data.get("old_sha")
        )

@dataclass
class TransactionRecord:
    tx_id: str
    agent_id: str
    session_id: str
    message: str
    timestamp: str
    timestamp_ns: int
    parents: List[str] = field(default_factory=list)
    base_revision: Optional[str] = None
    changes: Dict[str, ChangeRecord] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = CURRENT_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tx_id": self.tx_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "base_revision": self.base_revision,
            "parents": self.parents,
            "timestamp": self.timestamp,
            "timestamp_ns": self.timestamp_ns,
            "message": self.message,
            "changes": {k: v.to_dict() for k, v in self.changes.items()},
            # Keep legacy "files" field for backward compatibility with external readers
            "files": {k: v.blob_sha for k, v in self.changes.items() if v.blob_sha is not None},
            "metadata": self.metadata
        }

    @classmethod
    def create(
        cls,
        agent_id: str,
        message: str,
        changes: Dict[str, ChangeRecord],
        parents: Optional[List[str]] = None,
        base_revision: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> "TransactionRecord":
        now = datetime.now(timezone.utc)
        ts_ns = int(now.timestamp() * 1_000_000_000)
        return cls(
            tx_id=str(uuid.uuid4()),
            agent_id=agent_id,
            session_id=session_id or agent_id,
            message=message,
            timestamp=now.isoformat(),
            timestamp_ns=ts_ns,
            parents=parents or [],
            base_revision=base_revision,
            changes=changes,
            metadata=metadata or {},
            schema_version=CURRENT_SCHEMA_VERSION
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TransactionRecord":
        version = data.get("schema_version", 1)
        tx_id = data.get("tx_id", "")
        agent_id = data.get("agent_id", "unknown")
        session_id = data.get("session_id", agent_id)
        message = data.get("message", "")
        parents = data.get("parents", [])
        base_revision = data.get("base_revision")
        metadata = data.get("metadata", {})

        # Timestamps
        timestamp_ns = data.get("timestamp_ns", 0)
        timestamp_val = data.get("timestamp")
        if isinstance(timestamp_val, (int, float)):
            # Legacy float seconds
            dt = datetime.fromtimestamp(timestamp_val, tz=timezone.utc)
            timestamp_str = dt.isoformat()
            if not timestamp_ns:
                timestamp_ns = int(timestamp_val * 1_000_000_000)
        elif isinstance(timestamp_val, str):
            timestamp_str = timestamp_val
        else:
            timestamp_str = datetime.now(timezone.utc).isoformat()

        changes: Dict[str, ChangeRecord] = {}
        if "changes" in data and isinstance(data["changes"], dict):
            for path, cdata in data["changes"].items():
                if isinstance(cdata, dict):
                    changes[path] = ChangeRecord.from_dict(cdata)
                elif isinstance(cdata, str):
                    # In case of shorthand path -> sha
                    changes[path] = ChangeRecord(action="modify", blob_sha=cdata)
        elif "files" in data and isinstance(data["files"], dict):
            # Legacy v1 format: files is {path: sha}
            for path, sha in data["files"].items():
                changes[path] = ChangeRecord(action="modify", blob_sha=sha)

        return cls(
            tx_id=tx_id,
            agent_id=agent_id,
            session_id=session_id,
            message=message,
            timestamp=timestamp_str,
            timestamp_ns=timestamp_ns,
            parents=parents,
            base_revision=base_revision,
            changes=changes,
            metadata=metadata,
            schema_version=version
        )
