use std::path::Path;

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

    // Check traversal
    for part in &parts {
        if *part == ".." {
            return Err(format!("Path traversal ('..') detected: '{}'", rel_path));
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
    // If target exists or parent exists, verify prefix
    let check_path = if target_path.exists() {
        target_path.canonicalize().unwrap_or(target_path)
    } else if let Some(parent) = target_path.parent() {
        if parent.exists() {
            parent.canonicalize().unwrap_or(parent.to_path_buf()).join(target_path.file_name().unwrap())
        } else {
            target_path
        }
    } else {
        target_path
    };

    if !check_path.starts_with(&repo_canonical) {
        return Err(format!("Path resolves outside repository: '{}'", rel_path));
    }

    Ok(joined)
}
