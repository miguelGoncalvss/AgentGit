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

    assert!(conflicts.is_empty());
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
    let (count1, conflicts) = engine.materialize().unwrap();
    assert_eq!(conflicts.len(), 0);
    assert_eq!(count1, 1);

    // Second materialization without changes must write 0 files
    let (count2, _) = engine.materialize().unwrap();
    assert_eq!(count2, 0);

    let content = fs::read_to_string(dir.path().join("counter.txt")).unwrap();
    assert_eq!(content, "version 2\n");
}

#[test]
fn test_rust_journal_ignores_tmp_files() {
    let dir = tempdir().unwrap();
    let engine = AgGitEngine::init(dir.path()).unwrap();

    let mut f = HashMap::new();
    f.insert("test.txt".to_string(), b"published".to_vec());
    let _tx = engine.commit("agent_1", None, "Normal commit", Some(f), None, None).unwrap();

    // Inject temporary file with .tmp extension
    let journal_dir = dir.path().join(".aggit").join("journal");
    let tmp_file = journal_dir.join(".tmp_tx_abandoned.tmp");
    fs::write(tmp_file, b"in-flight incomplete write").unwrap();

    let all_txs = engine.journal.get_all_transactions().unwrap();
    // Only the published commit should be seen
    assert_eq!(all_txs.len(), 1);
}

#[test]
fn test_rust_binary_conflict() {
    let base = b"\x00\x01\x02";
    let a = b"\x00\x01\xAA";
    let b = b"\x00\x01\xBB";

    let (_merged, conflicts) = SemanticMerger::merge_files(
        "image.bin",
        Some(base),
        Some(a),
        Some(b),
        "A",
        "B",
    );

    assert_eq!(conflicts.len(), 1);
    assert_eq!(conflicts[0].conflict_type, "binary");
}
