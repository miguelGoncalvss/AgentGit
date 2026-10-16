# Format Compatibility & Migration Guide

🌐 **Translations / Idiomas:** English | [Português (Brasil)](translations/migration.pt-BR.md) | [Español](translations/migration.es.md)

AgGit is designed to ensure strict backward compatibility with repositories and event streams generated in prior versions.

---

## 1. Event Format Evolution

| Field | Schema v1 (Legacy) | Schema v2 (Current Canonical) |
| :--- | :--- | :--- |
| **Identifier (`tx_id`)** | 8 truncated characters | Full 36-character UUID v4 |
| **File Records** | Flat dictionary `files: {path: sha}` | Structured dictionary `changes: {path: {action, blob_sha, old_sha}}` |
| **Deletions** | Implicit key absence | Explicit action `"action": "delete"` |
| **Base Revision** | None (inherited linear HEAD) | Explicit `base_revision: "uuid"` |
| **Session Identity** | Only `agent_id` | `session_id` decoupled from `agent_id` |
| **Timestamp** | Float seconds | ISO-8601 UTC string + `timestamp_ns` (i64) |

---

## 2. Transparent Migration Mechanism

1. **Transparent Ingestion**:
   When reading events without `"schema_version"` or carrying `"schema_version": 1`, both Python (`TransactionRecord.from_dict`) and Rust (`Transaction::get_effective_changes`) readers automatically transform legacy `files` dictionaries into modification records (`action="modify"`).
2. **Durability of Historic Events**:
   Existing transactions in `.aggit/journal/` are never overwritten or mutated. New commits append records conforming to schema v2.
3. **CAS Blob Compatibility**:
   Object storage format in `.aggit/objects/` is 100% identical across schemas: header `[type] [size]\0` compressed with zlib and addressed by SHA-256.

---

## 3. Checkpoint Evolution & Self-Healing

### 3.1 Legacy Checkpoints (Schema v1)
In schema v1 (`.aggit/checkpoints/latest.json`), checkpoints recorded only `tree` and `processed_tx_ids`. Unresolved conflicts were not persisted as structured entities, creating the risk of lost conflict context upon process restarts.

### 3.2 Upgrading to Checkpoint Schema v2
- **Reader Backward Compatibility**: Loading checkpoints missing the `"pending_conflicts"` key safely initializes an empty list `[]`.
- **Structured Preservation**: Divergences identified during 3-way reconciliation persist with rich context (`path`, `base_sha`, `ours_sha`, `theirs_sha`, `conflict_type`).
- **Journal-Based Self-Healing**: If a checkpoint is corrupted or out of sync, the engine uses the append-only journal (`.aggit/journal/`) as the immutable ground truth. Removing or invalidating the checkpoint causes `aggit reconcile` to recalculate the DAG state, restoring the unified tree and accurately repopulating all pending conflicts.
