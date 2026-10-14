use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use flate2::read::ZlibDecoder;
use flate2::write::ZlibEncoder;
use flate2::Compression;
use sha2::{Digest, Sha256};
use memmap2::Mmap;

pub struct CASStorage {
    objects_dir: PathBuf,
}

impl CASStorage {
    pub fn new<P: AsRef<Path>>(repo_dir: P) -> Self {
        let objects_dir = repo_dir.as_ref().join(".aggit").join("objects");
        fs::create_dir_all(&objects_dir).expect("Failed to create objects directory");
        Self { objects_dir }
    }

    /// Computes SHA-256 and compresses payload
    fn hash_and_compress(obj_type: &str, data: &[u8]) -> (String, Vec<u8>) {
        let header = format!("{} {}\0", obj_type, data.len());
        let mut hasher = Sha256::new();
        hasher.update(header.as_bytes());
        hasher.update(data);
        let sha = format!("{:x}", hasher.finalize());

        let mut encoder = ZlibEncoder::new(Vec::new(), Compression::fast());
        encoder.write_all(header.as_bytes()).unwrap();
        encoder.write_all(data).unwrap();
        let compressed = encoder.finish().unwrap();

        (sha, compressed)
    }

    /// Stores raw bytes in CAS atomically
    pub fn write_object(&self, obj_type: &str, data: &[u8]) -> std::io::Result<String> {
        let (sha, compressed) = Self::hash_and_compress(obj_type, data);
        let shard = &sha[..2];
        let rest = &sha[2..];
        let shard_dir = self.objects_dir.join(shard);
        fs::create_dir_all(&shard_dir)?;

        let final_path = shard_dir.join(rest);
        if final_path.exists() {
            return Ok(sha);
        }

        let tmp_path = shard_dir.join(format!(".tmp_{}_{}", std::process::id(), rest));
        {
            let mut f = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&tmp_path)?;
            f.write_all(&compressed)?;
            f.sync_all()?;
        }

        // Atomic rename
        if let Err(_) = fs::rename(&tmp_path, &final_path) {
            let _ = fs::remove_file(&tmp_path);
        }

        Ok(sha)
    }

    /// Reads object from CAS using Memory-Mapped I/O for zero-copy streaming
    pub fn read_object(&self, sha: &str) -> std::io::Result<(String, Vec<u8>)> {
        let shard = &sha[..2];
        let rest = &sha[2..];
        let obj_path = self.objects_dir.join(shard).join(rest);

        let file = File::open(&obj_path)?;
        // Memory-map the object file directly from kernel memory
        let mmap = unsafe { Mmap::map(&file)? };

        let mut decoder = ZlibDecoder::new(&mmap[..]);
        let mut decompressed = Vec::new();
        decoder.read_to_end(&mut decompressed)?;

        let null_pos = decompressed
            .iter()
            .position(|&b| b == 0)
            .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidData, "Invalid header"))?;

        let header_str = std::str::from_utf8(&decompressed[..null_pos])
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;

        let parts: Vec<&str> = header_str.split(' ').collect();
        let obj_type = parts[0].to_string();
        let payload = decompressed[null_pos + 1..].to_vec();

        Ok((obj_type, payload))
    }

    pub fn store_blob(&self, data: &[u8]) -> std::io::Result<String> {
        self.write_object("blob", data)
    }

    pub fn get_blob(&self, sha: &str) -> std::io::Result<Vec<u8>> {
        let (obj_type, payload) = self.read_object(sha)?;
        if obj_type != "blob" {
            return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "Not a blob"));
        }
        Ok(payload)
    }
}
