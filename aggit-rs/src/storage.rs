use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use flate2::read::ZlibDecoder;
use flate2::write::ZlibEncoder;
use flate2::Compression;
use sha2::{Digest, Sha256};
use memmap2::Mmap;

const MAX_DECOMPRESSED_BYTES: u64 = 200 * 1024 * 1024; // 200 MB limit

pub struct CASStorage {
    objects_dir: PathBuf,
}

impl CASStorage {
    pub fn new<P: AsRef<Path>>(repo_dir: P) -> Self {
        let objects_dir = repo_dir.as_ref().join(".aggit").join("objects");
        fs::create_dir_all(&objects_dir).expect("Failed to create objects directory");
        Self { objects_dir }
    }

    /// Computes full payload bytes with header and its SHA-256 hash.
    /// Does NOT perform compression.
    fn compute_hash(obj_type: &str, data: &[u8]) -> (String, Vec<u8>) {
        let header = format!("{} {}\0", obj_type, data.len());
        let mut full_payload = Vec::with_capacity(header.len() + data.len());
        full_payload.extend_from_slice(header.as_bytes());
        full_payload.extend_from_slice(data);

        let mut hasher = Sha256::new();
        hasher.update(&full_payload);
        let sha = format!("{:x}", hasher.finalize());

        (sha, full_payload)
    }

    /// Stores raw bytes in CAS atomically.
    /// Checks existence before compressing to eliminate wasteful compression cycles.
    pub fn write_object(&self, obj_type: &str, data: &[u8]) -> std::io::Result<String> {
        let (sha, full_payload) = Self::compute_hash(obj_type, data);
        let shard = &sha[..2];
        let rest = &sha[2..];
        let shard_dir = self.objects_dir.join(shard);
        fs::create_dir_all(&shard_dir)?;

        let final_path = shard_dir.join(rest);
        // Early return if object is already published
        if final_path.exists() {
            return Ok(sha);
        }

        // Compress only if object is not yet in CAS
        let mut encoder = ZlibEncoder::new(Vec::new(), Compression::fast());
        encoder.write_all(&full_payload)?;
        let compressed = encoder.finish()?;

        // Generate unique temporary path per thread, process and UUID to avoid collisions
        let unique_suffix = format!("{}_{}_{}", std::process::id(), uuid::Uuid::new_v4(), rest);
        let tmp_path = shard_dir.join(format!(".tmp_obj_{}.tmp", unique_suffix));

        {
            let mut f = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&tmp_path)?;
            f.write_all(&compressed)?;
            f.sync_all()?;
        }

        // Atomic rename
        if let Err(e) = fs::rename(&tmp_path, &final_path) {
            let _ = fs::remove_file(&tmp_path);
            if !final_path.exists() {
                return Err(e);
            }
        }

        if !final_path.exists() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::Other,
                format!("Failed to verify published CAS object {}", sha),
            ));
        }

        Ok(sha)
    }

    /// Reads and verifies object from CAS using Memory-Mapped I/O with SHA integrity validation
    pub fn read_object(&self, sha: &str) -> std::io::Result<(String, Vec<u8>)> {
        let clean_sha = sha.trim().to_lowercase();
        if clean_sha.len() != 64 || !clean_sha.chars().all(|c| c.is_ascii_hexdigit()) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                format!("Invalid SHA-256 hash: '{}'", sha),
            ));
        }

        let shard = &clean_sha[..2];
        let rest = &clean_sha[2..];
        let obj_path = self.objects_dir.join(shard).join(rest);

        let file = File::open(&obj_path)?;
        let metadata = file.metadata()?;
        if metadata.len() == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                format!("Corrupt 0-byte CAS object file for {}", clean_sha),
            ));
        }

        // Memory-map the object file safely
        let mmap = unsafe { Mmap::map(&file)? };

        // Decompress with size limit
        let mut decoder = ZlibDecoder::new(&mmap[..]).take(MAX_DECOMPRESSED_BYTES + 1);
        let mut decompressed = Vec::new();
        decoder.read_to_end(&mut decompressed)?;

        if decompressed.len() as u64 > MAX_DECOMPRESSED_BYTES {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("Decompressed object {} exceeds max allowed limit", clean_sha),
            ));
        }

        // Verify cryptographic SHA-256 hash of decompressed bytes
        let mut hasher = Sha256::new();
        hasher.update(&decompressed);
        let calculated_sha = format!("{:x}", hasher.finalize());

        if calculated_sha != clean_sha {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("CAS object corruption: SHA mismatch for {}", clean_sha),
            ));
        }

        let null_pos = decompressed
            .iter()
            .position(|&b| b == 0)
            .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidData, "Missing null header terminator"))?;

        let header_str = std::str::from_utf8(&decompressed[..null_pos])
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;

        let parts: Vec<&str> = header_str.split(' ').collect();
        if parts.len() != 2 {
            return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "Invalid header format"));
        }

        let obj_type = parts[0].to_string();
        let expected_size: usize = parts[1]
            .parse()
            .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidData, "Invalid header size"))?;

        let payload = decompressed[null_pos + 1..].to_vec();
        if payload.len() != expected_size {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("Payload size mismatch: expected {}, got {}", expected_size, payload.len()),
            ));
        }

        Ok((obj_type, payload))
    }

    pub fn store_blob(&self, data: &[u8]) -> std::io::Result<String> {
        self.write_object("blob", data)
    }

    pub fn get_blob(&self, sha: &str) -> std::io::Result<Vec<u8>> {
        let (obj_type, payload) = self.read_object(sha)?;
        if obj_type != "blob" {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("Expected blob object, got {}", obj_type),
            ));
        }
        Ok(payload)
    }
}
