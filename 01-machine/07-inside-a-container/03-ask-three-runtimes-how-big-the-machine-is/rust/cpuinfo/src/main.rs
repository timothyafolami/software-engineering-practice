//! 7.3 -- Rust: the standard library got there first.
//!
//! WHAT THIS DEMONSTRATES
//!   `std::thread::available_parallelism()` accounts for the affinity mask
//!   *and* the cgroup CPU bandwidth limit on Linux. It is the only call in
//!   this entire thirteen-row matrix that answers question (3) -- "how much
//!   CPU time may I consume" -- without being asked for it by name, and it
//!   is in the standard library with no dependency and no version flag.
//!
//!   The consequence is larger than the call: tokio's `multi_thread` worker
//!   count and rayon's pool both derive from it, so an idiomatic Rust
//!   service is quota-sized by accident. That is a real advantage and it is
//!   worth being precise about where it stops:
//!
//!     * `num_cpus::get()` is also affinity/quota-aware on Linux, but
//!       `num_cpus::get_physical()` answers a THIRD question (physical
//!       cores, ignoring SMT), and code that reaches for one meaning the
//!       other is common.
//!     * tokio's BLOCKING pool defaults to 512 threads and is sized from
//!       nothing at all -- see 7.2's Rust row, where that is measured.
//!     * memory has no equivalent: nothing in std reads `memory.max`.
//!
//! WHAT TO LOOK FOR IN THE OUTPUT
//!   1. available_parallelism() next to the enforced quota. Inside a
//!      container under `--cpus=1.5` they agree, and Rust is the only
//!      runtime here for which that is true out of the box. Predict what
//!      it does with the .5 before you look.
//!   2. The comparison row: what the same program would have concluded from
//!      the host CPU count, which is what C++ and pre-1.25 Go do.
//!
//! RUN
//!   cargo run --release
//!
//!   Inside a Linux container, which is where the columns separate:
//!     docker run --rm --cpus=1.5 -v "$PWD:/w" -w /w rust:1 \
//!       sh -c 'cd /w && cargo run --release'

use std::fs;
use std::thread;

// ---------------------------------------------------------------- the kernel

/// CPUs of bandwidth the cgroup actually enforces, or `None` for no ceiling.
///
/// This is here for comparison, not because you need it: on Linux
/// `available_parallelism()` has already consulted this file. Printing both
/// is the habit -- read the enforced number before trusting any runtime that
/// claims to have read it for you.
fn read_cpu_max() -> Option<f64> {
    if let Ok(raw) = fs::read_to_string("/sys/fs/cgroup/cpu.max") {
        let mut parts = raw.split_whitespace();
        let quota = parts.next()?;
        let period: f64 = parts.next().unwrap_or("100000").parse().ok()?;
        if quota == "max" {
            return None;
        }
        return Some(quota.parse::<f64>().ok()? / period);
    }
    let quota: i64 = fs::read_to_string("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        .ok()?
        .trim()
        .parse()
        .ok()?;
    let period: i64 = fs::read_to_string("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        .ok()?
        .trim()
        .parse()
        .ok()?;
    (quota > 0 && period > 0).then(|| quota as f64 / period as f64)
}

fn read_or_na(path: &str) -> String {
    fs::read_to_string(path)
        .map(|s| s.trim().to_string())
        .unwrap_or_else(|_| "n/a".to_string())
}

/// Host logical CPUs, unfiltered -- the number `std::thread::hardware_concurrency`
/// would give you in C++ and the number pre-1.25 Go used. Not exposed by std,
/// deliberately, so it is read from the same place the kernel publishes it.
fn host_cpus() -> Option<usize> {
    if let Ok(cpuinfo) = fs::read_to_string("/proc/cpuinfo") {
        let count = cpuinfo.lines().filter(|l| l.starts_with("processor")).count();
        if count > 0 {
            return Some(count);
        }
    }
    if cfg!(target_os = "macos") {
        let out = std::process::Command::new("sysctl")
            .args(["-n", "hw.logicalcpu"])
            .output()
            .ok()?;
        return String::from_utf8_lossy(&out.stdout).trim().parse().ok();
    }
    None
}

// ------------------------------------------------------------------- output

fn print_table(headers: &[&str], rows: &[Vec<String>]) {
    let mut widths: Vec<usize> = headers.iter().map(|h| h.len()).collect();
    for row in rows {
        for (i, cell) in row.iter().enumerate() {
            widths[i] = widths[i].max(cell.len());
        }
    }
    let emit = |cells: &[String]| {
        println!(
            "{}",
            cells
                .iter()
                .enumerate()
                .map(|(i, c)| format!("{:width$}", c, width = widths[i]))
                .collect::<Vec<_>>()
                .join("  ")
        );
    };
    emit(&headers.iter().map(|h| h.to_string()).collect::<Vec<_>>());
    emit(&widths.iter().map(|w| "-".repeat(*w)).collect::<Vec<_>>());
    for row in rows {
        emit(row);
    }
}

fn main() {
    let parallelism = thread::available_parallelism().map(|n| n.get()).ok();
    let quota = read_cpu_max();
    let host = host_cpus();

    println!("7.3 -- how big is this machine? Rust's answer");
    println!(
        "  runtime     : rustc std, no dependencies, on {}/{}",
        std::env::consts::OS,
        std::env::consts::ARCH
    );
    println!();

    print_table(
        &[
            "what people call",
            "the call",
            "answer here",
            "which question it answers",
            "what it tracks",
        ],
        &[
            vec![
                "available_parallelism()".into(),
                "std::thread::available_parallelism()".into(),
                parallelism.map(|n| n.to_string()).unwrap_or("n/a".into()),
                "(2)+(3) affinity AND bandwidth".into(),
                "sched_getaffinity + cpu.max".into(),
            ],
            vec![
                "host logical CPUs".into(),
                "/proc/cpuinfo | sysctl".into(),
                host.map(|n| n.to_string()).unwrap_or("n/a".into()),
                "(1) how big is the machine".into(),
                "nothing -- the host, always".into(),
            ],
            vec![
                "/sys/fs/cgroup/cpu.max".into(),
                "fs::read_to_string(...)".into(),
                quota.map(|q| format!("{q:.2}")).unwrap_or("n/a".into()),
                "(3) how much CPU TIME may I consume".into(),
                "cpu.max -- THE ENFORCED NUMBER".into(),
            ],
        ],
    );
    println!();

    println!("  ground truth on this host:");
    println!("    cpu.max               {}", read_or_na("/sys/fs/cgroup/cpu.max"));
    println!(
        "    cpuset.cpus.effective {}",
        read_or_na("/sys/fs/cgroup/cpuset.cpus.effective")
    );
    println!("    memory.max            {}", read_or_na("/sys/fs/cgroup/memory.max"));
    println!();

    match (parallelism, quota, host) {
        (Some(p), Some(q), Some(h)) if p != h => {
            println!("  available_parallelism() returned {p} while the host has {h} CPUs.");
            println!("  It read the quota ({q:.2} CPU) and did the right thing without");
            println!("  being asked. Every other runtime in this topic needed a version");
            println!("  bump, a JVM flag, or twenty hand-written lines to get here.");
        }
        (Some(_), None, _) => {
            println!("  NOTE: no CPU quota is enforced here, so available_parallelism()");
            println!("        and the host count agree and the matrix has one column.");
            println!("        That is the correct result on this host -- run it under");
            println!("        --cpus=1.5 inside a container and the rows separate.");
        }
        _ => {}
    }
    println!();

    println!("  Where Rust's advantage stops, which matters as much as where it starts:");
    println!("    * tokio's BLOCKING pool defaults to 512 threads and is sized from");
    println!("      nothing at all. spawn_blocking under load puts far more runnable");
    println!("      threads in the cgroup than the worker pool ever would -- measured");
    println!("      in ../../02-throttled-at-30-percent-cpu/rust/, row 4.");
    println!("    * num_cpus::get() is also quota-aware on Linux, but get_physical()");
    println!("      answers a different question again (physical cores, ignoring SMT).");
    println!("      Reaching for one meaning the other is a common and silent bug.");
    println!("    * nothing in std reads memory.max. There is no");
    println!("      available_memory() to match available_parallelism(), and Rust has");
    println!("      no heap ceiling to configure even if there were -- that is 7.6.");

    if let Some(q) = quota {
        println!();
        let floor = (q.floor() as usize).max(1);
        let ceil = q.ceil() as usize;
        println!("  Rounding, since a fractional quota has to go somewhere:");
        println!("    quota {q:.2} CPU -> floor {floor}, ceil {ceil}");
        println!("    Go 1.25 rounds UP (more threads than the quota can keep busy,");
        println!("    still throttleable). Round DOWN and you leave the fraction");
        println!("    unused but are very nearly unthrottleable. Neither is wrong;");
        println!("    they optimise for throughput and for tail latency respectively.");
    }
}
