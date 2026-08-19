// Layer 5 - Topic 2: deadline propagation through a three-hop chain, in one
// Rust process.
//
// Rust is the honest one. tokio::time::timeout is a genuinely HARD cancel --
// dropping a future stops polling it, so the work really does stop at the
// next await point, with no ambient runtime cost and no cooperation needed
// from the callee. What Rust does not have is an ambient carrier: there is
// no Context in the standard library, so the deadline is a parameter you
// thread through every signature and the compiler makes you do it. That is
// the trade in miniature -- Rust makes forgetting VISIBLE and makes
// remembering VERBOSE. Read the signatures below as the point, not as noise.
//
// And then there is spawn_blocking, which cannot be cancelled at all.
// Dropping the handle abandons the result; the thread runs to completion.
// That is a zombie you cannot kill, and variant 5 measures it.
//
// WHAT THIS DEMONSTRATES
//
//   gateway -> service_b -> service_c, where C holds a pooled connection for
//   a controlled service time. The gateway's budget is 500ms.
//
//     1 healthy              everything succeeds; the bug is invisible
//     2 naive                every hop uses the same 500ms constant, so B
//                            and C never learn what is left of the budget
//     3 deadline threaded    the absolute Instant is a parameter on every
//                            signature; B and C refuse work that cannot
//                            finish and hand a connection straight back
//                            when the request behind it is already dead
//     4 + query honours it   the query itself stops at the deadline
//     5 spawn_blocking       identical to 4, except the query runs on the
//                            blocking pool, where nothing can stop it
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. `zombie/s` -- completions C finished after the gateway had already
//      given up. One pool slot and one service time each, for a response
//      nobody will read.
//   2. Row 4 is the only row where the pool comes back down, because it is
//      the only row where the work stops rather than the waiting.
//   3. Rows 4 and 5 request exactly the same thing of the runtime and get
//      different answers. `killed/s` is zero in row 5: the deadline fired,
//      the future was dropped, and the OS thread carried on regardless.
//      A hard cancel is only as hard as the thing underneath it allows.
//
// The load generator is OPEN MODEL: Poisson arrivals, and it does not wait
// for a response before sending the next request.
//
// RUN
//   cargo run --release

use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::sync::Semaphore;
use tokio::time::{sleep, sleep_until, timeout_at, Instant as TokioInstant};

// ------------------------------------------------------------------ config

const GATEWAY_BUDGET: Duration = Duration::from_millis(500);
const SLACK: Duration = Duration::from_millis(20);
const HOP_OVERHEAD: Duration = Duration::from_millis(5);
const C_SERVICE_FAST: Duration = Duration::from_millis(40);
const C_SERVICE_SLOW: Duration = Duration::from_millis(800);
const SLOW_FRACTION: f64 = 0.25;
const C_POOL_SIZE: usize = 8;
const RATE: f64 = 50.0;
const DURATION: Duration = Duration::from_secs(12);
const GAUGE_EVERY: Duration = Duration::from_millis(20);

/// A deterministic PRNG so every variant sees the identical arrival pattern
/// and the identical set of slow requests. What differs between the rows is
/// policy, and only policy.
struct Rng(u64);

impl Rng {
    fn next_f64(&mut self) -> f64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }
    fn exp(&mut self, rate: f64) -> Duration {
        Duration::from_secs_f64(-(1.0 - self.next_f64()).ln() / rate)
    }
}

// ----------------------------------------------------------------- metrics

#[derive(Default)]
struct Metrics {
    ok: AtomicI64,
    failed: AtomicI64,
    zombie: AtomicI64,
    killed: AtomicI64,
    abandoned: AtomicI64,
    in_use: AtomicI64,
    c_latency: Mutex<Vec<f64>>,
    gauge: Mutex<Vec<f64>>,
}

// -------------------------------------------------------------- the pool

/// The connection pool, and a database server that does not care about your
/// futures. `query` holds a permit for the whole of its duration unless it
/// was given a deadline it can actually act on.
struct Pool {
    sem: Semaphore,
    m: Arc<Metrics>,
}

impl Pool {
    fn new(size: usize, m: Arc<Metrics>) -> Self {
        Pool { sem: Semaphore::new(size), m }
    }

    async fn query(
        &self,
        d: Duration,
        deadline: Option<Instant>,
        honour_deadline: bool,
        on_blocking_pool: bool,
    ) -> bool {
        let _permit = self.sem.acquire().await.unwrap();

        // Checked out. If the request that queued for this connection died
        // while it was queueing, give the connection straight back rather
        // than spend a whole service time on a corpse. Under overload this
        // is where most of the recovered capacity comes from.
        if let Some(dl) = deadline {
            if dl.saturating_duration_since(Instant::now()) < SLACK {
                self.m.abandoned.fetch_add(1, Ordering::Relaxed);
                return false;
            }
        }

        self.m.in_use.fetch_add(1, Ordering::Relaxed);
        let completed = self.run(d, deadline, honour_deadline, on_blocking_pool).await;
        self.m.in_use.fetch_add(-1, Ordering::Relaxed);
        completed
    }

    async fn run(
        &self,
        d: Duration,
        deadline: Option<Instant>,
        honour_deadline: bool,
        on_blocking_pool: bool,
    ) -> bool {
        let stop_at = match (honour_deadline, deadline) {
            (true, Some(dl)) => Some(TokioInstant::from_std(dl - SLACK)),
            _ => None,
        };

        if on_blocking_pool {
            // spawn_blocking is a one-way door. The deadline below fires on
            // time and the thread does not care: dropping the handle
            // abandons the RESULT, never the work. Which means we cannot
            // release the connection either, because the thread still has
            // it. Note what this row does NOT increment.
            let handle = tokio::task::spawn_blocking(move || std::thread::sleep(d));
            match stop_at {
                Some(at) => {
                    let mut handle = handle;
                    tokio::select! {
                        _ = &mut handle => true,
                        _ = sleep_until(at) => {
                            // The deadline fired. Now wait for the thread
                            // anyway, because there is no other option and
                            // pretending otherwise would leak the permit.
                            let _ = handle.await;
                            true
                        }
                    }
                }
                None => {
                    let _ = handle.await;
                    true
                }
            }
        } else {
            match stop_at {
                Some(at) => {
                    tokio::select! {
                        _ = sleep(d) => true,
                        _ = sleep_until(at) => {
                            // A hard cancel: the sleep future is dropped and
                            // stops being polled. In a real driver this is
                            // the moment the statement is cancelled and the
                            // connection goes back to the pool early.
                            self.m.killed.fetch_add(1, Ordering::Relaxed);
                            false
                        }
                    }
                }
                None => {
                    sleep(d).await;
                    true
                }
            }
        }
    }
}

// --------------------------------------------------------------- the hops

/// Note the signature. `deadline` is here because somebody typed it here,
/// and it will be missing from any function where somebody did not. That is
/// the entire ergonomic difference between Rust and Go on this topic, and it
/// cuts both ways: nothing is implicit, so nothing is accidentally correct.
#[allow(clippy::too_many_arguments)]
async fn service_c(
    pool: Arc<Pool>,
    m: Arc<Metrics>,
    slow: bool,
    deadline: Option<Instant>,
    gateway_deadline: Instant,
    honour_deadline: bool,
    on_blocking_pool: bool,
) -> Result<(), ()> {
    if let Some(dl) = deadline {
        if dl.saturating_duration_since(Instant::now()) < SLACK {
            // Refuse to START work that cannot finish. A request rejected
            // here costs no pool slot, no queue position, nothing at all.
            return Err(());
        }
    }
    sleep(HOP_OVERHEAD).await;

    let d = if slow { C_SERVICE_SLOW } else { C_SERVICE_FAST };
    let started = Instant::now();

    // The query is a SPAWNED task, so dropping our handle to it does not
    // cancel it. That is not a trick to make the demo work -- it is what a
    // database server does, and what any driver call already in flight does.
    let pool2 = pool.clone();
    let m2 = m.clone();
    let handle = tokio::spawn(async move {
        let completed = pool2.query(d, deadline, honour_deadline, on_blocking_pool).await;
        let finished = Instant::now();
        m2.c_latency
            .lock()
            .unwrap()
            .push(finished.duration_since(started).as_secs_f64() * 1000.0);
        if completed && finished > gateway_deadline {
            m2.zombie.fetch_add(1, Ordering::Relaxed);
        }
    });

    let local_deadline = deadline.unwrap_or_else(|| Instant::now() + GATEWAY_BUDGET);
    match timeout_at(TokioInstant::from_std(local_deadline), handle).await {
        Ok(_) => Ok(()),
        Err(_) => Err(()),
    }
}

#[allow(clippy::too_many_arguments)]
async fn service_b(
    pool: Arc<Pool>,
    m: Arc<Metrics>,
    slow: bool,
    deadline: Option<Instant>,
    gateway_deadline: Instant,
    honour_deadline: bool,
    on_blocking_pool: bool,
) -> Result<(), ()> {
    if let Some(dl) = deadline {
        if dl.saturating_duration_since(Instant::now()) < SLACK {
            return Err(());
        }
    }
    sleep(HOP_OVERHEAD).await;

    // budget_out = budget_in - elapsed_here - slack, or -- in the naive
    // variant -- the same constant everybody else used, which is the bug.
    let out = match deadline {
        Some(dl) => Some(dl - SLACK),
        None => None,
    };

    service_c(pool, m, slow, out, gateway_deadline, honour_deadline, on_blocking_pool).await
}

async fn gateway(
    pool: Arc<Pool>,
    m: Arc<Metrics>,
    slow: bool,
    propagate: bool,
    honour_deadline: bool,
    on_blocking_pool: bool,
) {
    let deadline = Instant::now() + GATEWAY_BUDGET;
    let passed = if propagate { Some(deadline) } else { None };

    let fut = service_b(pool, m.clone(), slow, passed, deadline, honour_deadline, on_blocking_pool);
    match timeout_at(TokioInstant::from_std(deadline), fut).await {
        Ok(Ok(())) => m.ok.fetch_add(1, Ordering::Relaxed),
        _ => m.failed.fetch_add(1, Ordering::Relaxed),
    };
}

// -------------------------------------------------------------- the driver

async fn run_variant(
    slow_fraction: f64,
    propagate: bool,
    honour_deadline: bool,
    on_blocking_pool: bool,
) -> Arc<Metrics> {
    let m = Arc::new(Metrics::default());
    let pool = Arc::new(Pool::new(C_POOL_SIZE, m.clone()));
    let mut rng = Rng(20250502);

    let gauge_m = m.clone();
    let sampler = tokio::spawn(async move {
        loop {
            sleep(GAUGE_EVERY).await;
            let v = gauge_m.in_use.load(Ordering::Relaxed) as f64;
            gauge_m.gauge.lock().unwrap().push(v);
        }
    });

    let begin = Instant::now();
    let end = begin + DURATION;
    let mut at = begin;
    let mut handles = Vec::new();
    loop {
        at += rng.exp(RATE);
        if at > end {
            break;
        }
        let now = Instant::now();
        if at > now {
            sleep(at - now).await;
        }
        let slow = rng.next_f64() < slow_fraction;
        handles.push(tokio::spawn(gateway(
            pool.clone(),
            m.clone(),
            slow,
            propagate,
            honour_deadline,
            on_blocking_pool,
        )));
    }
    for h in handles {
        let _ = h.await;
    }
    // Drain. Zombies are by definition still running after everyone gave up,
    // so a report taken at the end of the load would undercount them.
    sleep(C_SERVICE_SLOW + Duration::from_millis(300)).await;
    sampler.abort();
    m
}

// -------------------------------------------------------------- reporting

const HEADER: &str =
    "variant                      gw success  zombie/s  C pool in use  C p99 ms  killed/s  gaveback/s";

fn print_row(label: &str, m: &Metrics) {
    let seconds = DURATION.as_secs_f64();
    let ok = m.ok.load(Ordering::Relaxed);
    let failed = m.failed.load(Ordering::Relaxed);
    let total = ok + failed;
    let success = if total > 0 { 100.0 * ok as f64 / total as f64 } else { 0.0 };

    let mut lat = m.c_latency.lock().unwrap().clone();
    lat.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let gauge = m.gauge.lock().unwrap().clone();

    println!(
        "{:<28} {:>9.1}% {:>9.1} {:>13} {:>9.0} {:>9.1} {:>11.1}",
        label,
        success,
        m.zombie.load(Ordering::Relaxed) as f64 / seconds,
        format!("{:.1}/{}", mean(&gauge), C_POOL_SIZE),
        percentile(&lat, 99.0),
        m.killed.load(Ordering::Relaxed) as f64 / seconds,
        m.abandoned.load(Ordering::Relaxed) as f64 / seconds,
    );
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

#[tokio::main]
async fn main() {
    let fast_demand = RATE * (1.0 - SLOW_FRACTION) * C_SERVICE_FAST.as_secs_f64();
    let slow_demand = RATE * SLOW_FRACTION * C_SERVICE_SLOW.as_secs_f64();

    println!("Deadline propagation through gateway -> service_b -> service_c, in Rust.");
    println!(
        "Gateway budget {}ms, slack {}ms per hop, C pool {}, offered {} rps for {}s.",
        GATEWAY_BUDGET.as_millis(),
        SLACK.as_millis(),
        C_POOL_SIZE,
        RATE,
        DURATION.as_secs()
    );
    println!(
        "When C is unwell, {}% of queries take {}ms and the rest take {}ms.",
        SLOW_FRACTION * 100.0,
        C_SERVICE_SLOW.as_millis(),
        C_SERVICE_FAST.as_millis()
    );
    println!(
        "Demand on the pool is then {:.1} + {:.1} = {:.1} connection-seconds per second",
        slow_demand,
        fast_demand,
        slow_demand + fast_demand
    );
    println!(
        "against {} available, i.e. rho = {:.2}. None of the slow queries can beat the budget.\n",
        C_POOL_SIZE,
        (slow_demand + fast_demand) / C_POOL_SIZE as f64
    );
    println!("{}", HEADER);
    println!("{}", "-".repeat(HEADER.len()));

    print_row("1 healthy", &*run_variant(0.0, false, false, false).await);
    print_row("2 naive", &*run_variant(SLOW_FRACTION, false, false, false).await);
    print_row("3 deadline threaded", &*run_variant(SLOW_FRACTION, true, false, false).await);
    print_row("4 + query honours it", &*run_variant(SLOW_FRACTION, true, true, false).await);
    print_row("5 spawn_blocking", &*run_variant(SLOW_FRACTION, true, true, true).await);

    println!();
    println!("Rows 2 and 3: the deadline is an Instant on every signature between");
    println!("the gateway and the query. Nothing carries it for you, and nothing");
    println!("silently loses it either. Compare the two rows' `gaveback/s`: that is");
    println!("connections handed back the instant C noticed the request behind them");
    println!("was already dead.");
    println!();
    println!("Rows 4 and 5 are the Rust-specific finding, and the reason this file");
    println!("exists in Rust. Both ask the runtime for the same thing. Row 4 gets a");
    println!("hard cancel: the future is dropped, it stops being polled, the");
    println!("connection goes back early -- see `killed/s`. Row 5 asks for the same");
    println!("cancel of a spawn_blocking thread, `killed/s` stays at zero, and the");
    println!("zombies come back. tokio::time::timeout cancels a FUTURE. A thread in");
    println!("std::thread::sleep, a blocking driver call, an FFI call -- none of");
    println!("those are futures, and none of them will stop for you.");
}
