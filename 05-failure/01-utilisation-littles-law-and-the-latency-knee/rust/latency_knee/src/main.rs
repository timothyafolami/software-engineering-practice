// Layer 5 - Topic 1: the latency knee in Rust.
//
// WHAT THIS DEMONSTRATES
//   Rust has no default concurrency limit and no default runtime, so every
//   bound in a Rust service is one somebody chose. That makes it the
//   cleanest place in this lab to separate two things people routinely
//   conflate: how many threads you have, and how much concurrency your
//   scarcest resource permits.
//
//   Sweeps 1 and 2 vary the permit count on a `tokio::sync::Semaphore`
//   standing in for a connection pool, and reproduce the same 1/(1-rho)
//   knee as every other language here. Sweep 3 changes the runtime flavour
//   instead -- `multi_thread` (a worker per core) down to `current_thread`
//   (one thread for everything) -- and changes nothing at all, because the
//   permit count was never about CPU.
//
//   Worth knowing while reading: `Semaphore::acquire` is FIFO-fair, so this
//   pool queues in arrival order. That is the right default and it is also
//   exactly the wrong policy under overload, where the oldest request is
//   the one whose caller has already gone. Topic 5, adaptive LIFO.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//  1. `achieved` plateaus at permits / service time: lambda_max = L / W.
//  2. p99 tracks the S/(1-rho) column until the queue stops draining.
//  3. `wait p50` is ~0 at rho=0.2 and is most of the latency by rho=0.95.
//     The work never got slower. The waiting room got longer.
//  4. Doubling the permits moves capacity and the knee proportionally.
//  5. The two runtime flavours produce the same capacity. If you have a
//     latency problem and your first instinct is more worker threads, this
//     is the row that says why it will not help.
//
// RUN
//	cargo run --release
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::sync::Semaphore;

const SERVICE: Duration = Duration::from_millis(40);
const STEP: Duration = Duration::from_secs(8);
const GAUGE_EVERY: Duration = Duration::from_millis(20);
const POOL_SIZES: [usize; 2] = [5, 10];
const RHOS: [f64; 6] = [0.2, 0.5, 0.8, 0.9, 0.95, 1.1];

/// A tiny xorshift, because this crate is deliberately dependency-free
/// apart from the runtime itself. Quality is irrelevant here; we need
/// exponential gaps, not cryptography.
struct Rng(u64);

impl Rng {
    fn next_f64(&mut self) -> f64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }

    /// Exponential inter-arrival gaps make a Poisson process: the standard
    /// model for independent users arriving. Evenly spaced arrivals would
    /// understate the queue, because bursts are what fill it.
    fn exponential(&mut self, rate: f64) -> f64 {
        -(1.0 - self.next_f64()).ln() / rate
    }
}

#[derive(Default)]
struct Samples {
    total: Vec<f64>,
    wait: Vec<f64>,
    completions: Vec<Instant>,
    gauge: Vec<f64>,
}

struct StepResult {
    offered: f64,
    achieved: f64,
    p50: f64,
    p99: f64,
    wait_p50: f64,
    mean_total: f64,
    gauge_l: f64,
}

/// One measurement step at a fixed offered rate.
///
/// OPEN MODEL, and specifically: each request's clock starts at the time it
/// was *scheduled* to arrive, not at the time this program got round to
/// spawning it. A generator that starts the clock at dispatch forgives
/// itself for running late. Real users do not wait for your executor before
/// deciding to click. Topic 6 is entirely about this distinction.
async fn step(pool: Arc<Semaphore>, rate: f64, dur: Duration, seed: u64) -> StepResult {
    let samples = Arc::new(Mutex::new(Samples::default()));
    let inflight = Arc::new(AtomicI64::new(0));
    let begin = Instant::now();
    let deadline = begin + dur;

    let gauge_samples = samples.clone();
    let gauge_inflight = inflight.clone();
    let gauge = tokio::spawn(async move {
        let mut ticker = tokio::time::interval(GAUGE_EVERY);
        loop {
            ticker.tick().await;
            let n = gauge_inflight.load(Ordering::Relaxed) as f64;
            gauge_samples.lock().unwrap().gauge.push(n);
        }
    });

    let mut rng = Rng(seed | 1);
    let mut handles = Vec::new();
    let mut at = begin;
    let mut sent = 0usize;
    loop {
        at += Duration::from_secs_f64(rng.exponential(rate));
        if at > deadline {
            break;
        }
        tokio::time::sleep_until(at.into()).await;
        sent += 1;
        let pool = pool.clone();
        let samples = samples.clone();
        let inflight = inflight.clone();
        handles.push(tokio::spawn(async move {
            inflight.fetch_add(1, Ordering::Relaxed);
            let permit = pool.acquire_owned().await.unwrap();
            let acquired = Instant::now();
            // The "work": an awaited query. It holds the permit for its
            // whole duration, which is the only reason the permit count
            // caps throughput at all.
            tokio::time::sleep(SERVICE).await;
            drop(permit);
            let done = Instant::now();
            inflight.fetch_sub(1, Ordering::Relaxed);
            let mut s = samples.lock().unwrap();
            s.wait.push(acquired.saturating_duration_since(at).as_secs_f64() * 1000.0);
            s.total.push(done.saturating_duration_since(at).as_secs_f64() * 1000.0);
            s.completions.push(done);
        }));
    }

    // Drain. Past rho=1 this is where the backlog built up during the step
    // finally comes out, which is why those rows carry latencies larger
    // than the step itself.
    for h in handles {
        let _ = h.await;
    }
    gauge.abort();

    let mut s = samples.lock().unwrap();
    s.total.sort_by(|a, b| a.partial_cmp(b).unwrap());
    s.wait.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let seconds = dur.as_secs_f64();
    let in_window = s.completions.iter().filter(|c| **c <= deadline).count();
    StepResult {
        offered: sent as f64 / seconds,
        achieved: in_window as f64 / seconds,
        p50: percentile(&s.total, 50.0),
        p99: percentile(&s.total, 99.0),
        wait_p50: percentile(&s.wait, 50.0),
        // Little's Law is a statement about MEANS. L = lambda * p50 is not
        // a law, and it stops holding exactly when the distribution skews,
        // which is exactly when you reach for it.
        mean_total: mean(&s.total),
        gauge_l: mean(&s.gauge),
    }
}

fn percentile(sorted: &[f64], p: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let k = ((p / 100.0) * (sorted.len() - 1) as f64).round() as usize;
    sorted[k.min(sorted.len() - 1)]
}

fn mean(v: &[f64]) -> f64 {
    if v.is_empty() {
        0.0
    } else {
        v.iter().sum::<f64>() / v.len() as f64
    }
}

const HEADER: &str = "  rho   offered  achieved      p50      p99   wait p50   L (gauge)   lam*Wbar   S/(1-rho)";

fn print_row(rho: f64, r: &StepResult, service: f64) {
    let predicted = if rho < 1.0 {
        format!("{:9.1}", service / (1.0 - rho) * 1000.0)
    } else {
        "      inf".to_string()
    };
    println!(
        "{:5.2} {:9.1} {:9.1} {:8.1} {:8.1} {:10.1} {:11.1} {:10.1} {}",
        rho,
        r.offered,
        r.achieved,
        r.p50,
        r.p99,
        r.wait_p50,
        r.gauge_l,
        r.achieved * r.mean_total / 1000.0,
        predicted
    );
}

async fn sweep(size: usize, label: &str) -> Vec<f64> {
    let pool = Arc::new(Semaphore::new(size));

    // Measure S rather than assuming SERVICE. A capacity computed from a
    // constant nobody measured is the commonest way this experiment lies.
    let warm = step(pool.clone(), 5.0, Duration::from_secs(2), 0x9E3779B9).await;
    let service = warm.mean_total / 1000.0;
    let capacity = size as f64 / service;

    println!("\n=== {label}: {size} permits, measured service time S = {:.1} ms ===", service * 1000.0);
    println!("predicted capacity L/S = {capacity:.1} rps\n");
    println!("{HEADER}");
    println!("{}", "-".repeat(HEADER.len()));

    let mut p99s = Vec::new();
    for (i, rho) in RHOS.iter().enumerate() {
        let r = step(pool.clone(), capacity * rho, STEP, 0xDEADBEEF + i as u64 * 7919).await;
        print_row(*rho, &r, service);
        p99s.push(r.p99);
    }
    p99s
}

/// The knee is a shape, and a table of numbers hides shapes.
fn chart(p99s: &[f64]) {
    let top = p99s.iter().cloned().fold(1.0f64, f64::max);
    println!("\n  p99 (ms) against rho");
    for (i, v) in p99s.iter().enumerate() {
        let n = ((56.0 * v / top).round() as usize).max(1);
        println!("  rho={:<6.2}|{} {:.0}", RHOS[i], "#".repeat(n), v);
    }
    println!("  {:10}+{} {:.0} ms full scale", "", "-".repeat(56), top);
}

fn main() {
    println!("Latency knee in Rust: one bounded resource, open-model Poisson arrivals.");
    println!("Nothing here is CPU-bound. Every limit in this program is a permit count.");

    let multi = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .unwrap();
    for size in POOL_SIZES {
        let p99s = multi.block_on(sweep(size, "multi_thread runtime"));
        chart(&p99s);
    }

    // Same permit count, one OS thread for the entire program. If capacity
    // were about threads this would collapse. It does not, because the
    // handler awaits rather than computes: the permits are the resource.
    println!("\n\n=== same 10 permits, current_thread runtime (one OS thread total) ===");
    let single = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    single.block_on(async {
        let pool = Arc::new(Semaphore::new(10));
        let warm = step(pool.clone(), 5.0, Duration::from_secs(2), 0x1234_5678).await;
        let service = warm.mean_total / 1000.0;
        let capacity = 10.0 / service;
        println!("measured service time S = {:.1} ms, capacity L/S = {capacity:.1} rps\n", service * 1000.0);
        println!("{HEADER}");
        println!("{}", "-".repeat(HEADER.len()));
        for (i, rho) in [0.5f64, 0.95].iter().enumerate() {
            let r = step(pool.clone(), capacity * rho, STEP, 0xC0FFEE + i as u64).await;
            print_row(*rho, &r, service);
        }
    });

    println!("\n  Compare these two rows against the 10-permit multi_thread sweep above.");
    println!("  Eight worker threads and one worker thread give the same capacity and");
    println!("  the same knee. The resource under contention was never a thread.");
    println!("\nThe two permit sweeps ran identical code and an identical ramp; the only");
    println!("difference is the permit count. Compare their capacity lines and their");
    println!("rho=0.9 rows: that is the whole topic in two numbers.");
}
