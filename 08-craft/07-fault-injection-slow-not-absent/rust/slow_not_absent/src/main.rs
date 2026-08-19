// Layer 8 Topic 7 - Rust: the timeout that cancels, and the timeout that lies.
//
// WHAT THIS DEMONSTRATES: the same fault as the lab's toxiproxy ladder -- a
// dependency that answers correctly, 20x slower -- against a real loopback TCP
// server, a real `Semaphore` pool and a fixed arrival rate. Phases A to C are
// the shape every language in this topic shows: unbounded latency, then a bound
// that converts it into fast failure.
//
// Phase D is the one only Rust can show this cleanly, and it is the reason this
// file exists. Rust has no ambient cancellation: a deadline is a value you wrap
// a future in. But DROPPING a future cancels it -- the work stops mid-await and
// the permit's `Drop` returns it to the pool before the caller has returned.
// That is a genuinely different cancellation model from every other language
// here, and it is free.
//
// The counter-example is one line of code away, and it is the most common
// mistake made when porting a deadline into Rust: `tokio::spawn` the work and
// put the timeout on the JoinHandle. The caller now gives up on schedule and the
// dashboard looks identical -- while the task keeps running, keeps holding its
// pool permit, and keeps loading the dependency. A timeout that abandons the
// caller while the work continues is not a deadline. It is a lie with better
// latency, and phase D counts exactly how many times it lied.
//
// WHAT TO LOOK FOR:
//   1. Phase B: zero errors, and a p99 many times the injected latency.
//   2. Phase C vs D: near-identical caller-side latency and error counts. The
//      difference is entirely in who is holding what -- `abandoned but
//      completed` and `connections destroyed by cancellation`.
//   3. Then the line neither of them improves: `the dependency executed N of the
//      M offered requests`, in BOTH phases, at roughly M. A client-side timeout
//      is a statement about what the caller waits for. It is not a statement
//      about what the dependency does, and this file measures the gap.
//
// Every latency is measured from the request's SCHEDULED arrival, so queueing is
// counted rather than erased.
//
//   cd rust/slow_not_absent && cargo run --release

use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

// --- knobs, in one place so the arithmetic on the page is checkable ----------

const POOL_SIZE: usize = 24; // P in Little's Law
const ARRIVAL_RPS: u64 = 400; // offered load, open model
const WINDOW_MS: u64 = 2000; // how long it is offered for
const FAST_MS: u64 = 5; // baseline dependency service time
const SLOW_MS: u64 = 100; // the injected fault: slow, not absent
const DEADLINE_MS: u64 = 250; // the per-request budget in phases C and D

// ============================================================================
// The dependency: a real TCP server whose service time is a knob, plus a
// counter of how many requests are in flight inside it at any moment. That
// counter is the thing phase D exposes and the caller's metrics cannot.
// ============================================================================

struct Dependency {
    latency_ms: AtomicI64,
    in_flight: AtomicUsize,
    peak_in_flight: AtomicUsize,
    /// Requests the dependency actually ran to completion, whether or not any
    /// caller was still listening. This is the number a client-side timeout
    /// cannot change, and phases C and D exist to make that visible.
    executed: AtomicUsize,
}

impl Dependency {
    fn new() -> Arc<Self> {
        Arc::new(Dependency {
            latency_ms: AtomicI64::new(FAST_MS as i64),
            in_flight: AtomicUsize::new(0),
            peak_in_flight: AtomicUsize::new(0),
            executed: AtomicUsize::new(0),
        })
    }

    fn set_latency(&self, ms: u64) {
        self.latency_ms.store(ms as i64, Ordering::SeqCst);
    }

    fn reset_counters(&self) {
        self.peak_in_flight.store(0, Ordering::SeqCst);
        self.executed.store(0, Ordering::SeqCst);
    }

    async fn serve(self: Arc<Self>, mut sock: TcpStream) {
        let _ = sock.write_all(b"READY\n").await;
        let mut req = [0u8; 4];
        loop {
            if sock.read_exact(&mut req).await.is_err() {
                break;
            }
            let n = self.in_flight.fetch_add(1, Ordering::SeqCst) + 1;
            self.peak_in_flight.fetch_max(n, Ordering::SeqCst);

            let ms = self.latency_ms.load(Ordering::SeqCst) as u64;
            tokio::time::sleep(Duration::from_millis(ms)).await;

            self.in_flight.fetch_sub(1, Ordering::SeqCst);
            self.executed.fetch_add(1, Ordering::SeqCst);
            if sock.write_all(b"ok\n").await.is_err() {
                break;
            }
        }
    }
}

async fn start_dependency(dep: Arc<Dependency>) -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let addr = listener.local_addr().expect("local_addr");
    tokio::spawn(async move {
        loop {
            match listener.accept().await {
                Ok((sock, _)) => {
                    let _ = sock.set_nodelay(true);
                    tokio::spawn(dep.clone().serve(sock));
                }
                Err(_) => return,
            }
        }
    });
    addr
}

// ============================================================================
// The pool. `Semaphore` is the bound; `OwnedSemaphorePermit` is the RAII half.
// Note what this buys: if the future holding a `Conn` is DROPPED -- by a
// timeout, by a `select!` losing a race, by the caller going away -- the permit
// is returned as part of unwinding, with no cleanup code anywhere. That is the
// half of Rust's cancellation model people mean when they call it good.
// ============================================================================

struct Pool {
    addr: SocketAddr,
    sem: Arc<Semaphore>,
    idle: Mutex<Vec<TcpStream>>,
    /// Connections thrown away because a future was dropped mid-response.
    dropped_midway: AtomicUsize,
}

impl Pool {
    fn new(addr: SocketAddr, size: usize) -> Arc<Self> {
        Arc::new(Pool {
            addr,
            sem: Arc::new(Semaphore::new(size)),
            idle: Mutex::new(Vec::new()),
            dropped_midway: AtomicUsize::new(0),
        })
    }

    /// `budget = None` means wait forever, which is `pool_timeout` unset --
    /// the shipped default in SQLAlchemy, in `sqlx` before you set
    /// `acquire_timeout`, and in most hand-rolled pools.
    async fn acquire(self: &Arc<Self>, budget: Option<Duration>) -> Option<Conn> {
        let permit = match budget {
            None => self.sem.clone().acquire_owned().await.ok()?,
            Some(d) => match tokio::time::timeout(d, self.sem.clone().acquire_owned()).await {
                Ok(Ok(p)) => p,
                _ => return None, // fast, honest failure
            },
        };
        let stream = {
            let mut idle = self.idle.lock().unwrap();
            idle.pop()
        };
        let stream = match stream {
            Some(s) => s,
            None => {
                let mut s = TcpStream::connect(self.addr).await.ok()?;
                let _ = s.set_nodelay(true);
                let mut hello = [0u8; 6];
                s.read_exact(&mut hello).await.ok()?;
                s
            }
        };
        Some(Conn { pool: self.clone(), stream: Some(stream), healthy: false, _permit: permit })
    }
}

/// A borrowed connection. `healthy` is set only after a complete reply has been
/// read, so a `Conn` dropped mid-await is destroyed rather than returned.
///
/// This is the hazard that comes with free cancellation: the drop is SILENT.
/// The connection has a half-delivered reply on it and the next borrower would
/// read that reply as its own. Nothing in the language warns you; you have to
/// have thought of it, here, in this `Drop`.
struct Conn {
    pool: Arc<Pool>,
    stream: Option<TcpStream>,
    healthy: bool,
    _permit: OwnedSemaphorePermit,
}

impl Drop for Conn {
    fn drop(&mut self) {
        if let Some(stream) = self.stream.take() {
            if self.healthy {
                self.pool.idle.lock().unwrap().push(stream);
            } else {
                self.pool.dropped_midway.fetch_add(1, Ordering::SeqCst);
                // stream closes here
            }
        }
    }
}

impl Conn {
    async fn round_trip(&mut self) -> std::io::Result<()> {
        let s = self.stream.as_mut().expect("borrowed");
        s.write_all(b"GET\n").await?;
        let mut reply = [0u8; 3];
        s.read_exact(&mut reply).await?; // <- cancellation lands here
        self.healthy = true;
        Ok(())
    }
}

// ============================================================================
// Measurement
// ============================================================================

#[derive(Clone, Copy, PartialEq)]
enum Outcome {
    Ok,
    PoolTimeout,
    DeadlineExceeded,
    ConnError,
}

#[derive(Default)]
struct Counters {
    lat_ms: Mutex<Vec<f64>>,
    wait_ms: Mutex<Vec<f64>>,
    svc_ms: Mutex<Vec<f64>>,
    ok: AtomicUsize,
    pool_timeout: AtomicUsize,
    deadline: AtomicUsize,
    conn_err: AtomicUsize,
    /// Requests whose caller gave up and whose work then finished anyway.
    abandoned_but_completed: AtomicUsize,
}

impl Counters {
    fn record(&self, outcome: Outcome, total_ms: f64, wait_ms: f64, svc_ms: f64) {
        self.lat_ms.lock().unwrap().push(total_ms);
        self.wait_ms.lock().unwrap().push(wait_ms);
        match outcome {
            Outcome::Ok => {
                self.ok.fetch_add(1, Ordering::SeqCst);
                self.svc_ms.lock().unwrap().push(svc_ms);
            }
            Outcome::PoolTimeout => { self.pool_timeout.fetch_add(1, Ordering::SeqCst); }
            Outcome::DeadlineExceeded => { self.deadline.fetch_add(1, Ordering::SeqCst); }
            Outcome::ConnError => { self.conn_err.fetch_add(1, Ordering::SeqCst); }
        }
    }
}

fn pct(v: &Mutex<Vec<f64>>, p: f64) -> f64 {
    let mut xs = v.lock().unwrap().clone();
    if xs.is_empty() {
        return 0.0;
    }
    xs.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let i = ((p / 100.0) * (xs.len() - 1) as f64).round() as usize;
    xs[i]
}

fn mean(v: &Mutex<Vec<f64>>) -> f64 {
    let xs = v.lock().unwrap();
    if xs.is_empty() { 0.0 } else { xs.iter().sum::<f64>() / xs.len() as f64 }
}

#[derive(Clone, Copy)]
struct Phase {
    label: &'static str,
    injected_ms: u64,
    budget: Option<Duration>,
    detached: bool,
}

struct Row {
    phase: Phase,
    offered: usize,
    ok: usize,
    pool_timeout: usize,
    deadline: usize,
    conn_err: usize,
    abandoned_completed: usize,
    dropped_conns: usize,
    wall_ms: f64,
    p50: f64,
    p99: f64,
    wait_p99: f64,
    svc_mean: f64,
    peak_in_flight: usize,
    executed: usize,
    max_lag_ms: f64,
}

/// One request. In phase C this whole future is what the timeout wraps, so a
/// deadline breach DROPS it -- and the `Conn` above goes with it.
async fn one_request(
    pool: Arc<Pool>,
    budget: Option<Duration>,
    c: Arc<Counters>,
    due: Instant,
    abandoned: Option<Arc<AtomicBool>>,
) {
    let acq = Instant::now();
    let conn = pool.acquire(budget).await;
    let wait_ms = acq.elapsed().as_secs_f64() * 1000.0;

    // If the caller already gave up, it has ALREADY recorded a deadline breach.
    // Recording again here would count one request twice -- and quietly put
    // work the caller never saw into the caller's latency histogram, which is
    // the exact confusion phase D is about.
    let give_up = |c: &Arc<Counters>| -> bool {
        match &abandoned {
            Some(f) if f.load(Ordering::SeqCst) => {
                c.abandoned_but_completed.fetch_add(1, Ordering::SeqCst);
                true
            }
            _ => false,
        }
    };

    let Some(mut conn) = conn else {
        if !give_up(&c) {
            c.record(Outcome::PoolTimeout, due.elapsed().as_secs_f64() * 1000.0, wait_ms, 0.0);
        }
        return;
    };
    let svc = Instant::now();
    let outcome = match conn.round_trip().await {
        Ok(()) => Outcome::Ok,
        Err(_) => Outcome::ConnError,
    };
    if give_up(&c) {
        return;
    }
    c.record(outcome, due.elapsed().as_secs_f64() * 1000.0, wait_ms,
             svc.elapsed().as_secs_f64() * 1000.0);
}

async fn run_phase(dep: Arc<Dependency>, addr: SocketAddr, ph: Phase) -> Row {
    dep.set_latency(ph.injected_ms);
    dep.reset_counters();
    let pool = Pool::new(addr, POOL_SIZE);
    let c = Arc::new(Counters::default());
    let total = (ARRIVAL_RPS * WINDOW_MS / 1000) as usize;
    let gap = Duration::from_nanos(1_000_000_000 / ARRIVAL_RPS);

    let t0 = Instant::now();
    let mut handles = Vec::with_capacity(total);
    let mut max_lag = 0.0f64;

    for i in 0..total {
        // OPEN MODEL: request i is due at t0 + i*gap and is launched then, whether
        // or not request i-1 finished. A closed loop would throttle itself as the
        // system slowed and would show no collapse at all.
        let due = t0 + gap * i as u32;
        tokio::time::sleep_until(tokio::time::Instant::from_std(due)).await;
        let lag = due.elapsed().as_secs_f64() * 1000.0;
        if lag > max_lag {
            max_lag = lag;
        }

        let pool = pool.clone();
        let c = c.clone();
        let budget = ph.budget;

        if !ph.detached {
            // PHASES A-C. The deadline wraps the work itself, so when it fires the
            // future is dropped: the await stops where it stands, `Conn::drop`
            // runs, the permit goes back. Cancellation is structural.
            handles.push(tokio::spawn(async move {
                match budget {
                    None => one_request(pool, budget, c, due, None).await,
                    Some(d) => {
                        if tokio::time::timeout(d, one_request(pool, budget, c.clone(), due, None))
                            .await
                            .is_err()
                        {
                            c.record(Outcome::DeadlineExceeded,
                                     due.elapsed().as_secs_f64() * 1000.0, 0.0, 0.0);
                        }
                    }
                }
            }));
        } else {
            // PHASE D. The work is spawned FIRST and the timeout is put on the
            // handle. Everything the caller measures is identical to phase C.
            // Nothing about the work changes: it keeps its permit and keeps
            // loading the dependency, and finishes into a caller that has left.
            let abandoned = Arc::new(AtomicBool::new(false));
            let flag = abandoned.clone();
            let c2 = c.clone();
            let inner = tokio::spawn(one_request(pool, None, c2, due, Some(flag)));
            let c3 = c.clone();
            handles.push(tokio::spawn(async move {
                let d = budget.unwrap_or(Duration::from_secs(60));
                if tokio::time::timeout(d, inner).await.is_err() {
                    abandoned.store(true, Ordering::SeqCst);
                    c3.record(Outcome::DeadlineExceeded,
                              due.elapsed().as_secs_f64() * 1000.0, 0.0, 0.0);
                    // and the task carries on. Nothing here can stop it.
                }
            }));
        }
    }
    for h in handles {
        let _ = h.await;
    }
    // Throughput is measured over the offer window plus the drain of work the
    // CALLERS were waiting for -- taken here, before the settle below, so the
    // settle does not deflate every rps in the table.
    let wall_ms = t0.elapsed().as_secs_f64() * 1000.0;

    // Work abandoned by a deadline is still running at this point -- in C
    // inside the dependency, in D inside this process too. Let it drain BEFORE
    // reading the counters, or its tail lands in the next phase's numbers and
    // the next phase reports more executions than it was offered. (It did,
    // while this file was being written: phase D reported 817 executions
    // against 800 offered. Bleed between phases is a cheap way to get a wrong
    // number that looks plausible.)
    tokio::time::sleep(Duration::from_millis(if ph.detached { 1500 } else { 800 })).await;

    Row {
        phase: ph,
        offered: total,
        ok: c.ok.load(Ordering::SeqCst),
        pool_timeout: c.pool_timeout.load(Ordering::SeqCst),
        deadline: c.deadline.load(Ordering::SeqCst),
        conn_err: c.conn_err.load(Ordering::SeqCst),
        abandoned_completed: c.abandoned_but_completed.load(Ordering::SeqCst),
        dropped_conns: pool.dropped_midway.load(Ordering::SeqCst),
        wall_ms,
        p50: pct(&c.lat_ms, 50.0),
        p99: pct(&c.lat_ms, 99.0),
        wait_p99: pct(&c.wait_ms, 99.0),
        svc_mean: mean(&c.svc_ms),
        peak_in_flight: dep.peak_in_flight.load(Ordering::SeqCst),
        executed: dep.executed.load(Ordering::SeqCst),
        max_lag_ms: max_lag,
    }
}

fn print_row(r: &Row) {
    let failed = r.pool_timeout + r.deadline + r.conn_err;
    let rps = r.ok as f64 / (r.wall_ms / 1000.0);
    println!(
        "  {:<30} {:>6} {:>6} {:>7.0} {:>8.0} {:>8.0} {:>9.0} {:>9.1} {:>7}",
        r.phase.label, r.offered, r.ok, rps, r.p50, r.p99, r.wait_p99, r.svc_mean,
        r.peak_in_flight
    );
    println!(
        "  {:<30} injected {} ms | pool timeouts {} | deadline exceeded {} | conn errors {} | \
         errors {:.1}% | generator max lag {:.0} ms",
        "", r.phase.injected_ms, r.pool_timeout, r.deadline, r.conn_err,
        100.0 * failed as f64 / r.offered as f64, r.max_lag_ms
    );
    println!(
        "  {:<30} the dependency executed {} of the {} offered requests",
        "", r.executed, r.offered
    );
}

#[tokio::main(flavor = "multi_thread")]
async fn main() {
    let dep = Dependency::new();
    let addr = start_dependency(dep.clone()).await;

    println!("Layer 8 topic 7 - Rust: dropping a future cancels it, and spawning one does not.");
    println!("\n  dependency: {addr}, service time is a knob");
    println!("  pool size P = {POOL_SIZE}      offered load = {ARRIVAL_RPS} rps (open model)");
    println!("  offer window = {WINDOW_MS} ms  -> {} requests per phase",
             ARRIVAL_RPS * WINDOW_MS / 1000);
    println!("  per-request budget in C and D = {DEADLINE_MS} ms\n");

    println!("  {:<30} {:>6} {:>6} {:>7} {:>8} {:>8} {:>9} {:>9} {:>7}",
             "phase", "offer", "ok", "rps", "p50 ms", "p99 ms", "wait p99", "svc mean", "peakIF");
    println!("  {}", "-".repeat(106));

    let a = run_phase(dep.clone(), addr, Phase {
        label: "A baseline, fast dep", injected_ms: FAST_MS, budget: None, detached: false,
    }).await;
    print_row(&a);

    let b = run_phase(dep.clone(), addr, Phase {
        label: "B slow dep, no bounds", injected_ms: SLOW_MS, budget: None, detached: false,
    }).await;
    print_row(&b);

    let c = run_phase(dep.clone(), addr, Phase {
        label: "C slow dep, timeout wraps work", injected_ms: SLOW_MS,
        budget: Some(Duration::from_millis(DEADLINE_MS)), detached: false,
    }).await;
    print_row(&c);

    let d = run_phase(dep.clone(), addr, Phase {
        label: "D slow dep, timeout on handle", injected_ms: SLOW_MS,
        budget: Some(Duration::from_millis(DEADLINE_MS)), detached: true,
    }).await;
    print_row(&d);

    println!("\nLITTLE'S LAW, worked from the measurements above");
    println!("  phase A: P / S = {} / {:.4} s = {:.0} rps of pool capacity, against {} rps offered",
             POOL_SIZE, a.svc_mean / 1000.0,
             if a.svc_mean > 0.0 { POOL_SIZE as f64 / (a.svc_mean / 1000.0) } else { 0.0 },
             ARRIVAL_RPS);
    println!("  phase B: P / S = {} / {:.4} s = {:.0} rps of pool capacity, against {} rps offered",
             POOL_SIZE, b.svc_mean / 1000.0,
             if b.svc_mean > 0.0 { POOL_SIZE as f64 / (b.svc_mean / 1000.0) } else { 0.0 },
             ARRIVAL_RPS);
    println!("  The pool did not shrink and the load did not rise.");

    println!("\nWHAT ACTUALLY HAPPENED");
    println!("  A -> B  p99 {:.0} ms -> {:.0} ms for a dependency that got {} ms slower;",
             a.p99, b.p99, SLOW_MS - FAST_MS);
    println!("          errors {:.1}% -> {:.1}%. Nothing failed. Everything is late.",
             100.0 * (a.pool_timeout + a.deadline + a.conn_err) as f64 / a.offered as f64,
             100.0 * (b.pool_timeout + b.deadline + b.conn_err) as f64 / b.offered as f64);
    println!("  B -> C  p99 {:.0} ms -> {:.0} ms, rps {:.0} -> {:.0}. The budget bought little or",
             b.p99, c.p99, b.ok as f64 / (b.wall_ms / 1000.0), c.ok as f64 / (c.wall_ms / 1000.0));
    println!("          no throughput and converted unbounded latency into fast failure.");

    println!("\nC AND D, THE PART THE CALLER CANNOT SEE");
    println!("  Caller-side, these two are the same experiment:");
    println!("    p99            C {:>7.0} ms      D {:>7.0} ms", c.p99, d.p99);
    println!("    deadline hits  C {:>7}         D {:>7}", c.deadline, d.deadline);
    println!("  Dependency-side, they are not:");
    println!("    peak in-flight at the dependency   C {:>5}      D {:>5}",
             c.peak_in_flight, d.peak_in_flight);
    println!("    abandoned work that ran to completion  C {:>5}  D {:>5}",
             c.abandoned_completed, d.abandoned_completed);
    println!("    connections destroyed by cancellation  C {:>5}  D {:>5}",
             c.dropped_conns, d.dropped_conns);
    println!();
    println!("  In C the timeout wraps the work, so the breach DROPS the future: the");
    println!("  await stops where it stands, `Conn::drop` runs, and the permit is back in");
    println!("  the pool before the caller has returned. In D the work was spawned first,");
    println!("  so the permit is held for the full service time and the task finishes into");
    println!("  a caller that has left. Same dashboard, different pool. That difference is");
    println!("  real, and Rust gives it to you for free.");
    println!();
    println!("  Now read the third line of each phase block above, which is the one to");
    println!("  take to work:");
    println!("    dependency executed   C {:>5} of {}      D {:>5} of {}",
             c.executed, c.offered, d.executed, d.offered);
    println!("  Cancelling a future is a CLIENT-side event. The request bytes were already");
    println!("  sent, the dependency is already working, and nothing in TCP tells it to");
    println!("  stop. C bought back a permit and paid {} destroyed connections for it.",
             c.dropped_conns);
    println!("  Neither phase reduced the load on the thing that is slow.");
    println!("  That is why the fix kit has two deadline entries and not one: a client");
    println!("  timeout bounds what the CALLER waits for, and a server-side");
    println!("  `statement_timeout` is the only thing that bounds what the DEPENDENCY");
    println!("  does. A client timeout without the server-side one converts a slow");
    println!("  dependency into a slow dependency plus connection churn.");

    println!("\nTHE RUST POINT");
    println!("  There is no ambient cancellation here and there is no `CancellationToken`");
    println!("  in the signatures above. What there is: dropping a future cancels it, and");
    println!("  `Drop` runs on the way out, so a correctly-scoped timeout releases every");
    println!("  resource with no cleanup code at all. That is genuinely better than the");
    println!("  alternatives -- and `tokio::spawn` opts out of it in one line.");
    println!("  The cost of free cancellation is the silent half: a connection dropped");
    println!("  mid-response is unusable, because the reply is still in flight. Nothing");
    println!("  tells you. `Conn::drop` above throws it away on purpose, and the count is");
    println!("  in the table -- a read timeout costs a connection, in every language here.");
    println!("  The lesson for Python: `asyncio.timeout()` around an awaited call cancels");
    println!("  like C. `asyncio.create_task` plus a timeout on the task behaves like D.");
}
