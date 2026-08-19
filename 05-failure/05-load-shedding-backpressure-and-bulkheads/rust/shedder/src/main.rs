// Layer 5 - Topic 5: load shedding, backpressure and bulkheads, in one Rust
// process.
//
// You cannot serve more than capacity. The only choice you have is whether
// the excess is rejected in one millisecond or times out after thirty seconds
// having consumed a connection, a task and a query. This file runs the same
// ramp seven times and changes only the admission decision.
//
// RUST'S ADMISSION STORY has three parts, and the third is the one no other
// language in this layer offers.
//
//   1. `Semaphore::try_acquire` is the literal definition of shedding: "is
//      there room, right now, yes or no" with no queueing and no await. It is
//      what `priority`'s tier 3 and `adaptive` use below.
//   2. `timeout(SHED_WAIT, sem.acquire())` is the bounded wait, and dropping
//      the acquire future removes the waiter from the queue with no cleanup
//      code anywhere. That is what `static` uses.
//   3. The permit is an RAII VALUE. `AdmissionTicket` below owns the permit
//      and the in-flight gauge, and releasing both is `Drop`. There is no
//      release() to forget, no `finally` to get wrong, no early-return path
//      that leaks a slot -- which is exactly the bug the other five versions
//      of this file each had to be written carefully to avoid.
//
// In a real service this lives in the middleware stack where it belongs:
// `tower::limit::ConcurrencyLimit` and `tower::load_shed` compose as layers,
// and a bounded `mpsc::Sender::try_send` returning `Err(TrySendError::Full)`
// is a TYPE you must handle -- the compiler will not let you ignore
// backpressure, which is precisely the failure mode everywhere else.
//
// WHAT THIS DEMONSTRATES
//   A backend with 8 concurrent servers at 40ms each -- 200 requests/second
//   of capacity, measured the way topic 1 measures it -- behind six different
//   admission policies, at 80% and 130% of that capacity.
//
//     none rho=0.8      the healthy baseline. Everything looks fine.
//     none rho=1.3      an UNBOUNDED queue: tokio's Semaphore is fair and
//                       will happily hold every waiter you give it.
//     static rho=1.3    a semaphore of SHED_LIMIT plus a 50ms queue-wait
//                       deadline -> 503 Retry-After.
//     priority rho=1.3  the same limit, but /checkout (tier 0) may use all
//                       of it and /search (tier 3) may not.
//     adaptive rho=1.3  no configured number at all: a gradient controller
//                       infers the limit from latency. Service time triples
//                       half way through, on purpose.
//     bulkhead          one pool of 8 shared between checkout and a slow
//                       /report endpoint, then the SAME EIGHT split 6 + 2.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. `p99_acc` and `goodput` in `none rho=1.3` against `static rho=1.3`.
//      Rejecting work should INCREASE the number of requests answered in
//      time. Check that rather than believe it.
//   2. `tier0%` in the priority row.
//   3. `limit` in the adaptive row, before and after service time triples at
//      t=6s. Reason about Little's law before calling the controller broken:
//      the ideal in-flight limit for 8 servers is about 8 however long each
//      request takes. What must fall is the RATE, not the limit.
//   4. `reject_ms`, the cost of saying no.
//
// RUN
//   cargo run --release
//
// Roughly two and a half minutes: seven scenarios of twenty seconds.

use std::collections::HashMap;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio::time::{sleep, timeout, Instant};

// ---------------------------------------------------------------- config
//
// Identical to python/shedder.py's constants: the six languages differ in how
// admission is expressed, not in what is being measured.

const WORKERS: usize = 8; // the real resource: 8 concurrent servers
const SERVICE: Duration = Duration::from_millis(40); // 8 / 0.040 = 200 rps
const CAPACITY: f64 = WORKERS as f64 / 0.040;

const RHO_LOW: f64 = 0.8;
const RHO_HIGH: f64 = 1.3;

const SLO: Duration = Duration::from_millis(500); // later than this is not goodput
// PERTURB_AT_S + MIN_RTT_RESET_S + room to watch the adaptive limit come
// back. At 12s the run ended during the dip and the return -- the half that
// shows the reset working -- was invisible.
const DURATION_S: f64 = 20.0;
const REPORT_EVERY: f64 = 2.0;

const SHED_LIMIT: usize = 12; // the knee's concurrency, measured
const SHED_WAIT: Duration = Duration::from_millis(50); // queue-wait deadline
const TIER3_LIMIT: i64 = 10; // priority: tier 3 may not use the last two
const TIER0_SHARE: f64 = 0.20;

const ADAPT_MIN: f64 = 2.0;
const ADAPT_MAX: f64 = 64.0;
const ADAPT_START: f64 = 10.0;
const ADAPT_WINDOW: Duration = Duration::from_millis(250);
const ADAPT_SMOOTHING: f64 = 0.2;
const MIN_RTT_RESET: Duration = Duration::from_secs(5);
const PERTURB_AT: f64 = 6.0;
const PERTURB_FACTOR: u32 = 3;

const CHECKOUT_RPS: f64 = 120.0;
const REPORT_RPS: f64 = 6.0;
const REPORT_SERVICE: Duration = Duration::from_millis(800); // 4.8 servers
const BULK_CHECKOUT: usize = 6; // the same 8, split. Nothing is added.
const BULK_REPORT: usize = 2;

// ------------------------------------------------------------------- rng

struct Rng(Mutex<u64>);

impl Rng {
    fn new(seed: u64) -> Self {
        Rng(Mutex::new(seed))
    }
    fn next_f64(&self) -> f64 {
        let mut s = self.0.lock().unwrap();
        *s ^= *s << 13;
        *s ^= *s >> 7;
        *s ^= *s << 17;
        (*s >> 11) as f64 / (1u64 << 53) as f64
    }
    fn exp(&self, rate: f64) -> Duration {
        Duration::from_secs_f64(-(1.0 - self.next_f64()).ln() / rate)
    }
}

// ----------------------------------------------------------- the backend

/// The resource being protected. `Semaphore` is a real bounded queue and its
/// waiters are fair -- but nothing bounds the number of WAITERS, so with no
/// admission control in front of it this is an unbounded queue with a bounded
/// server. That is mode `none`, and it is what a service looks like when
/// nobody decided.
struct Backend {
    sem: Arc<Semaphore>,
    in_use: AtomicI64,
}

impl Backend {
    fn new(workers: usize) -> Self {
        Backend {
            sem: Arc::new(Semaphore::new(workers)),
            in_use: AtomicI64::new(0),
        }
    }

    async fn call(&self, service: Duration) {
        let _permit = self.sem.acquire().await.unwrap();
        self.in_use.fetch_add(1, Ordering::Relaxed);
        struct Gauge<'a>(&'a AtomicI64);
        impl Drop for Gauge<'_> {
            fn drop(&mut self) {
                self.0.fetch_sub(1, Ordering::Relaxed);
            }
        }
        let _gauge = Gauge(&self.in_use);
        sleep(service).await;
    }
}

// ------------------------------------------------------ the gradient limit

/// Netflix `concurrency-limits` in miniature, borrowed from TCP congestion
/// control rather than from queueing theory: sample latency continuously,
/// remember the minimum you have seen, raise the in-flight limit while
/// current latency stays near that minimum, lower it when latency climbs. You
/// never configure a number; the system discovers it, and rediscovers it when
/// your code changes -- which matters because the hand-measured number from
/// topic 1 goes stale the day someone adds a join.
///
/// The non-obvious parameter is the min-RTT RESET. Without it a single fast
/// sample from a quiet moment is remembered forever, so after a genuine
/// permanent slowdown the gradient sticks near zero and the limit collapses to
/// the floor and stays there. Vegas-style controllers all re-baseline.
struct GradientLimit {
    limit: f64,
    min_rtt: Duration,
    samples: Vec<Duration>,
    last_update: Option<Instant>,
    last_reset: Option<Instant>,
}

impl GradientLimit {
    fn new() -> Self {
        GradientLimit {
            limit: ADAPT_START,
            min_rtt: Duration::from_secs(3600),
            samples: Vec::new(),
            last_update: None,
            last_reset: None,
        }
    }

    fn update(&mut self, now: Instant) {
        if let Some(last) = self.last_update {
            if now.duration_since(last) < ADAPT_WINDOW {
                return;
            }
        }
        self.last_update = Some(now);
        if self.samples.is_empty() {
            return;
        }
        self.samples.sort();
        let window_min = self.samples[0];
        let median = self.samples[self.samples.len() / 2];
        self.samples.clear();

        let stale = match self.last_reset {
            None => true,
            Some(last) => now.duration_since(last) >= MIN_RTT_RESET,
        };
        if stale {
            self.min_rtt = window_min;
            self.last_reset = Some(now);
        } else if window_min < self.min_rtt {
            self.min_rtt = window_min;
        }

        // gradient < 1 means "we are queueing"; the limit comes down in
        // proportion. The sqrt term is the queue you are willing to keep, and
        // is what stops the limit collapsing to 1 the moment one request is
        // slow.
        let gradient = (self.min_rtt.as_secs_f64() / median.as_secs_f64().max(1e-6))
            .clamp(0.5, 1.0);
        let target = self.limit * gradient + self.limit.sqrt();
        self.limit = (self.limit * (1.0 - ADAPT_SMOOTHING) + ADAPT_SMOOTHING * target)
            .clamp(ADAPT_MIN, ADAPT_MAX);
    }
}

// ---------------------------------------------------------- the admission

/// The admission ticket. Holding one means you are in flight; dropping one
/// means you are not. Both the permit and the gauge are released by `Drop`,
/// so no code path can forget them -- which is the single thing Rust brings
/// to this topic that no other runtime here does.
struct AdmissionTicket {
    _permit: Option<OwnedSemaphorePermit>,
    inflight: Arc<AtomicI64>,
}

impl Drop for AdmissionTicket {
    fn drop(&mut self) {
        self.inflight.fetch_sub(1, Ordering::Relaxed);
    }
}

/// The fifty lines. Everything above the backend and below the router.
///
/// The interesting part is what happens when you cannot have a permit
/// immediately, and there are exactly three honest answers: wait a BOUNDED
/// time (static, tier 0), refuse now (priority's tier 3, adaptive), or wait
/// forever (mode `none`, which is what you ship when you do not decide).
struct Admission {
    mode: &'static str,
    sem: Arc<Semaphore>,
    inflight: Arc<AtomicI64>,
    limiter: Option<Mutex<GradientLimit>>,
}

impl Admission {
    fn new(mode: &'static str) -> Self {
        Admission {
            mode,
            sem: Arc::new(Semaphore::new(SHED_LIMIT)),
            inflight: Arc::new(AtomicI64::new(0)),
            limiter: if mode == "adaptive" {
                Some(Mutex::new(GradientLimit::new()))
            } else {
                None
            },
        }
    }

    fn limit(&self) -> f64 {
        match &self.limiter {
            Some(l) => l.lock().unwrap().limit,
            None => SHED_LIMIT as f64,
        }
    }

    fn inflight(&self) -> i64 {
        self.inflight.load(Ordering::Relaxed)
    }

    /// Returns the ticket, or the time spent deciding not to issue one. That
    /// second number is the cost of a rejection and belongs on a dashboard: a
    /// shedder that takes 50ms to say no has spent 10% of a 500ms budget on
    /// nothing.
    async fn admit(&self, tier: u8) -> Result<AdmissionTicket, Duration> {
        let t0 = Instant::now();
        let ticket = |permit| AdmissionTicket {
            _permit: permit,
            inflight: self.inflight.clone(),
        };

        if self.mode == "none" {
            // No admission control at all. Every request is accepted and waits
            // in the backend's queue for as long as that takes, and the queue
            // has no bound because nobody gave it one.
            self.inflight.fetch_add(1, Ordering::Relaxed);
            return Ok(ticket(None));
        }

        if self.mode == "adaptive" {
            // Limit-based, no queueing: the controller's whole job is to hold
            // the limit at the value where waiting is unnecessary.
            if self.inflight() as f64 >= self.limit() {
                return Err(t0.elapsed());
            }
            self.inflight.fetch_add(1, Ordering::Relaxed);
            return Ok(ticket(None));
        }

        if self.mode == "priority" && tier > 0 {
            // Tier 3 is shed against a LOWER limit -- the last two permits are
            // reserved for tier 0 -- and gets `try_acquire_owned`, which is
            // the non-blocking form: no queue, no await, an answer now.
            if self.inflight() >= TIER3_LIMIT {
                return Err(t0.elapsed());
            }
            return match self.sem.clone().try_acquire_owned() {
                Ok(p) => {
                    self.inflight.fetch_add(1, Ordering::Relaxed);
                    Ok(ticket(Some(p)))
                }
                Err(_) => Err(t0.elapsed()),
            };
        }

        // static, and priority's tier 0: a BOUNDED wait. Dropping the acquire
        // future on timeout removes this waiter from the semaphore's queue,
        // and no cleanup code appears anywhere.
        match timeout(SHED_WAIT, self.sem.clone().acquire_owned()).await {
            Ok(Ok(p)) => {
                self.inflight.fetch_add(1, Ordering::Relaxed);
                Ok(ticket(Some(p)))
            }
            _ => Err(t0.elapsed()),
        }
    }
}

// ------------------------------------------------------------- the metrics

#[derive(Default)]
struct MetricsInner {
    offered: i64,
    accepted: i64,
    rejected: i64,
    goodput: i64,
    latencies: Vec<Duration>,
    lat_tier0: Vec<Duration>,
    reject_cost: Vec<Duration>,
    tier0_offered: i64,
    tier0_goodput: i64,
    w_offered: i64,
    w_accepted: i64,
    w_rejected: i64,
    w_goodput: i64,
    w_lat: Vec<Duration>,
    rows: Vec<Row>,
}

struct Row {
    t: f64,
    offered: f64,
    accepted: f64,
    reject: f64,
    goodput: f64,
    p99: f64,
    inflight: i64,
    limit: f64,
    busy: i64,
}

fn percentile(v: &[Duration], q: f64) -> f64 {
    if v.is_empty() {
        return 0.0;
    }
    let mut s = v.to_vec();
    s.sort();
    let idx = ((q * s.len() as f64).ceil() as usize).saturating_sub(1).min(s.len() - 1);
    s[idx].as_secs_f64() * 1000.0
}

// ------------------------------------------------------------- the server

struct Server {
    m: Mutex<MetricsInner>,
    admission: Admission,
    checkout_backend: Backend,
    report_backend: Option<Backend>, // Some() only when the pools are split
    service: Mutex<Duration>,
}

impl Server {
    fn new(mode: &'static str) -> Self {
        let admission_mode = if mode.starts_with("bulkhead") {
            "none" // bulkheads are structural, not admission
        } else {
            mode
        };
        Server {
            m: Mutex::new(MetricsInner::default()),
            admission: Admission::new(admission_mode),
            checkout_backend: Backend::new(if mode == "bulkhead_split" {
                BULK_CHECKOUT
            } else {
                WORKERS
            }),
            // The bulkhead: /report gets its own, smaller pool and is
            // structurally incapable of touching checkout's servers.
            report_backend: if mode == "bulkhead_split" {
                Some(Backend::new(BULK_REPORT))
            } else {
                None
            },
            service: Mutex::new(SERVICE),
        }
    }
}

async fn handle(server: Arc<Server>, tier: u8, is_report: bool) {
    let t0 = Instant::now();
    {
        let mut m = server.m.lock().unwrap();
        m.offered += 1;
        m.w_offered += 1;
        if tier == 0 {
            m.tier0_offered += 1;
        }
    }

    let ticket = match server.admission.admit(tier).await {
        Ok(t) => t,
        Err(cost) => {
            let mut m = server.m.lock().unwrap();
            m.rejected += 1;
            m.w_rejected += 1;
            m.reject_cost.push(cost);
            // A 503 with Retry-After, having touched nothing. That is the
            // entire product.
            return;
        }
    };

    {
        let mut m = server.m.lock().unwrap();
        m.accepted += 1;
        m.w_accepted += 1;
    }

    let service = if is_report {
        REPORT_SERVICE
    } else {
        *server.service.lock().unwrap()
    };
    let backend = match (is_report, &server.report_backend) {
        (true, Some(b)) => b,
        _ => &server.checkout_backend,
    };
    backend.call(service).await;
    drop(ticket); // explicit only for the reader; the scope end would do it

    let latency = t0.elapsed();
    {
        let mut m = server.m.lock().unwrap();
        m.latencies.push(latency);
        m.w_lat.push(latency);
        if tier == 0 {
            m.lat_tier0.push(latency);
        }
        if latency <= SLO {
            m.goodput += 1;
            m.w_goodput += 1;
            if tier == 0 {
                m.tier0_goodput += 1;
            }
        }
    }
    if let Some(l) = &server.admission.limiter {
        l.lock().unwrap().samples.push(latency);
    }
}

// ------------------------------------------------------------- the harness

struct Scenario {
    key: &'static str,
    mode: &'static str,
    label: String,
    note: String,
    rate: f64,
    tier0_share: f64,
    report_rps: f64,
}

async fn run_scenario(sc: &Scenario) -> Arc<Server> {
    let server = Arc::new(Server::new(sc.mode));
    let rng = Rng::new(20250505);

    let begin = Instant::now();
    let mut last_report = begin;
    let mut at = begin;
    let mut next_report = begin;
    let mut perturbed = false;

    loop {
        let t_planned = at.duration_since(begin).as_secs_f64();
        if t_planned > DURATION_S {
            break;
        }
        at += rng.exp(sc.rate);
        tokio::time::sleep_until(at).await;
        let now = Instant::now();
        let t = now.duration_since(begin).as_secs_f64();

        if sc.mode == "adaptive" && !perturbed && t >= PERTURB_AT {
            // "Then change service time by 3x at runtime and watch it
            // re-converge." Nobody redeployed. Nobody changed the limit.
            *server.service.lock().unwrap() = SERVICE * PERTURB_FACTOR;
            perturbed = true;
        }

        let tier = if rng.next_f64() < sc.tier0_share { 0u8 } else { 3u8 };
        tokio::spawn(handle(server.clone(), tier, false));

        // The slow endpoint, offered as its own open-model stream rather than
        // as a fraction of checkout: reports do not arrive because checkouts
        // do.
        // Note `next_report +` and the `while`, not `now +` and an `if`: this
        // is an ABSOLUTE schedule, exactly like `at` above. Rescheduling from
        // `now` throws away the lateness of every arrival, and since the check
        // only runs when a checkout arrives, the lateness is real and it grows
        // with load -- so the relative version quietly offers LESS /report the
        // more overloaded the server gets, which is backwards and hides the
        // very effect this scenario exists to show.
        while sc.report_rps > 0.0 && now >= next_report {
            next_report += rng.exp(sc.report_rps);
            tokio::spawn(handle(server.clone(), 3, true));
        }

        if let Some(l) = &server.admission.limiter {
            l.lock().unwrap().update(now);
        }

        if now.duration_since(last_report).as_secs_f64() >= REPORT_EVERY {
            let span = now.duration_since(last_report).as_secs_f64();
            let limit = server.admission.limit();
            let inflight = server.admission.inflight();
            let busy = server.checkout_backend.in_use.load(Ordering::Relaxed);
            let mut m = server.m.lock().unwrap();
            let row = Row {
                t,
                offered: sc.rate,
                accepted: m.w_accepted as f64 / span,
                reject: 100.0 * m.w_rejected as f64 / (m.w_offered.max(1)) as f64,
                goodput: m.w_goodput as f64 / span,
                p99: percentile(&m.w_lat, 0.99),
                inflight,
                limit,
                busy,
            };
            m.rows.push(row);
            m.w_offered = 0;
            m.w_accepted = 0;
            m.w_rejected = 0;
            m.w_goodput = 0;
            m.w_lat.clear();
            last_report = now;
        }
    }

    // Let the tail drain: requests still in flight at the end of the window
    // are neither goodput nor rejections, and counting them either way would
    // be a lie about the run.
    sleep(Duration::from_secs(1)).await;
    server
}

// -------------------------------------------------------------- reporting

const HEADER: &str =
    "      t   offered  accepted  reject%   goodput  p99_acc  inflight  limit   busy";

struct Summary {
    key: &'static str,
    offered: f64,
    accepted: f64,
    rejected: f64,
    goodput: f64,
    p99: f64,
    p99_t0: f64,
    tier0: f64,
    reject_ms: f64,
}

fn render(sc: &Scenario, server: &Server) -> Summary {
    println!("\n=== {} ===", sc.label);
    println!("    {}", sc.note);
    println!("{}", HEADER);
    println!("{}", "-".repeat(HEADER.len()));
    let m = server.m.lock().unwrap();
    for r in &m.rows {
        let mark = if sc.mode == "adaptive" && (r.t - PERTURB_AT).abs() < REPORT_EVERY / 2.0 {
            "  <-- service time x3"
        } else {
            ""
        };
        println!(
            "  {:5.1} {:9.1} {:9.1} {:8.0} {:9.1} {:8.0} {:9} {:6.1} {:6}{}",
            r.t, r.offered, r.accepted, r.reject, r.goodput, r.p99, r.inflight, r.limit, r.busy, mark
        );
    }
    let reject_ms = if m.reject_cost.is_empty() {
        0.0
    } else {
        m.reject_cost.iter().map(|d| d.as_secs_f64() * 1000.0).sum::<f64>()
            / m.reject_cost.len() as f64
    };
    let out = Summary {
        key: sc.key,
        offered: m.offered as f64 / DURATION_S,
        accepted: m.accepted as f64 / DURATION_S,
        rejected: 100.0 * m.rejected as f64 / m.offered.max(1) as f64,
        goodput: m.goodput as f64 / DURATION_S,
        p99: percentile(&m.latencies, 0.99),
        p99_t0: percentile(&m.lat_tier0, 0.99),
        tier0: 100.0 * m.tier0_goodput as f64 / m.tier0_offered.max(1) as f64,
        reject_ms,
    };
    println!(
        "mode={}  offered={:.0}  accepted={:.0}  rejected={:.0}%  goodput={:.0}  \
         p99_accepted={:.0}ms  tier0_success={:.0}%  p99_tier0={:.0}ms  reject_ms={:.1}",
        out.key, out.offered, out.accepted, out.rejected, out.goodput, out.p99, out.tier0,
        out.p99_t0, out.reject_ms
    );
    out
}

#[tokio::main]
async fn main() {
    println!("Load shedding, backpressure and bulkheads: the same ramp, seven admission policies.");
    println!(
        "Backend capacity is {}/{:.3} = {:.0} rps, measured the way topic 1 measures it. \
         Anything above that is not servable by anybody.",
        WORKERS,
        SERVICE.as_secs_f64(),
        CAPACITY
    );
    println!(
        "Offered load is {:.1}x and {:.1}x that number. Goodput counts responses inside a \
         {}ms SLO; p99_acc is the p99 of ACCEPTED requests, p99_tier0 the p99 of tier-0 \
         (/checkout) requests alone.",
        RHO_LOW,
        RHO_HIGH,
        SLO.as_millis()
    );
    println!(
        "The static limit is {} in flight with a {}ms queue-wait deadline. The adaptive one is \
         not configured at all.",
        SHED_LIMIT,
        SHED_WAIT.as_millis()
    );

    let scenarios = vec![
        Scenario {
            key: "none_0.8",
            mode: "none",
            label: "1 none, rho=0.8".to_string(),
            note: "The healthy baseline. Nothing is rejected because nothing needs to be."
                .to_string(),
            rate: RHO_LOW * CAPACITY,
            tier0_share: TIER0_SHARE,
            report_rps: 0.0,
        },
        Scenario {
            key: "none_1.3",
            mode: "none",
            label: "2 none, rho=1.3".to_string(),
            note: "An unbounded queue at 130% of capacity. Watch p99_acc climb while reject% \
                   stays at zero."
                .to_string(),
            rate: RHO_HIGH * CAPACITY,
            tier0_share: TIER0_SHARE,
            report_rps: 0.0,
        },
        Scenario {
            key: "static_1.3",
            mode: "static",
            label: "3 static shedding, rho=1.3".to_string(),
            note: format!(
                "A semaphore of {} plus a {}ms wait deadline -> 503 Retry-After.",
                SHED_LIMIT,
                SHED_WAIT.as_millis()
            ),
            rate: RHO_HIGH * CAPACITY,
            tier0_share: TIER0_SHARE,
            report_rps: 0.0,
        },
        Scenario {
            key: "priority_1.3",
            mode: "priority",
            label: "4 priority shedding, rho=1.3".to_string(),
            note: format!(
                "/checkout is tier 0 ({:.0}% of traffic) and may use all {}; /search is tier 3 \
                 and may use {}.",
                TIER0_SHARE * 100.0,
                SHED_LIMIT,
                TIER3_LIMIT
            ),
            rate: RHO_HIGH * CAPACITY,
            tier0_share: TIER0_SHARE,
            report_rps: 0.0,
        },
        Scenario {
            key: "adaptive_1.3",
            mode: "adaptive",
            label: "5 adaptive shedding, rho=1.3".to_string(),
            note: format!(
                "No configured limit. Service time triples at t={:.0}s with nobody redeploying \
                 anything.",
                PERTURB_AT
            ),
            rate: RHO_HIGH * CAPACITY,
            tier0_share: TIER0_SHARE,
            report_rps: 0.0,
        },
        Scenario {
            key: "bulk_shared",
            mode: "bulkhead_shared",
            label: "6 bulkhead: one shared pool".to_string(),
            note: format!(
                "{:.0} rps of checkout plus {:.0} rps of {}ms /report, all {} servers shared.",
                CHECKOUT_RPS,
                REPORT_RPS,
                REPORT_SERVICE.as_millis(),
                WORKERS
            ),
            rate: CHECKOUT_RPS,
            tier0_share: 1.0,
            report_rps: REPORT_RPS,
        },
        Scenario {
            key: "bulk_split",
            mode: "bulkhead_split",
            label: format!("7 bulkhead: the same 8, split {} + {}", BULK_CHECKOUT, BULK_REPORT),
            note: "Nothing is added. /report is now structurally incapable of touching \
                   checkout's servers."
                .to_string(),
            rate: CHECKOUT_RPS,
            tier0_share: 1.0,
            report_rps: REPORT_RPS,
        },
    ];

    let mut summaries: Vec<(String, Summary)> = Vec::new();
    for sc in &scenarios {
        let server = run_scenario(sc).await;
        summaries.push((sc.label.clone(), render(sc, &server)));
    }

    println!("\n{}", "=".repeat(104));
    println!(
        "{:<38}{:>8}{:>9}{:>8}{:>8}{:>8}{:>9}{:>10}{:>10}",
        "mode", "offered", "accepted", "goodput", "p99_acc", "p99_t0", "reject%", "tier0_ok%",
        "reject_ms"
    );
    println!("{}", "-".repeat(104));
    let mut by_key: HashMap<&str, &Summary> = HashMap::new();
    for (label, s) in &summaries {
        by_key.insert(s.key, s);
        println!(
            "{:<38}{:>8.0}{:>9.0}{:>8.0}{:>8.0}{:>8.0}{:>9.0}{:>10.0}{:>10.1}",
            label, s.offered, s.accepted, s.goodput, s.p99, s.p99_t0, s.rejected, s.tier0,
            s.reject_ms
        );
    }

    let none13 = by_key["none_1.3"];
    let static13 = by_key["static_1.3"];
    println!("\nRead rows 2 and 3 as one comparison and everything else is commentary:");
    println!(
        "  none     rho=1.3   goodput {:6.0} rps   p99 {:6.0} ms   rejected {:.0}%",
        none13.goodput, none13.p99, none13.rejected
    );
    println!(
        "  static   rho=1.3   goodput {:6.0} rps   p99 {:6.0} ms   rejected {:.0}%",
        static13.goodput, static13.p99, static13.rejected
    );
    println!("Same offered load, same backend, same 200 rps of capacity. The only");
    println!("difference is that one of them said no.");

    let shared = by_key["bulk_shared"];
    let split = by_key["bulk_split"];
    println!("\nThe bulkhead pair is the other comparison worth making, and it is the one");
    println!("that adds nothing at all:");
    println!(
        "  shared pool   checkout goodput {:6.0} rps   checkout p99 {:6.0} ms",
        shared.goodput, shared.p99_t0
    );
    println!(
        "  split {} + {}   checkout goodput {:6.0} rps   checkout p99 {:6.0} ms",
        BULK_CHECKOUT, BULK_REPORT, split.goodput, split.p99_t0
    );
    println!("The split pool has FEWER servers available to checkout, and the boundary is");
    println!(
        "worth more than the two servers it costs -- because /report at {:.0} rps x {}ms wants",
        REPORT_RPS,
        REPORT_SERVICE.as_millis()
    );
    println!(
        "{:.1} servers' worth of the shared pool and takes them from whoever asks last. Note",
        REPORT_RPS * REPORT_SERVICE.as_secs_f64()
    );
    println!(
        "what it costs: /report itself can now only ever get {:.1} rps through. That is the",
        BULK_REPORT as f64 / REPORT_SERVICE.as_secs_f64()
    );
    println!("bargain, and you should be able to say it out loud before you make it.");
    println!("\nThree things to carry out of this file:");
    println!("  1. An unbounded queue does not smooth load. It converts an availability");
    println!("     problem into a latency problem and hides it until latency exceeds every");
    println!("     timeout in the system at once.");
    println!("  2. Shed on WAIT TIME, not on queue length. Length is meaningless without a");
    println!("     service time attached: the same length is a healthy queue for a 1ms handler");
    println!("     and a catastrophe for a 500ms one.");
    println!("  3. In Rust specifically: make the ticket an RAII value, as AdmissionTicket is");
    println!("     here, and the entire class of \"we forgot to release it on the error path\"");
    println!("     bugs stops being possible rather than being caught in review.");
}
