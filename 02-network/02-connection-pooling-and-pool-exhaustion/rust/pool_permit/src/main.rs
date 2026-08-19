// Layer 2 · Topic 2 - Rust: the checked-out connection is a value with a
// lifetime, so leaking one is a compile error rather than an incident.
//
// Every other language in this topic can leak a pool slot. In Python you
// forget `finally: conn.close()` or an exception escapes before the
// context manager is entered. In Java you forget `semaphore.release()` in
// a `finally`. In Go you forget `defer rows.Close()` and the connection
// stays checked out until GC, which is to say indefinitely. Each of those
// is a real, common, production-grade bug, and each one presents
// identically: the pool empties over hours and the service dies slowly.
//
// Rust's version of a pool permit is an owned value whose Drop returns the
// slot. You cannot forget to release it, because you cannot not-drop it.
// The failure mode is not "we leaked the pool"; it is at worst "we held
// the permit longer than we meant to", which is visible in the code as a
// scope.
//
// Three policies over the same tokio Semaphore, so the difference is the
// policy rather than the machinery:
//
//   1. acquire()                 - wait forever. The default incident.
//   2. try_acquire()             - never wait. Shed instantly.
//   3. timeout(d, acquire())     - wait, but bounded. Usually right.
//
// What to look for in the output: identical completed-per-second across
// all three (Little's Law is not negotiable), and completely different
// latency tails and error rates.
//
// Run: cargo run --release

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Semaphore;

const POOL_SIZE: usize = 10;
const QUERY: Duration = Duration::from_millis(50);
const ARRIVAL_RATE: u64 = 400; // requests per second, open model
const DURATION: Duration = Duration::from_secs(5);
const MAX_WAIT: Duration = Duration::from_millis(150);

#[derive(Clone, Copy)]
enum Policy {
    WaitForever,
    NeverWait,
    BoundedWait,
}

#[derive(Default)]
struct Counters {
    completed: AtomicUsize,
    rejected: AtomicUsize,
    waiters: AtomicUsize,
    peak_waiters: AtomicUsize,
}

#[tokio::main]
async fn main() {
    println!("{}", "=".repeat(78));
    println!("Rust: the pool permit is a value, and the policy is the only variable");
    println!("{}", "=".repeat(78));
    println!(
        "  pool {POOL_SIZE} permits, {} ms each, offered {ARRIVAL_RATE} rps for {} s",
        QUERY.as_millis(),
        DURATION.as_secs()
    );
    println!(
        "  Little's Law ceiling: {POOL_SIZE} / {:.3}s = {:.0} rps\n",
        QUERY.as_secs_f64(),
        POOL_SIZE as f64 / QUERY.as_secs_f64()
    );

    run("1. acquire()            - wait forever", Policy::WaitForever).await;
    println!();
    run("2. try_acquire()        - never wait, shed immediately", Policy::NeverWait).await;
    println!();
    run(
        &format!("3. timeout({} ms, acquire()) - bounded wait", MAX_WAIT.as_millis()),
        Policy::BoundedWait,
    )
    .await;

    println!();
    println!("  The Rust-specific part, which is not about the numbers:");
    println!("    `acquire_owned()` hands back a SemaphorePermit. Returning the slot is");
    println!("    that permit's Drop impl, so the only way to hold a connection past");
    println!("    its scope is to deliberately move it somewhere longer-lived -- which");
    println!("    is visible at the call site. There is no equivalent of forgetting a");
    println!("    `finally: release()`.");
    println!("    What Rust does NOT give you is the policy. `acquire()` waits forever,");
    println!("    exactly like SQLAlchemy's default and Go's MaxConnsPerHost queue, and");
    println!("    the compiler has no opinion about that at all. Bounded waiting is a");
    println!("    decision, in every language.");
}

async fn run(label: &str, policy: Policy) {
    let semaphore = Arc::new(Semaphore::new(POOL_SIZE));
    let counters = Arc::new(Counters::default());
    let latencies = Arc::new(tokio::sync::Mutex::new(Vec::<f64>::new()));

    let start = Instant::now();
    let mut handles = Vec::new();
    let mut index: u64 = 0;

    // Open model: arrivals are scheduled from `start`, never from the
    // completion of previous work.
    loop {
        let due = start + Duration::from_nanos(index * 1_000_000_000 / ARRIVAL_RATE);
        if due.duration_since(start) > DURATION {
            break;
        }
        let now = Instant::now();
        if due > now {
            tokio::time::sleep(due - now).await;
        }
        index += 1;

        let semaphore = Arc::clone(&semaphore);
        let counters = Arc::clone(&counters);
        let latencies = Arc::clone(&latencies);
        handles.push(tokio::spawn(async move {
            let began = Instant::now();
            let waiting = counters.waiters.fetch_add(1, Ordering::Relaxed) + 1;
            counters.peak_waiters.fetch_max(waiting, Ordering::Relaxed);

            let permit = match policy {
                Policy::WaitForever => Some(semaphore.acquire_owned().await.unwrap()),
                Policy::NeverWait => semaphore.try_acquire_owned().ok(),
                Policy::BoundedWait => {
                    match tokio::time::timeout(MAX_WAIT, semaphore.acquire_owned()).await {
                        Ok(Ok(permit)) => Some(permit),
                        _ => None,
                    }
                }
            };
            counters.waiters.fetch_sub(1, Ordering::Relaxed);

            match permit {
                Some(permit) => {
                    tokio::time::sleep(QUERY).await; // the query
                    counters.completed.fetch_add(1, Ordering::Relaxed);
                    // `permit` is dropped here, and the slot goes back. There
                    // is no release() call to forget.
                    drop(permit);
                }
                None => {
                    counters.rejected.fetch_add(1, Ordering::Relaxed);
                }
            }
            latencies.lock().await.push(began.elapsed().as_secs_f64() * 1000.0);
        }));
    }

    let issue_window = start.elapsed();
    let issued = index;
    for handle in handles {
        let _ = handle.await;
    }
    let total = start.elapsed();

    let mut sorted = latencies.lock().await.clone();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let at = |f: f64| -> f64 {
        if sorted.is_empty() {
            return f64::NAN;
        }
        sorted[((sorted.len() as f64 * f) as usize).min(sorted.len() - 1)]
    };

    println!("  {label}");
    println!(
        "    offered              {:.0} rps ({issued} issued over {:.1}s)",
        issued as f64 / issue_window.as_secs_f64(),
        issue_window.as_secs_f64()
    );
    println!(
        "    completed            {:.0} rps ({} over the full {:.1}s)",
        counters.completed.load(Ordering::Relaxed) as f64 / total.as_secs_f64(),
        counters.completed.load(Ordering::Relaxed),
        total.as_secs_f64()
    );
    println!("    rejected             {}", counters.rejected.load(Ordering::Relaxed));
    println!(
        "    peak waiters on pool {}   <-- this is the queue",
        counters.peak_waiters.load(Ordering::Relaxed)
    );
    println!(
        "    latency p50 {:.0} ms   p95 {:.0} ms   p99 {:.0} ms   max {:.0} ms",
        at(0.50),
        at(0.95),
        at(0.99),
        at(1.0)
    );
    if total > issue_window.mul_f64(1.2) {
        println!(
            "    BACKLOG              {:.1}s of draining after the load stopped",
            (total - issue_window).as_secs_f64()
        );
    }
}
