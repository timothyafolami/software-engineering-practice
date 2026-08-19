//! Layer 4 Topic 7 (part 5) -- what makes THIS runtime stop renewing its lease.
//!
//! WHAT THIS DEMONSTRATES: Rust has no garbage collector, so it cannot have a
//! collector pause. It loses the lease anyway. That is exactly why it is in this
//! topic: the hazard is about SCHEDULING, and a language marketed on not having
//! a GC does not get an exemption.
//!
//! Four runs:
//!   1. current_thread flavour, blocking call inside a task. One OS thread runs
//!      every task, so a task that does not `.await` owns it -- structurally
//!      identical to Python's asyncio and Node's event loop.
//!   2. current_thread + `tokio::task::spawn_blocking`. The fix, which is the
//!      same fix as Python's run_in_executor and Node's libuv pool under a
//!      different name: every tokio runtime keeps a separate blocking pool
//!      around regardless of flavour.
//!   3. multi_thread flavour, ONE blocking task. Usually survives, and that is
//!      the trap -- it looks like the flavour fixed the problem.
//!   4. multi_thread with as many blocking tasks as there are worker threads.
//!      The trap closing: the pool is finite, and a finite pool with every
//!      worker blocked is a single thread with extra steps.
//!
//! WHAT TO LOOK FOR IN THE OUTPUT: runs 3 and 4 together. If you conclude from
//! run 3 that `multi_thread` is the fix, run 4 is the production incident.
//!
//!   cd rust/pause_audit && cargo run --release

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tokio::runtime::Builder;
use tokio::time::interval;

const LEASE_TTL: Duration = Duration::from_secs(10);
const RENEW_INTERVAL: Duration = Duration::from_secs(1);
const HAZARD: Duration = Duration::from_secs(12);

/// Renewal gaps on a monotonic clock. `Instant` is the only choice here and the
/// type system enforces it -- there is no way to reach a `SystemTime` from an
/// `Instant`, so an NTP step cannot be mistaken for a pause. Topic 3 has the
/// full argument; this is that argument paying off.
#[derive(Default)]
struct Renewals {
    gaps: Vec<Duration>,
    last: Option<Instant>,
}

impl Renewals {
    fn tick(&mut self) {
        let now = Instant::now();
        if let Some(prev) = self.last {
            self.gaps.push(now.duration_since(prev));
        }
        self.last = Some(now);
    }

    fn longest(&self) -> Duration {
        self.gaps.iter().copied().max().unwrap_or_default()
    }
}

/// The hazard. A real blocking call: `std::thread::sleep` would do it too, but
/// hashing keeps the CPU busy so nobody can argue the runtime "knew" this was a
/// sleep and parked it. Nothing here yields to the executor, ever.
fn blocking_work(d: Duration) -> u64 {
    let end = Instant::now() + d;
    let mut acc: u64 = 0x9e37_79b9_7f4a_7c15;
    let mut rounds: u64 = 0;
    while Instant::now() < end {
        for _ in 0..50_000 {
            acc = acc.rotate_left(7) ^ acc.wrapping_mul(0x2545_f491_4f6c_dd1d);
        }
        rounds += 50_000;
    }
    std::hint::black_box(acc);
    rounds
}

struct Outcome {
    longest: Duration,
    took: Duration,
    rounds: u64,
}

fn run(flavour: &str, workers: usize, blocking_tasks: usize, offload: bool) -> Outcome {
    let rt = if flavour == "current_thread" {
        Builder::new_current_thread().enable_time().build().unwrap()
    } else {
        Builder::new_multi_thread()
            .worker_threads(workers)
            .enable_time()
            .build()
            .unwrap()
    };

    rt.block_on(async move {
        let renewals = Arc::new(Mutex::new(Renewals::default()));
        let r = Arc::clone(&renewals);
        let keepalive = tokio::spawn(async move {
            let mut tick = interval(RENEW_INTERVAL);
            loop {
                tick.tick().await;
                r.lock().unwrap().tick();
            }
        });

        tokio::time::sleep(2 * RENEW_INTERVAL).await;

        let start = Instant::now();
        let mut handles = Vec::new();
        for _ in 0..blocking_tasks {
            if offload {
                // spawn_blocking hands the work to tokio's dedicated blocking
                // pool, which exists on every runtime regardless of flavour.
                // Structurally the same fix as Python's run_in_executor.
                handles.push(tokio::task::spawn_blocking(|| blocking_work(HAZARD)));
            } else {
                // A plain task. It never awaits, so it owns its worker thread
                // for the whole hazard window.
                handles.push(tokio::spawn(async { blocking_work(HAZARD) }));
            }
        }
        let mut rounds = 0;
        for h in handles {
            rounds += h.await.unwrap();
        }
        let took = start.elapsed();

        tokio::time::sleep(2 * RENEW_INTERVAL).await;
        keepalive.abort();

        let guard = renewals.lock().unwrap();
        Outcome {
            longest: guard.longest(),
            took,
            rounds,
        }
    })
}

fn report(label: &str, o: &Outcome) -> bool {
    let lost = o.longest > LEASE_TTL;
    println!(
        "  {:<40}{:>8.2}s    {:>8.2}s    {:<16}{:>13} rounds",
        label,
        o.longest.as_secs_f64(),
        o.took.as_secs_f64(),
        if lost { "LOST THE LEASE" } else { "held" },
        o.rounds
    );
    lost
}

fn main() {
    let cpus = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4);

    println!("==============================================================================");
    println!("Layer 4 Topic 7 -- Rust pause audit");
    println!("==============================================================================");
    println!(
        "  {} / {}, available_parallelism = {}",
        std::env::consts::OS,
        std::env::consts::ARCH,
        cpus
    );
    println!(
        "  lease TTL {}s, renewal every {}s, hazard {}s",
        LEASE_TTL.as_secs(),
        RENEW_INTERVAL.as_secs(),
        HAZARD.as_secs()
    );
    println!("  hazard: a task that never awaits -- no GC involved anywhere");
    println!("  clock : std::time::Instant, which cannot be a wall clock by construction");
    println!();
    println!(
        "  {:<40}{:>9}    {:>9}    {:<16}",
        "run", "longest gap", "hazard took", "verdict"
    );

    let mut any_lost = false;
    any_lost |= report(
        "current_thread, blocking in a task",
        &run("current_thread", 1, 1, false),
    );
    report(
        "current_thread, spawn_blocking",
        &run("current_thread", 1, 1, true),
    );
    report(
        "multi_thread, ONE blocking task",
        &run("multi_thread", cpus, 1, false),
    );
    any_lost |= report(
        &format!("multi_thread, {} blocking tasks", cpus),
        &run("multi_thread", cpus, cpus, false),
    );
    report(
        &format!("multi_thread, {} via spawn_blocking", cpus),
        &run("multi_thread", cpus, cpus, true),
    );

    println!();
    println!("  There is no garbage collector in this program. There was never going to");
    println!("  be a collector pause, and the renewal task starved anyway -- which is the");
    println!("  reason Rust is in this topic. The hazard is scheduling, not garbage.");
    println!();
    println!("  Read rows 3 and 4 together, in that order. One blocking task on the");
    println!("  multi_thread flavour usually survives, and concluding 'multi_thread fixes");
    println!("  it' is the mistake: the worker pool is FINITE, so enough blocking tasks");
    println!("  turn it back into a single thread with extra steps. Under load you get");
    println!("  row 4, and you tested row 3.");
    println!();
    println!("  Fencing is what makes this survivable. A stale holder that resumes must");
    println!("  be REJECTED BY THE RESOURCE -- `AND fence < $epoch` in the UPDATE --");
    println!("  because no amount of runtime tuning removes the pause.");

    if any_lost {
        std::process::exit(1);
    }
}
