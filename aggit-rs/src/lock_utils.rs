#[cfg(windows)]

pub mod sys {
    use std::fs::File;
    use std::os::windows::io::AsRawHandle;
    use std::path::Path;
    use std::time::{Duration, Instant};

    extern "system" {
        fn LockFileEx(
            hFile: *mut std::ffi::c_void,
            dwFlags: u32,
            dwReserved: u32,
            nNumberOfBytesToLockLow: u32,
            nNumberOfBytesToLockHigh: u32,
            lpOverlapped: *mut std::ffi::c_void,
        ) -> i32;

        fn UnlockFile(
            hFile: *mut std::ffi::c_void,
            dwFileOffsetLow: u32,
            dwFileOffsetHigh: u32,
            nNumberOfBytesToUnlockLow: u32,
            nNumberOfBytesToUnlockHigh: u32,
        ) -> i32;
    }

    const LOCKFILE_EXCLUSIVE_LOCK: u32 = 0x00000002;
    const LOCKFILE_FAIL_IMMEDIATELY: u32 = 0x00000001;

    pub struct FileLock {
        file: File,
    }

    impl FileLock {
        pub fn acquire<P: AsRef<Path>>(path: P, timeout: Duration) -> std::io::Result<Self> {
            let start = Instant::now();
            if let Some(parent) = path.as_ref().parent() {
                let _ = std::fs::create_dir_all(parent);
            }
            loop {
                let file = std::fs::OpenOptions::new()
                    .read(true)
                    .write(true)
                    .create(true)
                    .open(path.as_ref())?;

                let handle = file.as_raw_handle();
                let mut overlapped = [0u8; 32];
                let ret = unsafe {
                    LockFileEx(
                        handle as *mut std::ffi::c_void,
                        LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                        0,
                        1,
                        0,
                        overlapped.as_mut_ptr() as *mut std::ffi::c_void,
                    )
                };
                if ret != 0 {
                    return Ok(Self { file });
                }
                if start.elapsed() >= timeout {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::TimedOut,
                        format!("Lock timeout on {:?}", path.as_ref()),
                    ));
                }
                std::thread::sleep(Duration::from_millis(20));
            }
        }
    }

    impl Drop for FileLock {
        fn drop(&mut self) {
            let handle = self.file.as_raw_handle();
            unsafe {
                UnlockFile(handle as *mut std::ffi::c_void, 0, 0, 1, 0);
            }
        }
    }
}

#[cfg(not(windows))]
pub mod sys {
    use std::fs::File;
    use std::os::unix::io::AsRawFd;
    use std::path::Path;
    use std::time::{Duration, Instant};

    extern "C" {
        fn flock(fd: i32, operation: i32) -> i32;
    }
    const LOCK_EX: i32 = 2;
    const LOCK_NB: i32 = 4;
    const LOCK_UN: i32 = 8;

    pub struct FileLock {
        file: File,
    }

    impl FileLock {
        pub fn acquire<P: AsRef<Path>>(path: P, timeout: Duration) -> std::io::Result<Self> {
            let start = Instant::now();
            if let Some(parent) = path.as_ref().parent() {
                let _ = std::fs::create_dir_all(parent);
            }
            loop {
                let file = std::fs::OpenOptions::new()
                    .read(true)
                    .write(true)
                    .create(true)
                    .open(path.as_ref())?;

                let fd = file.as_raw_fd();
                let ret = unsafe { flock(fd, LOCK_EX | LOCK_NB) };
                if ret == 0 {
                    return Ok(Self { file });
                }
                if start.elapsed() >= timeout {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::TimedOut,
                        format!("Lock timeout on {:?}", path.as_ref()),
                    ));
                }
                std::thread::sleep(Duration::from_millis(20));
            }
        }
    }

    impl Drop for FileLock {
        fn drop(&mut self) {
            let fd = self.file.as_raw_fd();
            unsafe {
                flock(fd, LOCK_UN);
            }
        }
    }
}

pub use sys::FileLock;
