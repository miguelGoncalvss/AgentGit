# AgGit Storage Format Specification

🌐 **Translations / Idiomas:** English | [Português (Brasil)](translations/storage-format.pt-BR.md) | [Español](translations/storage-format.es.md)

This document describes the on-disk data structures, schemas, and serialization formats used by AgGit.

---

## 1. Content-Addressable Storage (CAS)

Location: `.aggit/objects/<shard>/<rest>`  
Where `<shard>` represents the first 2 hexadecimal characters of the SHA-256 hash, and `<rest>` corresponds to the remaining 62 characters.

### 1.1 Object Format
Prior to zlib compression, payloads carry the standard Git-compatible header:
```
[type] [size_in_bytes]\0[raw_data]
```
- `type`: UTF-8 string (`blob`, `manifest`, `tree`).
- `size_in_bytes`: Decimal ASCII representation.
- `\0`: Null byte delimiter.
- Compression: zlib level 6 (fast).

---

## 2. Journal Events (Transactions)

Location: `.aggit/journal/<timestamp_ns>_<agent_id>_<tx_id>.json`

### 2.1 Schema Version 2 (Current Canonical)
```json
{
  "schema_version": 2,
  "tx_id": "c9bf9e57-1685-4c89-bafb-ff5af830be8a",
  "agent_id": "agent_backend",
  "session_id": "session_sprint_1",
  "base_revision": "4f2a7b1c-8e99-4d22-921a-bc015f8d9021",
  "parents": [
    "4f2a7b1c-8e99-4d22-921a-bc015f8d9021"
  ],
  "timestamp": "2026-10-14T20:55:00.123456Z",
  "timestamp_ns": 1792011300123456000,
  "message": "feat: add user authentication endpoint",
  "changes": {
    "src/auth.py": {
      "action": "modify",
      "blob_sha": "a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0",
      "old_sha": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    },
    "docs/deprecated.txt": {
      "action": "delete"
    }
  },
  "files": {
    "src/auth.py": "a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0"
  },
  "metadata": {}
}
```

---

## 3. Session-Isolated Staging

Location: `.aggit/staging/<agent_safe_prefix>_<ident_hash>.json`

```json
{
  "generation": 3,
  "agent_id": "agent_backend",
  "session_id": "session_sprint_1",
  "base_revision": "4f2a7b1c-...",
  "entries": {
    "src/auth.py": {
      "action": "modify",
      "blob_sha": "a1b2c3d4...",
      "old_sha": "01234567..."
    }
  }
}
```

---

## 4. Reconciliation Checkpoints

Location: `.aggit/checkpoints/latest.json`

### 4.1 Schema Version 2 (Current Canonical)
```json
{
  "version": 2,
  "tree": {
    "src/auth.py": "a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0",
    "package.json": "887766554433221100ffeeddccbbaa99887766554433221100ffeeddccbbaa99"
  },
  "processed_tx_ids": [
    "c9bf9e57-1685-4c89-bafb-ff5af830be8a",
    "f2a7b1c8-e994-d229-21ab-c015f8d90210"
  ],
  "pending_conflicts": [
    {
      "path": "src/config.py",
      "base_sha": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "ours_sha": "a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0",
      "theirs_sha": "f1e2d3c4b5a60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0",
      "ours_tx_id": "c9bf9e57-1685-4c89-bafb-ff5af830be8a",
      "theirs_tx_id": "f2a7b1c8-e994-d229-21ab-c015f8d90210",
      "conflict_type": "content_divergence",
      "description": "Conflict in src/config.py: divergent modifications"
    }
  ]
}
```

---

## 5. Git Export Ledger

Location: `.aggit/git_export_map.json`

Maps AgGit revisions to equivalent commit SHAs on the local Git repository.

### 5.1 Composite Key Structure
To ensure lineage traceability, branch continuity, and multi-branch isolation, ledger keys follow a composite structure:
```
<revision_id>::<git_branch>::<parent_git_commit>
```
Example:
```json
{
  "c9bf9e57-1685-4c89-bafb-ff5af830be8a::feature/agent-sprint::": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
  "d1a2b3c4-5e6f-7a8b-9c0d-1e2f3a4b5c6d::feature/agent-sprint::4b825dc642cb6eb9a060e54bf8d69288fbee4904": "8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b"
}
```

---

## 6. On-Disk Materialization Manifest

Location: `.aggit/materialized_state.json`

Tracks the physical manifest of files written to the working tree during the last successful materialization, enabling 3-state pre-flight protection:
```json
{
  "tracked_files": {
    "src/auth.py": "a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0",
    "package.json": "887766554433221100ffeeddccbbaa99887766554433221100ffeeddccbbaa99"
  },
  "last_reconciled_tx": "c9bf9e57-1685-4c89-bafb-ff5af830be8a"
}
```
