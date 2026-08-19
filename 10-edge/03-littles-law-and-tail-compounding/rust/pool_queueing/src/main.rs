// Layer 10 - Topic 3: the pool is the concurrency limit. (Rust / tokio)
//
// What this demonstrates
//     Part 1  L = λW as a wall, against tokio's Semaphore -- the same
//             object sqlx and deadpool use underneath, in a different
//             costume. c permits and a mean service time W pin maximum
//             throughput at c/W. Service time never changes; everything
//             that moves is acquire wait.
//     Part 2  The Rust-specific property, and its Rust-specific way of
//             being thrown away. A dropped future IS a cancelled query, so
//             a client deadline expressed as `tokio::time::timeout` around
//             the work releases the permit at the deadline -- no leak, no
//             cleanup code, nothing to remember. But `tokio::spawn` breaks
//             that: a spawned task outlives the future that spawned it, so
//             abandoning its JoinHandle abandons nothing. The permit stays
//             held for work whose result no longer has a reader.
//
// What to look for
//     - Part 1: `svc p50` flat across every row while `acq p99` explodes.
//     - Part 2: identical load, identical deadline. Compare `goodput`
//       (requests that finished inside the deadline) and `permit-seconds
//       wasted` (permit time spent on work already abandoned). The
//       structured arm wastes none by construction.
//     - This is the same lesson as topic 2's cancellation experiment,
//       arriving at a different resource: there, the abandoned request held
//       KV blocks; here it holds a pool slot. Cancellation is about
//       ownership, and `spawn` is where ownership gets dropped on purpose.
//
// The Kingman variance arm lives in python/pool_queueing.py -- distributions
// are arithmetic, not a property of any runtime.
//
// One dependency (tokio), declared in Cargo.toml. Runs with no arguments:
//     cargo run --release --manifest-path rust/pool_queueing/Cargo.toml

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tokio::sync::Semaphore;
use tokio::time::sleep;

const SEED: u64 = 20260818;

/// Deterministic xorshift, so two runs of this file are comparable.
struct Rng(u64);

impl Rng {
    fn next_f64(&mut self) -> f64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }

    fn exponential(&mut self, mean: f64) -> f64 {
        -(1.0 - self.next_f64()).ln() * mean
    }
}

#[derive(Default)]
struct Samples {
    acquire: Vec<f64>,
    service: Vec<f64>,
    total: Vec<f64>,
}

fn pct(values: &[f64], q: f64) -> f64 {
    if values.is_empty() {
        return f64::NAN;
    }
    let mut v = values.to_vec();
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    v[((q * v.len() as f64) as usize).min(v.len() - 1)]
}

/// Part 1: open-loop Poisson arrivals against a `slots`-permit semaphore.
async fn wall_run(lambda: f64, slots: usize, service: Duration, duration: Duration) -> (Samples, u64, f64) {
    let mut rng = Rng(SEED);
    let sem = Arc::new(Semaphore::new(slots));
    let samples = Arc::new(Mutex::new(Samples::default()));
    let completed = Arc::new(AtomicU64::new(0));

    let start = Instant::now();
    let mut next = start;
    let mut tasks = Vec::new();
    while start.elapsed() < duration {
        next += Duration::from_secs_f64(rng.exponential(1.0 / lambda));
        let now = Instant::now();
        if next > now {
            sleep(next - now).await;
        }
        let sem = sem.clone();
        let samples = samples.clone();
        let completed = completed.clone();
        tasks.push(tokio::spawn(async move {
            let arrived = Instant::now();
            let permit = sem.acquire_owned().await.unwrap();
            let acquired = Instant::now();
            sleep(service).await;
            let done = Instant::now();
            drop(permit);
            completed.fetch_add(1, Ordering::Relaxed);
            let mut s = samples.lock().unwrap();
            s.acquire.push((acquired - arrived).as_secs_f64() * 1e3);
            s.service.push((done - acquired).as_secs_f64() * 1e3);
            s.total.push((done - arrived).as_secs_f64() * 1e3);
        }));
    }
    let wall = start.elapsed().as_secs_f64();
    // Completions that landed INSIDE the arrival window. Throughput must be
    // counted over the same interval as `wall`: the counter keeps rising
    // during the drain below, and dividing the post-drain total by the
    // arrival window reports a rate above c/W -- above the wall itself.
    let completed_in_window = completed.load(Ordering::Relaxed);

    // Bounded drain: past the wall the queue never drains, and that is the
    // result rather than a bug.
    let _ = tokio::time::timeout(duration, async {
        for t in tasks {
            let _ = t.await;
        }
    })
    .await;

    let s = std::mem::take(&mut *samples.lock().unwrap());
    (s, completed_in_window, wall)
}

struct DeadlineResult {
    goodput: u64,
    abandoned: u64,
    wasted_permit_seconds: f64,
}

/// Part 2: the same load with a client deadline, two ways of expressing it.
///
/// `detached = false`  the work is a future awaited inside `timeout`. When
///                     the deadline fires the future is dropped, the permit
///                     goes back immediately, and nothing needed writing.
/// `detached = true`   the work is `tokio::spawn`ed and only its JoinHandle
///                     is awaited inside `timeout`. Dropping a JoinHandle
///                     does not stop the task, so the permit stays held.
async fn deadline_run(
    lambda: f64,
    slots: usize,
    service: Duration,
    deadline: Duration,
    duration: Duration,
    detached: bool,
) -> DeadlineResult {
    let mut rng = Rng(SEED);
    let sem = Arc::new(Semaphore::new(slots));
    let goodput = Arc::new(AtomicU64::new(0));
    let abandoned = Arc::new(AtomicU64::new(0));
    // Permit time spent on work whose caller has already given up, in
    // microseconds. This is the resource the abandoned request is stealing.
    let wasted_us = Arc::new(AtomicU64::new(0));

    let start = Instant::now();
    let mut next = start;
    let mut tasks = Vec::new();
    while start.elapsed() < duration {
        next += Duration::from_secs_f64(rng.exponential(1.0 / lambda));
        let now = Instant::now();
        if next > now {
            sleep(next - now).await;
        }
        let (sem, goodput, abandoned, wasted_us) =
            (sem.clone(), goodput.clone(), abandoned.clone(), wasted_us.clone());
        tasks.push(tokio::spawn(async move {
            let arrived = Instant::now();
            let work = {
                let sem = sem.clone();
                let wasted_us = wasted_us.clone();
                async move {
                    let permit = sem.acquire_owned().await.unwrap();
                    let held_from = Instant::now();
                    sleep(service).await;
                    let overrun = arrived.elapsed().saturating_sub(deadline);
                    if !overrun.is_zero() {
                        // Permit time past the caller's deadline: pure waste.
                        let held = held_from.elapsed().min(overrun);
                        wasted_us.fetch_add(held.as_micros() as u64, Ordering::Relaxed);
                    }
                    drop(permit);
                }
            };

            let finished = if detached {
                // The bug: spawn detaches the work from this future's
                // lifetime. Timing out here drops a handle, not a task.
                let handle = tokio::spawn(work);
                tokio::time::timeout(deadline, handle).await.is_ok()
            } else {
                // The fix, which is not a fix so much as the absence of a
                // mistake: the work is owned by this future, so the timeout
                // drops it and the permit is released at the deadline.
                tokio::time::timeout(deadline, work).await.is_ok()
            };

            if finished {
                goodput.fetch_add(1, Ordering::Relaxed);
            } else {
                abandoned.fetch_add(1, Ordering::Relaxed);
            }
        }));
    }

    let _ = tokio::time::timeout(duration, async {
        for t in tasks {
            let _ = t.await;
        }
    })
    .await;

    DeadlineResult {
        goodput: goodput.load(Ordering::Relaxed),
        abandoned: abandoned.load(Ordering::Relaxed),
        wasted_permit_seconds: wasted_us.load(Ordering::Relaxed) as f64 / 1e6,
    }
}

#[tokio::main]
async fn main() {
    println!("Rust / tokio - pool queueing and Little's Law");
    println!("  arrivals: Poisson (c_a = 1), open loop, seed {SEED}");

    let slots = 20usize;
    let service = Duration::from_millis(50);
    println!(
        "\nPart 1 - L = λW. c = {slots} permits, W = {}ms, so λ_max = c/W = {:.0} req/s",
        service.as_millis(),
        slots as f64 / service.as_secs_f64()
    );
    println!("{}", "-".repeat(78));
    println!(
        "  {:<10} {:>5} {:>9} {:>9} {:>9} {:>9} {:>9}",
        "run", "ρ", "acq p50", "acq p99", "svc p50", "tot p99", "done/s"
    );
    for lambda in [200.0, 360.0, 400.0, 440.0] {
        let (s, completed, wall) =
            wall_run(lambda, slots, service, Duration::from_secs(3)).await;
        let rho = lambda * service.as_secs_f64() / slots as f64;
        println!(
            "  {:<10} {:>5.2} {:>9.1} {:>9.1} {:>9.1} {:>9.1} {:>9.0}",
            format!("λ={lambda:.0}"),
            rho,
            pct(&s.acquire, 0.5),
            pct(&s.acquire, 0.99),
            pct(&s.service, 0.5),
            pct(&s.total, 0.99),
            completed as f64 / wall
        );
    }
    println!("\n  Service time is identical in every row. Everything that moved is");
    println!("  waiting for a permit, which is why acquire wait needs its own timer.");

    let deadline = Duration::from_millis(120);
    println!("\nPart 2 - a client deadline, and what `tokio::spawn` does to it");
    println!("{}", "-".repeat(78));
    println!(
        "  λ = 420/s against c = {slots}, W = {}ms (ρ > 1 on purpose), \
         deadline {}ms",
        service.as_millis(),
        deadline.as_millis()
    );
    println!(
        "\n  {:<34} {:>9} {:>11} {:>22}",
        "how the deadline is expressed", "goodput", "abandoned", "permit-seconds wasted"
    );
    for (label, detached) in [
        ("timeout(deadline, work)", false),
        ("timeout(deadline, spawn(work))", true),
    ] {
        let r = deadline_run(
            420.0,
            slots,
            service,
            deadline,
            Duration::from_secs(4),
            detached,
        )
        .await;
        println!(
            "  {:<34} {:>9} {:>11} {:>22.2}",
            label, r.goodput, r.abandoned, r.wasted_permit_seconds
        );
    }
    println!("\n  Same deadline, same load, same semaphore. In the first row the");
    println!("  permit comes back when the caller gives up, because dropping the");
    println!("  future drops everything it owned. In the second, spawn moved the");
    println!("  work out from under the timeout, so the permit is held to the end");
    println!("  of a query whose result nobody will read. Rust gives you the");
    println!("  strongest cancellation guarantee of the six runtimes here and one");
    println!("  very easy way to opt out of it.");
    println!();
    println!("  The Kingman variance arm is in python/pool_queueing.py.");
}
