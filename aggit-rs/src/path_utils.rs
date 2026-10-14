use std::path::Path;

const WINDOWS_RESERVED: &[&str] = &[
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
];

pub fn sanitize_repo_path<P: AsRef<Path>>(repo_dir: P, rel_path: &str) -> Result<String, String> {
    if rel_path.trim().is_empty() {
        return Err("Path cannot be empty or whitespace only".to_string());
    }

    if rel_path.contains('\0') {
        return Err("Path contains forbidden null byte".to_string());
    }

    // Disallow Windows drive prefixes or UNC paths
    if rel_path.contains(':') || rel_path.starts_with("\\\\") || rel_path.starts_with("//") {
        return Err(format!("Absolute or drive/UNC paths not allowed: '{}'", rel_path));
    }

    if rel_path.starts_with('/') || rel_path.starts_with('\\') {
        return Err(format!("Absolute paths not allowed: '{}'", rel_path));
    }

    let norm_str = rel_path.replace('\\', "/");
    let parts: Vec<&str> = norm_str.split('/').filter(|s| !s.is_empty()).collect();

    if parts.is_empty() {
        return Err("Path resolves to empty".to_string());
    }

    // Check traversal and reserved device names
    for part in &parts {
        if *part == ".." {
            return Err(format!("Path traversal ('..') detected: '{}'", rel_path));
        }
        let base_stem = part.split('.').next().unwrap_or("").to_uppercase();
        if WINDOWS_RESERVED.contains(&base_stem.as_str()) {
            return Err(format!("Reserved Windows device name forbidden: '{}' in '{}'", part, rel_path));
        }
    }

    if parts[0] == ".git" || parts[0] == ".aggit" {
        return Err(format!("Cannot target internal control directory '{}': '{}'", parts[0], rel_path));
    }

    let joined = parts.join("/");

    // Verify canonical path remains within repo_dir
    let repo_canonical = match repo_dir.as_ref().canonicalize() {
        Ok(p) => p,
        Err(_) => repo_dir.as_ref().to_path_buf(),
    };

    let target_path = repo_canonical.join(&joined);
    // Walk existing parents to detect any symlink / junction escapes
    let curr = if target_path.exists() || target_path.is_symlink() {
        target_path.clone()
    } else {
        let mut p = target_path.parent();
        let mut existing_parent = repo_canonical.clone();
        while let Some(parent) = p {
            if parent.exists() || parent.is_symlink() {
                existing_parent = parent.to_path_buf();
                break;
            }
            p = parent.parent();
        }
        existing_parent
    };

    if let Ok(canon) = curr.canonicalize() {
        if !canon.starts_with(&repo_canonical) {
            return Err(format!("Path resolves outside repository via symlink: '{}'", rel_path));
        }
    }

    Ok(joined)
}
