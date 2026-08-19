//! 7.6 -- Rust: no GC, so RSS is exactly what you asked for, and failure is
//! an abort rather than an Err.
//!
//! WHAT THIS DEMONSTRATES
//!   There is no collector to tune and no heap ceiling to configure.
//!   Allocation goes to the system allocator, RSS is live data plus
//!   fragmentation, and nothing in std reads `memory.max` -- there is no
//!   `available_memory()` to match `available_parallelism()`.
//!
//!   When allocation genuinely fails, the default behaviour is
//!   `handle_alloc_error` -> **abort**, not an `Err`. `Vec::try_reserve`
//!   exists for the code paths that need to survive it, and this program
//!   runs both so you can see the difference:
//!
//!     cargo run --release -- --infallible   Vec::push. Aborts on failure.
//!     cargo run --release -- --try-reserve  try_reserve. Returns an Err.
//!
//!   And then the honest caveat, which is the actual lesson: in a container
//!   under Linux's default overcommit you rarely reach EITHER path. `malloc`
//!   returns a valid pointer, the kernel commits nothing, and the cgroup
//!   charge lands when you first WRITE to the page -- at which point you are
//!   SIGKILLed on a memory store, not inside anyone's error handling.
//!   `try_reserve` protects you from an allocator that says no. The cgroup
//!   never says no; it kills you.
//!
//!   Rust's real contribution here is that it makes the size of your live
//!   set honest. There is no "the GC will get it eventually" to hide behind,
//!   so the number you must size `memory.max` from is a number you can
//!   actually compute.
//!
//! WHAT TO LOOK FOR IN THE OUTPUT
//!   1. RSS tracking allocated bytes almost exactly. Compare that with
//!      python/oom.py (sticky arenas) and java/Oom.java (heap plus
//!      metaspace plus code cache plus stacks).
//!   2. In --try-reserve mode under a container limit: whether the Err ever
//!      arrives before the SIGKILL does. Usually it does not.
//!   3. The Drop impl. It runs on a normal return and on an unwind. It does
//!      not run on SIGKILL, and it does not run on abort either.
//!
//! RUN
//!   docker run --rm --memory=256m -v "$PWD:/w" -w /w rust:1 \
//!     sh -c 'cd /w && cargo run --release'
//!   echo "exit code: $?"      # 137
//!
//!   cargo run --release       # on this Mac: no cgroup, so a self-imposed cap
//!
//! On macOS there is no cgroup memory controller and nothing can OOM-kill
//! this process, so with no limit to read it imposes its own and says so.

use std::env;
use std::fs;

const CHUNK_MB: usize = 8;

// ---------------------------------------------------------------- the kernel

/// Bytes the cgroup will let this container charge, or `None` for no limit.
fn memory_max() -> Option<u64> {
    if let Ok(raw) = fs::read_to_string("/sys/fs/cgroup/memory.max") {
        let raw = raw.trim();
        if raw != "max" {
            return raw.parse().ok();
        }
        return None;
    }
    // v1 spells "unlimited" as a number near 2^63, not as a word.
    let v1: u64 = fs::read_to_string("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        .ok()?
        .trim()
        .parse()
        .ok()?;
    (v1 < 1 << 62).then_some(v1)
}

fn read_or(path: &str, fallback: &str) -> String {
    fs::read_to_string(path)
        .map(|s| s.trim().to_string())
        .unwrap_or_else(|_| fallback.to_string())
}

/// Current RSS in MiB, or `None` where there is no way to ask.
///
/// Linux only, deliberately. Darwin's getrusage reports a PEAK, which is a
/// different question, and answering a different question under the same
/// name is how memory dashboards end up lying. `None` and a printed reason
/// beats a plausible wrong number.
fn rss_mb() -> Option<f64> {
    let status = fs::read_to_string("/proc/self/status").ok()?;
    for line in status.lines() {
        if let Some(rest) = line.strip_prefix("VmRSS:") {
            let kb: f64 = rest.split_whitespace().next()?.parse().ok()?;
            return Some(kb / 1024.0);
        }
    }
    None
}

fn rss_display() -> String {
    rss_mb()
        .map(|v| format!("{v:6.0} MiB"))
        .unwrap_or_else(|| "   n/a   ".to_string())
}

/// A Drop impl, so you can watch it not run.
struct Farewell;

impl Drop for Farewell {
    fn drop(&mut self) {
        println!("  [Drop] returning from main normally -- RSS {}", rss_display());
    }
}

fn main() {
    let _farewell = Farewell;

    let args: Vec<String> = env::args().collect();
    let try_reserve = args.iter().any(|a| a == "--try-reserve");
    let self_limit_mb: usize = args
        .iter()
        .position(|a| a == "--limit-mb")
        .and_then(|i| args.get(i + 1))
        .and_then(|v| v.parse().ok())
        .unwrap_or(512);

    let limit = memory_max();

    println!("7.6 -- memory: Rust");
    println!(
        "  runtime      : rustc std, no dependencies, on {}/{}",
        env::consts::OS,
        env::consts::ARCH
    );
    println!(
        "  memory.max   : {}",
        limit
            .map(|v| format!("{} MiB", v / (1 << 20)))
            .unwrap_or_else(|| "no limit / no cgroupfs".into())
    );
    println!(
        "  memory.high  : {}   <- degrades instead of killing; no Compose key",
        read_or("/sys/fs/cgroup/memory.high", "unset")
    );
    println!("  heap ceiling : none. Rust has no such concept and nothing to configure.");
    println!("  GC           : none. RSS is live data plus fragmentation, and that is all.");
    println!("  starting RSS : {}", rss_display());
    println!();

    if rss_mb().is_none() {
        println!("  NOTE: no /proc/self/status on this platform, so there is no");
        println!("        CURRENT-RSS reading to print. getrusage offers a PEAK, which");
        println!("        is a different question, and this program will not print one");
        println!("        number under the name of another. Run it in a Linux container");
        println!("        for the RSS column.");
        println!();
    }

    let ceiling_mb = match limit {
        None => {
            println!("  !! No cgroup memory limit on this host, so nothing can OOM-kill");
            println!("  !! this process. It will stop ITSELF at {self_limit_mb} MiB and say so.");
            println!("  !! For the kill:");
            println!("  !!   docker run --rm --memory=256m -v \"$PWD:/w\" -w /w rust:1 \\");
            println!("  !!     sh -c 'cd /w && cargo run --release'");
            println!();
            self_limit_mb
        }
        Some(bytes) => {
            let target = (bytes as f64 / (1 << 20) as f64 * 1.5) as usize;
            println!("  Allocating toward {target} MiB against a {} MiB limit.", bytes / (1 << 20));
            println!("  Every chunk is written to. Under Linux's default overcommit the");
            println!("  allocation itself is free -- the cgroup charge lands on the WRITE.");
            println!();
            target
        }
    };

    println!(
        "  mode: {}",
        if try_reserve {
            "--try-reserve  (the fallible path: Vec::try_reserve returns an Err)"
        } else {
            "--infallible   (the default path: allocation failure -> abort, not Err)"
        }
    );
    println!();

    let mut blocks: Vec<Vec<u8>> = Vec::new();
    let mut allocated_mb = 0usize;

    while allocated_mb < ceiling_mb {
        let mut block: Vec<u8> = Vec::new();
        if try_reserve {
            // The fallible path. This is what code that must survive OOM
            // looks like -- and note how little of your codebase does this,
            // and how completely it fails to help against a cgroup.
            if let Err(err) = block.try_reserve_exact(CHUNK_MB << 20) {
                println!();
                println!("  try_reserve returned Err: {err:?}");
                println!("  The ALLOCATOR said no, and Rust handed you a value instead of");
                println!("  aborting. That is the entire point of try_reserve -- and it is");
                println!("  a different event from a cgroup OOM kill, which never gives");
                println!("  the allocator a chance to say anything.");
                break;
            }
        } else {
            // The default path. If this fails, handle_alloc_error aborts the
            // process -- no Err, no unwind, no Drop.
            block.reserve_exact(CHUNK_MB << 20);
        }
        // Touch every page. Reserving is free under overcommit; the cgroup
        // charge happens on the write, which is why the kill lands here and
        // not on the line above.
        block.resize(CHUNK_MB << 20, 1u8);
        blocks.push(block);
        allocated_mb += CHUNK_MB;

        if allocated_mb % 32 == 0
            || limit.is_some_and(|l| (allocated_mb as u64) << 20 > l * 8 / 10)
        {
            println!(
                "    allocated {allocated_mb:5} MiB   RSS {}   memory.events: {}",
                rss_display(),
                read_or("/sys/fs/cgroup/memory.events", "n/a").replace('\n', " ")
            );
        }
    }

    println!();
    println!("  Reached {allocated_mb} MiB without being killed or aborting.");
    match limit {
        None => {
            println!("  Expected: no cgroup here to kill anything, and the self-imposed");
            println!("  ceiling stopped the loop. Nothing was enforced.");
        }
        Some(_) => {
            println!("  NOT expected under a memory limit. The kernel reclaimed enough to");
            println!("  keep up, or memory.high is set and doing its job.");
        }
    }
    println!();
    println!("  The three ways a Rust process can end here, and only one is Rust's:");
    println!("    * try_reserve Err        the allocator refused. Recoverable. Rare in");
    println!("                             a container, because the allocator is not");
    println!("                             what refuses you.");
    println!("    * handle_alloc_error     abort, exit 134. No unwind, no Drop, but the");
    println!("                             runtime does print 'memory allocation failed'.");
    println!("    * SIGKILL                exit 137. Nothing printed. Not a Rust event at");
    println!("                             all -- the kernel did it, and no language");
    println!("                             feature can intercept it.");
    println!();
    println!("  Rust's contribution to this topic is not an error path. It is that RSS");
    println!("  above is your live set, near enough exactly -- no collector lag, no");
    println!("  'it will come down eventually'. That is the number to size memory.max");
    println!("  from, and Rust is the only runtime here that hands it to you honestly.");
}
