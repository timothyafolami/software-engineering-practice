//! 7.2 -- Rust: the runtime that was already right, used as the control.
//!
//! WHAT THIS DEMONSTRATES
//!   `std::thread::available_parallelism()` accounts for the affinity mask
//!   *and* the cgroup CPU bandwidth limit on Linux. It is the only call in
//!   this entire topic that answers "how much CPU time may I consume" without
//!   being asked for it by name. Because tokio's `multi_thread` worker count
//!   and rayon's pool both derive from it, an idiomatic Rust service is
//!   quota-sized by accident.
//!
//!   That makes Rust the useful control in this experiment: same workload,
//!   same bucket, a runtime whose default is already correct. Any throttling
//!   you see here is your own thread count and not a bad default -- which is
//!   why the interesting row is the LAST one, where `spawn_blocking` puts
//!   threads from tokio's blocking pool (default 512, sized from nothing at
//!   all) into the same cgroup. Rust's gap is not the worker pool. It is the
//!   pool nobody mentions.
//!
//! WHAT TO LOOK FOR IN THE OUTPUT
//!   1. The header: available_parallelism() next to what cpu.max enforces.
//!      Inside a container under `--cpus=1.0` those agree, and Rust is the
//!      only runtime here for which that is true out of the box.
//!   2. Row 1 vs row 2: the same offered load at two worker counts. Identical
//!      throughput -- the quota was always the ceiling -- and a different
//!      throttle ratio and heartbeat gap.
//!   3. Row 3: `spawn_blocking` with the default blocking pool. More runnable
//!      threads in the cgroup, the same fixed bucket, a worse tail. Being
//!      "container-aware" is a property of one specific call, not of a
//!      runtime.
//!
//! PORTABILITY
//!   Runs on Linux and macOS. There is no `/sys/fs/cgroup` on Darwin, so the
//!   bucket below is then a userspace MODEL and the program prints a FALLBACK
//!   banner. Real numbers come from `/sys/fs/cgroup/cpu.stat` inside a
//!   container under `--cpus=1.0`.
//!
//! RUN
//!   cargo run --release

use std::fs;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

const WORK_MS: f64 = 40.0; // CPU cost of one "request"
const OFFERED_RATE: f64 = 9.0; // req/s -> ~0.36 CPU of demand
const RUN_SECONDS: f64 = 15.0;
const HEARTBEAT_MS: u64 = 10;
const PERIOD_US: u64 = 100_000; // the kernel default, and Docker's
const CHUNK_MS: f64 = 2.0; // budget check-in granularity
const SEED: u64 = 20_260_818;

// ---------------------------------------------------------------- the kernel

/// CPUs of bandwidth the cgroup actually enforces, or `None` for no ceiling.
///
/// Twenty lines and no dependencies. `available_parallelism()` is doing this
/// for you on Linux; every other runtime in this topic either does it too or
/// does not, and this is the whole of what "container-aware" means.
fn read_cpu_max() -> Option<f64> {
    if let Ok(raw) = fs::read_to_string("/sys/fs/cgroup/cpu.max") {
        let mut parts = raw.split_whitespace();
        let quota = parts.next()?;
        let period: u64 = parts.next().unwrap_or("100000").parse().ok()?;
        if quota == "max" {
            return None; // a cgroup exists, with no ceiling set
        }
        return Some(quota.parse::<f64>().ok()? / period as f64);
    }
    // cgroup v1: two files, and -1 spells "unlimited".
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

/// Per-thread CPU time in microseconds. Not wall time: a thread the budget
/// has parked is consuming no CPU, and billing it wall time would charge it
/// for the freeze it is already being punished by, which inverts the result.
fn thread_cpu_us() -> f64 {
    let mut ts = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    // CLOCK_THREAD_CPUTIME_ID exists on both Linux and macOS 10.12+.
    unsafe { libc::clock_gettime(libc::CLOCK_THREAD_CPUTIME_ID, &mut ts) };
    ts.tv_sec as f64 * 1e6 + ts.tv_nsec as f64 / 1e3
}

/// OS threads this process has right now. `/proc` does not exist on Darwin,
/// so there are two genuinely different mechanisms here rather than one that
/// silently returns a wrong number on the second platform.
fn thread_census() -> Option<usize> {
    if let Ok(status) = fs::read_to_string("/proc/self/status") {
        for line in status.lines() {
            if let Some(rest) = line.strip_prefix("Threads:") {
                return rest.trim().parse().ok();
            }
        }
    }
    if cfg!(target_os = "macos") {
        let out = std::process::Command::new("ps")
            .args(["-M", "-p", &std::process::id().to_string()])
            .output()
            .ok()?;
        let lines = String::from_utf8_lossy(&out.stdout).lines().count();
        return (lines > 1).then(|| lines - 1);
    }
    None
}

// ---------------------------------------------------------------- the budget

/// One cgroup's worth of CFS bandwidth: a bucket of `quota_us` microseconds
/// refilled every `period_us`, with every task parked when it is empty.
///
/// This is a MODEL, not the kernel. Inside a Linux container the real numbers
/// live in `/sys/fs/cgroup/cpu.stat`; this exists so the same program produces
/// a comparable table on a host with no cgroupfs.
struct CpuBudget {
    quota_us: i64,
    period_us: u64,
    state: Mutex<BudgetState>,
    refilled: Condvar,
    running: AtomicBool,
}

struct BudgetState {
    balance: i64,
    usage_us: u64,
    periods: u64,
    throttled: u64,
    generation: u64,
    froze_this_period: bool,
}

impl CpuBudget {
    fn new(quota_cpus: f64, period_us: u64) -> Arc<Self> {
        let quota_us = (quota_cpus * period_us as f64) as i64;
        let budget = Arc::new(CpuBudget {
            quota_us,
            period_us,
            state: Mutex::new(BudgetState {
                balance: quota_us,
                usage_us: 0,
                periods: 0,
                throttled: 0,
                generation: 0,
                froze_this_period: false,
            }),
            refilled: Condvar::new(),
            running: AtomicBool::new(true),
        });
        let refill = Arc::clone(&budget);
        thread::spawn(move || refill.refill_loop());
        budget
    }

    /// Charge CPU already burned, then park if the bucket is empty. The kernel
    /// also charges after the fact, in 5ms slices, which is why a container's
    /// `usage_usec` can slightly overshoot its quota inside one period.
    fn spend(&self, micros: f64) {
        let mut state = self.state.lock().unwrap();
        state.balance -= micros as i64;
        state.usage_us += micros as u64;
        while state.balance <= 0 && self.running.load(Ordering::Relaxed) {
            state.froze_this_period = true;
            let seen = state.generation;
            let (guard, _) = self
                .refilled
                .wait_timeout(state, Duration::from_micros(self.period_us))
                .unwrap();
            state = guard;
            if state.generation != seen {
                break;
            }
        }
    }

    /// For tasks that consume no measurable CPU and are frozen anyway. The
    /// heartbeat calls this: it never spends a microsecond, and the kernel
    /// dequeues it along with everything else in the cgroup.
    fn park_if_throttled(&self) {
        let mut state = self.state.lock().unwrap();
        while state.balance <= 0 && self.running.load(Ordering::Relaxed) {
            let seen = state.generation;
            let (guard, _) = self
                .refilled
                .wait_timeout(state, Duration::from_micros(self.period_us))
                .unwrap();
            state = guard;
            if state.generation != seen {
                break;
            }
        }
    }

    fn refill_loop(&self) {
        let mut next = Instant::now();
        while self.running.load(Ordering::Relaxed) {
            next += Duration::from_micros(self.period_us);
            let now = Instant::now();
            if next > now {
                thread::sleep(next - now);
            }
            let mut state = self.state.lock().unwrap();
            state.periods += 1;
            if state.froze_this_period {
                state.throttled += 1;
            }
            state.froze_this_period = false;
            // Unused quota is LOST, not banked. Banking it is precisely what
            // cpu.max.burst does, and it is 0 by default in Docker and in
            // Kubernetes -- which is why nr_bursts is zero everywhere.
            state.balance = self.quota_us;
            state.generation += 1;
            drop(state);
            self.refilled.notify_all();
        }
    }

    fn stop(&self) {
        self.running.store(false, Ordering::Relaxed);
        self.refilled.notify_all();
    }

    fn snapshot(&self) -> (u64, u64, u64) {
        let state = self.state.lock().unwrap();
        (state.usage_us, state.periods, state.throttled)
    }
}

// ------------------------------------------------------------------ the work

/// A deterministic, un-optimisable CPU burn. Not a sleep -- a sleeping thread
/// spends no quota, and spending it is the entire subject.
struct Workload {
    block: Vec<u64>,
    blocks_per_chunk: usize,
    sink: AtomicU64,
}

fn xorshift(state: &mut u64) -> u64 {
    *state ^= *state << 13;
    *state ^= *state >> 7;
    *state ^= *state << 17;
    *state
}

impl Workload {
    fn new(chunk_ms: f64) -> Self {
        let mut seed = SEED;
        let block: Vec<u64> = (0..32 * 1024).map(|_| xorshift(&mut seed)).collect();
        let mut workload = Workload { block, blocks_per_chunk: 1, sink: AtomicU64::new(0) };
        workload.blocks_per_chunk = workload.calibrate(chunk_ms);
        workload
    }

    fn hash_block(&self, seed: u64) -> u64 {
        let mut h = seed ^ 0x9E37_79B9_7F4A_7C15;
        for &word in &self.block {
            h ^= word;
            h = h.wrapping_mul(0x1_0000_0001_B3);
            h = h.rotate_left(7);
        }
        h
    }

    /// Size ONE CHUNK -- the granularity at which a worker checks in with the
    /// budget, the same role the kernel's 5ms bandwidth slice plays. Warm up
    /// first and time second: the first pass over a 256 KiB block comes from
    /// DRAM and every later pass from L2.
    fn calibrate(&self, target_ms: f64) -> usize {
        let mut h = 0u64;
        for _ in 0..40 {
            h = self.hash_block(h);
        }
        let start = thread_cpu_us();
        for _ in 0..40 {
            h = self.hash_block(h);
        }
        let per_block_ms = (thread_cpu_us() - start) / 1000.0 / 40.0;
        self.sink.fetch_add(h, Ordering::Relaxed);
        if per_block_ms <= 0.0 {
            1
        } else {
            ((target_ms / per_block_ms) as usize).max(1)
        }
    }

    /// Burn until this task has actually CONSUMED `work_ms` of thread CPU
    /// time, charging the budget between chunks.
    ///
    /// Spend-until-consumed rather than a fixed block count, for a reason
    /// specific to this machine: an Apple M1 has four performance cores and
    /// four efficiency cores, so "how many hash blocks is 40ms" has two
    /// different answers depending on where the thread landed. A block count
    /// calibrated on one core type and run on the other silently changes the
    /// offered load, which is the one variable this experiment holds fixed.
    /// Charging real consumed CPU time is immune to that, and it is also what
    /// the kernel does -- which is the better reason.
    fn burn(&self, work_ms: f64, budget: &CpuBudget) {
        let mut h = self.sink.load(Ordering::Relaxed);
        let mut charged_us = 0.0;
        let target_us = work_ms * 1000.0;
        while charged_us < target_us {
            let start = thread_cpu_us();
            for _ in 0..self.blocks_per_chunk {
                h = self.hash_block(h);
            }
            let spent = thread_cpu_us() - start;
            charged_us += spent;
            budget.spend(spent);
        }
        self.sink.store(h, Ordering::Relaxed);
    }
}

// --------------------------------------------------------------- one variant

struct Outcome {
    completed: usize,
    req_per_s: f64,
    avg_cpu: f64,
    periods: u64,
    throttled: u64,
    p50: f64,
    p99: f64,
    hb_gap: f64,
    threads: usize,
}

/// Poisson arrivals, not evenly spaced. Throttling at low average utilisation
/// is a burstiness effect: the bucket is drained by demand that clumps inside
/// one 100ms window, not by demand averaged over a minute. Evenly-spaced
/// arrivals cannot reproduce it at all, which is why hand-rolled load loops so
/// reliably fail to find production's tail latency.
fn poisson_schedule(rate: f64, seconds: f64, seed: u64) -> Vec<f64> {
    let mut state = seed;
    let mut out = Vec::new();
    let mut t = 0.0;
    while t < seconds {
        out.push(t * 1000.0);
        let uniform = (xorshift(&mut state) >> 11) as f64 / (1u64 << 53) as f64;
        t += -(1.0 - uniform).ln() / rate;
    }
    out
}

fn percentile(sorted: &[f64], p: f64) -> f64 {
    if sorted.is_empty() {
        return f64::NAN;
    }
    let rank = ((p / 100.0) * (sorted.len() - 1) as f64).round() as usize;
    sorted[rank.min(sorted.len() - 1)]
}

/// `blocking` selects between running the CPU work on tokio's worker threads
/// (the normal path) and handing it to `spawn_blocking`, whose pool defaults
/// to 512 threads and is sized from nothing at all.
fn run_variant(
    workload: &Arc<Workload>,
    worker_threads: usize,
    quota_cpus: f64,
    blocking: bool,
) -> Outcome {
    let budget = CpuBudget::new(quota_cpus, PERIOD_US);
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(worker_threads)
        .enable_time()
        .build()
        .expect("tokio runtime");

    let schedule = poisson_schedule(OFFERED_RATE, RUN_SECONDS, SEED);
    let latencies: Arc<Mutex<Vec<f64>>> = Arc::new(Mutex::new(Vec::new()));
    let origin = Instant::now();

    // The heartbeat: wants a tick every 10ms, spends no measurable CPU, and is
    // frozen anyway. That asymmetry -- punished for someone else's consumption
    // -- is what makes a health check fail on a container whose own CPU graph
    // looks calm.
    let hb_stop = Arc::new(AtomicBool::new(false));
    let hb_gap = Arc::new(Mutex::new(0.0f64));
    let heartbeat = {
        let budget = Arc::clone(&budget);
        let stop = Arc::clone(&hb_stop);
        let gap = Arc::clone(&hb_gap);
        thread::spawn(move || {
            let mut last = Instant::now();
            while !stop.load(Ordering::Relaxed) {
                thread::sleep(Duration::from_millis(HEARTBEAT_MS));
                budget.park_if_throttled();
                let now = Instant::now();
                let mut gap = gap.lock().unwrap();
                *gap = gap.max((now - last).as_secs_f64() * 1000.0);
                last = now;
            }
        })
    };

    runtime.block_on(async {
        let mut handles = Vec::with_capacity(schedule.len());
        for due_ms in schedule {
            let elapsed_ms = origin.elapsed().as_secs_f64() * 1000.0;
            if due_ms > elapsed_ms {
                tokio::time::sleep(Duration::from_secs_f64((due_ms - elapsed_ms) / 1000.0)).await;
            }
            let workload = Arc::clone(workload);
            let budget = Arc::clone(&budget);
            let latencies = Arc::clone(&latencies);
            let handle = if blocking {
                tokio::task::spawn_blocking(move || {
                    workload.burn(WORK_MS, &budget);
                    let latency = origin.elapsed().as_secs_f64() * 1000.0 - due_ms;
                    latencies.lock().unwrap().push(latency);
                })
            } else {
                tokio::task::spawn(async move {
                    workload.burn(WORK_MS, &budget);
                    let latency = origin.elapsed().as_secs_f64() * 1000.0 - due_ms;
                    latencies.lock().unwrap().push(latency);
                })
            };
            handles.push(handle);
        }
        for handle in handles {
            let _ = handle.await;
        }
    });

    let threads = thread_census().unwrap_or(0);
    let wall_s = origin.elapsed().as_secs_f64();
    let (usage_us, periods, throttled) = budget.snapshot();

    hb_stop.store(true, Ordering::Relaxed);
    let _ = heartbeat.join();
    budget.stop();
    runtime.shutdown_timeout(Duration::from_secs(5));

    let max_gap = *hb_gap.lock().unwrap();
    let mut ordered = latencies.lock().unwrap().clone();
    ordered.sort_by(|a, b| a.partial_cmp(b).unwrap());
    Outcome {
        completed: ordered.len(),
        req_per_s: ordered.len() as f64 / RUN_SECONDS,
        avg_cpu: usage_us as f64 / 1e6 / wall_s * 100.0,
        periods,
        throttled,
        p50: percentile(&ordered, 50.0),
        p99: percentile(&ordered, 99.0),
        hb_gap: max_gap,
        threads,
    }
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
        let line: Vec<String> = cells
            .iter()
            .enumerate()
            .map(|(i, c)| format!("{:width$}", c, width = widths[i]))
            .collect();
        println!("{}", line.join("  "));
    };
    emit(&headers.iter().map(|h| h.to_string()).collect::<Vec<_>>());
    emit(&widths.iter().map(|w| "-".repeat(*w)).collect::<Vec<_>>());
    for row in rows {
        emit(row);
    }
}

fn main() {
    let parallelism = thread::available_parallelism().map(|n| n.get()).unwrap_or(1);
    let quota = read_cpu_max();

    println!("7.2 -- throttled at 30% CPU: Rust");
    println!("  runtime                : rustc/std + tokio multi_thread");
    println!(
        "  available_parallelism(): {}   <- affinity AND cgroup bandwidth, on Linux",
        parallelism
    );
    println!(
        "  quota actually enforced: {}",
        match quota {
            Some(q) => format!("{q:.2} CPU (cpu.max)"),
            None => "none (no cpu.max on this host)".to_string(),
        }
    );
    println!(
        "  OS threads at rest     : {}   (before any runtime is built)",
        thread_census().map(|n| n.to_string()).unwrap_or_else(|| "n/a".into())
    );
    println!();

    if quota.is_none() {
        println!("  !! FALLBACK: no cpu.max to read on this host");
        println!("  !! The bucket below is a userspace MODEL of CFS bandwidth control,");
        println!("  !! not the Linux kernel. Real numbers come from");
        println!("  !! /sys/fs/cgroup/cpu.stat inside a container.");
        println!();
    }

    let workload = Arc::new(Workload::new(CHUNK_MS));
    println!(
        "  one hash block costs {:.3} ms on the calibrating core (measured); {} blocks per {:.0}ms chunk",
        CHUNK_MS / workload.blocks_per_chunk as f64,
        workload.blocks_per_chunk,
        CHUNK_MS
    );
    println!(
        "  each work unit burns until it has CONSUMED {WORK_MS:.0}ms of thread CPU time,"
    );
    println!("  so a P-core and an E-core do the same WORK per request, not the same blocks.");
    println!(
        "  offered load: {:.0} req/s x {:.0}ms CPU = {:.2} CPU of demand",
        OFFERED_RATE,
        WORK_MS,
        OFFERED_RATE * WORK_MS / 1000.0
    );
    println!("  quota:        1.00 CPU. The demand is comfortably under the limit.");
    println!("  heartbeat wants a tick every {HEARTBEAT_MS}ms; {RUN_SECONDS:.0}s per row");
    println!();

    let variants: Vec<(String, usize, f64, bool)> = vec![
        (
            format!("tokio, {parallelism} workers (the default), 1.0 CPU"),
            parallelism,
            1.0,
            false,
        ),
        ("fix 1: tokio, 1 worker, 1.0 CPU".to_string(), 1, 1.0, false),
        (
            format!("fix 2: tokio, {parallelism} workers, 2.0 CPU"),
            parallelism,
            2.0,
            false,
        ),
        (
            "spawn_blocking (pool default 512), 1.0 CPU".to_string(),
            parallelism,
            1.0,
            true,
        ),
    ];

    let mut rows = Vec::new();
    for (label, workers, quota_cpus, blocking) in &variants {
        let outcome = run_variant(&workload, *workers, *quota_cpus, *blocking);
        rows.push(vec![
            label.clone(),
            outcome.completed.to_string(),
            format!("{:.1}", outcome.req_per_s),
            format!("{:.0}%", outcome.avg_cpu),
            format!("{}/{}", outcome.throttled, outcome.periods),
            format!(
                "{:.3}",
                if outcome.periods > 0 {
                    outcome.throttled as f64 / outcome.periods as f64
                } else {
                    0.0
                }
            ),
            format!("{:.0}", outcome.p50),
            format!("{:.0}", outcome.p99),
            format!("{:.0}", outcome.hb_gap),
            outcome.threads.to_string(),
        ]);
        println!("  ran: {label}");
    }

    println!();
    print_table(
        &[
            "variant", "n", "req/s", "avg CPU", "throttled", "ratio", "p50 ms", "p99 ms",
            "hb gap ms", "threads",
        ],
        &rows,
    );

    println!();
    println!("  Rust is the control here. Inside a container, row 1's worker count");
    println!("  IS the quota, because available_parallelism() read cpu.max -- so the");
    println!("  default that every other runtime in this topic had to be taught is");
    println!("  the one Rust shipped with.");
    println!();
    println!("  Row 4 is where the gap actually is. spawn_blocking hands work to a");
    println!("  pool that defaults to 512 threads and is sized from nothing at all,");
    println!("  and those threads live in the same cgroup, drawing on the same");
    println!("  bucket. 'Container-aware' is a property of one specific call, never");
    println!("  of a runtime -- and the call that got it right is not the one your");
    println!("  blocking work goes through.");
    println!();
    println!("  Ground truth, inside a container: cat /sys/fs/cgroup/cpu.stat");
}
