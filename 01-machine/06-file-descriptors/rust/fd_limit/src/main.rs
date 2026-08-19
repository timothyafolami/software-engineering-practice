// Layer 1 - Same experiment, from Rust's side. std has no getrlimit
// wrapper, and pulling in the `libc` crate for one syscall felt like
// overkill for a teaching script, so this reads /proc/self/limits like the
// Node version does.
use std::fs::{self, File};

fn read_soft_limit() -> String {
    let limits = fs::read_to_string("/proc/self/limits").unwrap_or_default();
    limits
        .lines()
        .find(|l| l.starts_with("Max open files"))
        .unwrap_or("(unknown -- not on Linux?)")
        .trim()
        .to_string()
}

fn main() {
    println!("{}", read_soft_limit());

    let mut fds = Vec::new();
    loop {
        match File::open("/dev/null") {
            Ok(f) => fds.push(f),
            Err(e) => {
                println!(
                    "hit error ('too many open files') after opening {} fds: {}",
                    fds.len(),
                    e
                );
                break;
            }
        }
    }
    let count = fds.len();
    drop(fds);
    println!("closed all {} fds; process is healthy again", count);
}
