use std::collections::HashMap;
use std::fs;
use tempfile::tempdir;

use aggit::storage::CASStorage;
use aggit::staging::StagingArea;
use aggit::semantic::SemanticMerger;
use aggit::path_utils::sanitize_repo_path;
use aggit::engine::AgGitEngine;

#[test]
fn test_rust_path_sanitization() {
    let dir = tempdir().unwrap();
    let root = dir.path();

    assert_eq!(sanitize_repo_path(root, "src/main.rs").unwrap(), "src/main.rs");
    assert_eq!(sanitize_repo_path(root, "docs/architecture.md").unwrap(), "docs/architecture.md");

    // Traversal rejection
    assert!(sanitize_repo_path(root, "../outside.rs").is_err());
    assert!(sanitize_repo_path(root, "src/../../outside.rs").is_err());

    // Absolute / Drive rejection
    assert!(sanitize_repo_path(root, "/etc/passwd").is_err());
    assert!(sanitize_repo_path(root, "C:/Windows/calc.exe").is_err());

    // Reserved Windows device names
    assert!(sanitize_repo_path(root, "con.txt").is_err());
    assert!(sanitize_repo_path(root, "src/nul").is_err());

    // Control directories
    assert!(sanitize_repo_path(root, ".git/config").is_err());
    assert!(sanitize_repo_path(root, ".aggit/journal/tamper.json").is_err());
}

#[test]
fn test_rust_cas_storage_integrity() {
    let dir = tempdir().unwrap();
    let cas = CASStorage::new(dir.path());

    let payload = b"AgGit Rust CAS Content Integrity Test";
    let sha = cas.store_blob(payload).unwrap();
    assert_eq!(sha.len(), 64);

    let retrieved = cas.get_blob(&sha).unwrap();
    assert_eq!(retrieved, payload);

    // Repeated write must return same sha
    let sha2 = cas.store_blob(payload).unwrap();
    assert_eq!(sha, sha2);

    // Corrupt object on disk and verify error
    let shard = &sha[..2];
    let rest = &sha[2..];
    let obj_path = dir.path().join(".aggit").join("objects").join(shard).join(rest);
    fs::write(&obj_path, b"corrupted random data").unwrap();

    assert!(cas.get_blob(&sha).is_err());
}

#[test]
fn test_rust_json_merge_independent_additions() {
    let base = b"{}";
    let a = b"{\"porta\": 3000}";
    let b = b"{\"debug\": true}";

    let (merged_opt, conflicts) = SemanticMerger::merge_files(
        "config.json",
        Some(base),
        Some(a),
        Some(b),
        "A",
        "B",
    );

    assert_eq!(conflicts.len(), 0);
    assert!(merged_opt.is_some());

    let merged_bytes = merged_opt.unwrap();
    let parsed: serde_json::Value = serde_json::from_slice(&merged_bytes).unwrap();
    assert_eq!(parsed["porta"], 3000);
    assert_eq!(parsed["debug"], true);
}

#[test]
fn test_rust_staging_batch_and_generation() {
    let dir = tempdir().unwrap();
    let storage = CASStorage::new(dir.path());
    let staging = StagingArea::new(dir.path());

    let mut batch = HashMap::new();
    batch.insert("file1.txt".to_string(), Some(b"content 1".to_vec()));
    batch.insert("file2.txt".to_string(), Some(b"content 2".to_vec()));

    let res = staging.stage_batch(&storage, "agent_1", None, Some(batch), None, None).unwrap();
    assert_eq!(res.len(), 2);

    // Stage deletion
    staging.stage_delete("agent_1", "file1.txt", None).unwrap();
    let staged = staging.get_staged_changes("agent_1", None);
    assert_eq!(staged["file1.txt"].action, aggit::schema::ChangeAction::Delete);
}

#[test]
fn test_rust_incremental_checkpoints_and_materialization() {
    let dir = tempdir().unwrap();
    let engine = AgGitEngine::init(dir.path()).unwrap();

    let mut f1 = HashMap::new();
    f1.insert("counter.txt".to_string(), b"version 1\n".to_vec());
    let tx1 = engine.commit("agent_1", None, "Commit 1", Some(f1), None, None).unwrap();

    let mut f2 = HashMap::new();
    f2.insert("counter.txt".to_string(), b"version 2\n".to_vec());
    let _tx2 = engine.commit("agent_2", None, "Commit 2", Some(f2), Some(tx1.tx_id.clone()), None).unwrap();

    // Materialize
    let (count1, conflicts) = engine.materialize(false).unwrap();
    assert_eq!(conflicts.len(), 0);
    assert_eq!(count1, 1);

    // Second materialization without changes must write 0 files
    let (count2, _) = engine.materialize(false).unwrap();
    assert_eq!(count2, 0);

    let content = fs::read_to_string(dir.path().join("counter.txt")).unwrap();
    assert_eq!(content, "version 2\n");
}

#[test]
fn test_rust_conflict_persists_and_resolves_explicitly() {
    let dir = tempdir().unwrap();
    let engine = AgGitEngine::init(dir.path()).unwrap();

    let mut f0 = HashMap::new();
    f0.insert("file.txt".to_string(), b"base\n".to_vec());
    let tx0 = engine.commit("a0", None, "base", Some(f0), None, None).unwrap();
    let _ = engine.reconcile_state(false).unwrap();

    let mut f1 = HashMap::new();
    f1.insert("file.txt".to_string(), b"edit A\n".to_vec());
    let _ = engine.commit("a1", None, "edit A", Some(f1), Some(tx0.tx_id.clone()), None).unwrap();

    let mut f2 = HashMap::new();
    f2.insert("file.txt".to_string(), b"edit B\n".to_vec());
    let _ = engine.commit("a2", None, "edit B", Some(f2), Some(tx0.tx_id.clone()), None).unwrap();

    // Call 1: conflict detected
    let (_, conflicts1) = engine.reconcile_state(false).unwrap();
    assert_eq!(conflicts1.len(), 1);

    // Call 2: conflict MUST STILL PERSIST without explicit resolution!
    let (_, conflicts2) = engine.reconcile_state(false).unwrap();
    assert_eq!(conflicts2.len(), 1);

    // Explicit resolution
    let _ = engine.resolve_conflict("file.txt", Some(b"resolved content\n".to_vec()), "admin", "resolve").unwrap();

    // Call 3: conflict cleared, unified tree updated
    let (files3, conflicts3) = engine.reconcile_state(false).unwrap();
    assert_eq!(conflicts3.len(), 0);
    assert_eq!(files3["file.txt"], b"resolved content\n");
}

#[test]
fn test_rust_materialize_blocks_on_local_edit() {
    let dir = tempdir().unwrap();
    let engine = AgGitEngine::init(dir.path()).unwrap();

    let mut f0 = HashMap::new();
    f0.insert("main.rs".to_string(), b"fn main() {}\n".to_vec());
    let _ = engine.commit("a0", None, "init", Some(f0), None, None).unwrap();
    let _ = engine.materialize(false).unwrap();

    // User locally edits main.rs on disk
    fs::write(dir.path().join("main.rs"), b"// local edit by user\n").unwrap();

    let mut f1 = HashMap::new();
    f1.insert("main.rs".to_string(), b"fn main() { println!(\"v2\"); }\n".to_vec());
    let _ = engine.commit("a0", None, "update", Some(f1), None, None).unwrap();

    // Materialize must fail to prevent overwriting user work
    let mat_res = engine.materialize(false);
    assert!(mat_res.is_err());
}

#[test]
fn test_rust_named_session_end_to_end() {
    let dir = tempdir().unwrap();
    let engine = AgGitEngine::init(dir.path()).unwrap();

    // Stage in named session
    let file_path = dir.path().join("test_session.txt");
    fs::write(&file_path, b"session specific bytes\n").unwrap();

    let sha = engine.staging.stage_file(&engine.storage, "agent_x", "test_session.txt", None, Some("session_special")).unwrap();
    assert_eq!(sha.len(), 64);

    // Commit using same named session
    let tx = engine.commit("agent_x", Some("session_special"), "commit session", None, None, None).unwrap();
    assert_eq!(tx.session_id, "session_special");
    assert!(tx.changes.contains_key("test_session.txt"));

    // Materialize and verify bytes
    let (count, conflicts) = engine.materialize(false).unwrap();
    assert_eq!(conflicts.len(), 0);
    assert_eq!(count, 1);
    let bytes = fs::read(dir.path().join("test_session.txt")).unwrap();
    assert_eq!(bytes, b"session specific bytes\n");
}
