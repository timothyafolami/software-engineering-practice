//! Layer 5 - Topic 6: fan-out tails, hedging, and coordinated omission (Rust).
//!
//! One process holds a gateway, up to 50 backends and BOTH load models, so the
//! only thing missing versus the containerised version is real network
//! variance. Everything else -- the arithmetic of percentiles under fan-out,
//! the cost of hedging, and the lie a closed-loop generator tells -- is here.
//! Same constants, same phases and same columns as `../../python/fanout.py`,
//! `../../golang/fanout.go` and `../../nodejs/fanout.js`, so the tables line up.
//!
//! RUST CANCELS BY DROPPING, AND THAT IS THE WHOLE PER-LANGUAGE POINT
//!
//! `tokio::select!` on two futures drops the loser the instant the winner
//! completes, and dropping a future stops polling it. The hedge cleanup that
//! Python asks you to remember (`.cancel()`), that Go gives you as a
//! `context.CancelFunc` and that Node makes you wire up through an
//! `AbortController` is, here, a consequence of the ownership rules: there is
//! no line of code to forget, because the cleanup is the `}`.
//!
//! The cost is the mirror image, and phase B measures it rather than asserting
//! it. `Backend::call` holds a `SemaphorePermit` for its service time. When the
//! future is dropped mid-flight, the permit's `Drop` returns it to the
//! semaphore whether or not you had thought about that being the right moment
//! -- which is correct for a worker slot and would be a bug for anything with
//! partial state (a half-written buffer, a half-consumed stream). That is what
//! "cancellation safety" means in practice, and the third row of phase B
//! exists to price it: `hedge @p95, loser LEAKED` deliberately holds the loser
//! alive in a `tokio::spawn` so it cannot be dropped, which is what you get if
//! you reach for `spawn` to "keep things tidy" and never cancel the handle.
//!
//! WHAT THIS DEMONSTRATES
//!
//!   Phase A  A gateway fans out to K identical backends and waits for all of
//!            them, K in {1,2,5,10,20,50}, against two service-time
//!            distributions that share a p50 of 10ms and a p99 of 200ms:
//!            log-normal, and bimodal with a 1% slow mode. Backends are
//!            deliberately unsaturated here, so the only thing acting is the
//!            arithmetic.
//!   Phase B  Hedging at the MEASURED backend p95, under a 5% token bucket:
//!            no hedge, loser dropped by `select!`, and loser leaked.
//!   Phase C  The same server, the same nominal rate, measured twice: once by
//!            an open-model generator (arrivals on a fixed schedule) and once
//!            by a closed-loop one (a fixed number of virtual users, each
//!            waiting for a response before sending again).
//!
//! WHAT TO LOOK FOR IN THE OUTPUT
//!
//!   1. Phase A's `measured` column against `predicted`, which is 1 - 0.99^K
//!      and is arithmetic, not measurement. If the two disagree badly, read
//!      the README's "what would mean the experiment is broken" list before
//!      believing either.
//!   2. The two distributions' `e2e_p50` columns diverging as K grows while
//!      their tail columns stay together. Same p50, same p99, same tail
//!      probability, different shape -- and the shape is what the user feels.
//!   3. Phase B's `svc_ms/req`: the backend service time actually consumed per
//!      request. It is the column that separates the dropped and leaked rows,
//!      which issue exactly the same calls.
//!   4. Phase C's two p99s and the two histograms underneath them. The closed
//!      loop also prints an omission-corrected p99, measured from when each
//!      request was DUE rather than when the generator got round to sending
//!      it. The gap between raw and corrected is the size of the lie.
//!
//! A NOTE ON THE TIMER FLOOR: tokio's timer wheel resolves to roughly a
//! millisecond and the p50 here is 10ms. Read the calibration block first --
//! it prints what the backend distribution actually measured as, not what it
//! was configured as, and every later table is relative to those numbers.
//!
//! RUN
//!     cargo run --release
//!
//! tokio only. Takes roughly three minutes.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::sync::Semaphore;
use tokio::time::{sleep, sleep_until, Instant};

// ------------------------------------------------------------------ config

const BACKEND_P50_MS: f64 = 10.0;
const TAIL_RATIO: f64 = 20.0; // p99 / p50, per the README's specification
const Z99: f64 = 2.3263478740408408;
const TAIL_THRESHOLD_MS: f64 = BACKEND_P50_MS * TAIL_RATIO; // 200.0ms, by construction

const K_VALUES: [usize; 6] = [1, 2, 5, 10, 20, 50];
const SAMPLES_PER_CELL: usize = 1500;
const MAX_RATE: f64 = 400.0; // requests/s ceiling for a cell
const MAX_BACKEND_CALLS_PER_S: f64 = 10_000.0;
const STAT_WORKERS: usize = 512; // phase A: backends must NOT queue

const HEDGE_K: [usize; 2] = [10, 50];
const HEDGE_BUDGET_RATIO: f64 = 0.05; // "at most 5% of backend calls may hedge"
const HEDGE_BUCKET_CAPACITY: f64 = 20.0;

const CO_K: usize = 10;
const CO_WORKERS: usize = 4; // phase C: backends that CAN saturate
const CO_RHO: f64 = 0.90;
const CO_SECONDS: f64 = 25.0;

const CALIB_SAMPLES: usize = 20_000;
const CALIB_BATCH: usize = 500;
const SEED: u64 = 20260819;

fn lognormal_sigma() -> f64 {
    TAIL_RATIO.ln() / Z99
}

/// Nearest-rank percentile. No interpolation, and above all no averaging of
/// percentiles, which is the arithmetic sin this topic is about.
fn pct(sorted: &[f64], q: f64) -> f64 {
    if sorted.is_empty() {
        return f64::NAN;
    }
    let idx = ((q * sorted.len() as f64).ceil() as isize - 1).clamp(0, sorted.len() as isize - 1);
    sorted[idx as usize]
}

/// A seeded xorshift64*, shared behind a Mutex. The std library ships no RNG
/// and this program takes no dependencies beyond tokio; contention is
/// irrelevant because each draw is followed by a millisecond-scale sleep.
struct Rng(Mutex<u64>);

impl Rng {
    fn new(seed: u64) -> Self {
        Rng(Mutex::new(seed | 1))
    }
    fn unit(&self) -> f64 {
        let mut s = self.0.lock().unwrap();
        let mut x = *s;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        *s = x;
        ((x.wrapping_mul(0x2545_F491_4F6C_DD1D) >> 11) as f64) / ((1u64 << 53) as f64)
    }
    /// Box-Muller. No cached second value: pairing would make the sample
    /// stream depend on task interleaving, which is not deterministic here.
    fn gauss(&self) -> f64 {
        let mut u = self.unit();
        while u <= 0.0 {
            u = self.unit();
        }
        (-2.0 * u.ln()).sqrt() * (2.0 * std::f64::consts::PI * self.unit()).cos()
    }
    fn expo(&self, rate: f64) -> f64 {
        let mut u = self.unit();
        while u <= 0.0 {
            u = self.unit();
        }
        -u.ln() / rate
    }
}

// ----------------------------------------------------------- distributions

#[derive(Clone, Copy)]
enum Dist {
    /// p50 = exp(mu); p99 = exp(mu + z99*sigma), sigma chosen so p99/p50 = 20.
    LogNormal { mu: f64, sigma: f64 },
    /// 99% fast and tight, 1% slow -- and the slow mode's FLOOR is the p99.
    ///
    /// Putting the slow mode's minimum exactly at 20x the p50 is what makes
    /// P(leg > 200ms) equal 1% on the nose, so the same tail threshold works
    /// for both distributions and `predicted` stays honest. A slow mode centred
    /// on 200ms would put only half of 1% above the threshold, and the
    /// predicted/measured comparison would compare two different things.
    Bimodal {
        fast_mu: f64,
        fast_sigma: f64,
        slow_floor: f64,
        slow_extra: f64,
        p_slow: f64,
    },
}

impl Dist {
    fn name(&self) -> &'static str {
        match self {
            Dist::LogNormal { .. } => "lognormal",
            Dist::Bimodal { .. } => "bimodal",
        }
    }
    fn sample(&self, rng: &Rng) -> Duration {
        let secs = match *self {
            Dist::LogNormal { mu, sigma } => (mu + sigma * rng.gauss()).exp(),
            Dist::Bimodal {
                fast_mu,
                fast_sigma,
                slow_floor,
                slow_extra,
                p_slow,
            } => {
                if rng.unit() < p_slow {
                    slow_floor + rng.expo(1.0 / slow_extra)
                } else {
                    (fast_mu + fast_sigma * rng.gauss()).exp()
                }
            }
        };
        Duration::from_secs_f64(secs.max(0.0))
    }
}

// --------------------------------------------------------------- the server

/// One backend: a fixed number of workers, a queue, and a service time.
///
/// `workers` is what makes phase C possible. Set it high and the backend is a
/// pure delay generator, which is what phase A wants; set it to 4 and the thing
/// has a capacity, a queue in front of it, and therefore an opinion about how
/// fast you are allowed to send.
struct Backend {
    sem: Arc<Semaphore>,
    started: AtomicU64,
    completed: AtomicU64,
    busy_us: AtomicU64,
}

impl Backend {
    fn new(workers: usize) -> Self {
        Backend {
            sem: Arc::new(Semaphore::new(workers)),
            started: AtomicU64::new(0),
            completed: AtomicU64::new(0),
            busy_us: AtomicU64::new(0),
        }
    }

    /// One call. There is no cancellation token and no `select!` in here: if
    /// the caller drops this future, polling stops, the permit's `Drop` returns
    /// the worker, and `BusyGuard::drop` books the service time actually
    /// consumed. Cancellation is structural.
    async fn call(&self, dist: Dist, rng: &Rng) {
        self.started.fetch_add(1, Ordering::Relaxed);
        let _permit = self.sem.clone().acquire_owned().await.unwrap(); // queueing, if any
        let held = dist.sample(rng);
        let _guard = BusyGuard {
            start: Instant::now(),
            sink: &self.busy_us,
        };
        sleep(held).await;
        self.completed.fetch_add(1, Ordering::Relaxed);
    }
}

/// Books service time on drop, so a call that is cancelled halfway is billed
/// for the half it used and not for the half it did not. Doing this in `Drop`
/// rather than after the `.await` is the only way to get it right in a runtime
/// where the code after an `.await` may simply never run.
struct BusyGuard<'a> {
    start: Instant,
    sink: &'a AtomicU64,
}

impl<'a> Drop for BusyGuard<'a> {
    fn drop(&mut self) {
        self.sink
            .fetch_add(self.start.elapsed().as_micros() as u64, Ordering::Relaxed);
    }
}

/// gRPC/Envoy-shaped retry throttle: every primary call earns `ratio` of a
/// token, every hedge spends a whole one. Steady state is therefore "hedges are
/// at most `ratio` of primary calls", with `capacity` worth of burst. This is
/// the difference between a hedge and a retry storm with better branding.
struct TokenBucket {
    ratio: f64,
    capacity: f64,
    tokens: Mutex<f64>,
}

impl TokenBucket {
    fn new(ratio: f64, capacity: f64) -> Self {
        TokenBucket {
            ratio,
            capacity,
            tokens: Mutex::new(capacity),
        }
    }
    fn on_primary(&self) {
        let mut t = self.tokens.lock().unwrap();
        *t = (*t + self.ratio).min(self.capacity);
    }
    fn take(&self) -> bool {
        let mut t = self.tokens.lock().unwrap();
        if *t >= 1.0 {
            *t -= 1.0;
            true
        } else {
            false
        }
    }
}

#[derive(Clone, Copy, PartialEq)]
enum HedgeMode {
    Off,
    /// `select!` drops the loser: the permit comes back, the sleep stops.
    DropLoser,
    /// The loser is moved into a `tokio::spawn` and never cancelled, so it runs
    /// to completion holding its worker. Same policy, same budget, and this one
    /// is a retry storm.
    LeakLoser,
}

/// Fans out to K backends and waits for every one of them.
struct Gateway {
    backends: Vec<Arc<Backend>>,
    dist: Dist,
    rng: Arc<Rng>,
    hedge_delay: Option<Duration>,
    mode: HedgeMode,
    bucket: TokenBucket,
    legs_hedged: AtomicU64,
    budget_denied: AtomicU64,
}

impl Gateway {
    fn new(
        backends: Vec<Arc<Backend>>,
        dist: Dist,
        rng: Arc<Rng>,
        hedge_delay: Option<Duration>,
        mode: HedgeMode,
    ) -> Arc<Self> {
        Arc::new(Gateway {
            backends,
            dist,
            rng,
            hedge_delay,
            mode,
            bucket: TokenBucket::new(HEDGE_BUDGET_RATIO, HEDGE_BUCKET_CAPACITY),
            legs_hedged: AtomicU64::new(0),
            budget_denied: AtomicU64::new(0),
        })
    }

    /// One leg. Returns true if this leg fired a hedge.
    async fn leg(self: &Arc<Self>, idx: usize) -> bool {
        let backend = self.backends[idx].clone();
        let delay = match self.hedge_delay {
            None => {
                backend.call(self.dist, &self.rng).await;
                return false;
            }
            Some(d) => d,
        };
        self.bucket.on_primary();

        if self.mode == HedgeMode::LeakLoser {
            return self.leg_leaky(backend, delay).await;
        }

        let first = backend.call(self.dist, &self.rng);
        tokio::pin!(first);
        tokio::select! {
            _ = &mut first => return false,
            _ = sleep(delay) => {}
        }

        // Past the measured p95 and still nothing. Hedge -- if the budget says so.
        if !self.bucket.take() {
            self.budget_denied.fetch_add(1, Ordering::Relaxed);
            first.await;
            return false;
        }
        self.legs_hedged.fetch_add(1, Ordering::Relaxed);

        let second = backend.call(self.dist, &self.rng);
        tokio::pin!(second);
        tokio::select! {
            _ = &mut first => {}
            _ = &mut second => {}
        }
        // Both futures are dropped here, at the closing brace. The loser's
        // permit is released by `Drop`; there is no cancel call to forget, and
        // no way to forget it.
        true
    }

    /// The same policy written the way you write it when you reach for
    /// `tokio::spawn` to make the borrow checker stop complaining.
    ///
    /// Two things change and only one of them is visible in the code.
    /// `tokio::spawn` detaches: the task now owns its data and runs whether or
    /// not anybody is holding the handle. And **dropping a `JoinHandle` does
    /// not cancel the task** -- only `handle.abort()` does. So the `}` that
    /// cancelled the loser above cancels nothing here, and the losing copy runs
    /// to completion holding its worker. Same policy, same budget, same number
    /// of calls issued; read `svc_ms/req` for the difference.
    async fn leg_leaky(self: &Arc<Self>, backend: Arc<Backend>, delay: Duration) -> bool {
        let dist = self.dist;
        let rng = self.rng.clone();
        let b1 = backend.clone();
        let mut first = tokio::spawn(async move { b1.call(dist, &rng).await });

        tokio::select! {
            _ = &mut first => return false,
            _ = sleep(delay) => {}
        }
        if !self.bucket.take() {
            self.budget_denied.fetch_add(1, Ordering::Relaxed);
            let _ = first.await;
            return false;
        }
        self.legs_hedged.fetch_add(1, Ordering::Relaxed);

        let rng2 = self.rng.clone();
        let b2 = backend.clone();
        let mut second = tokio::spawn(async move { b2.call(dist, &rng2).await });
        tokio::select! {
            _ = &mut first => {}
            _ = &mut second => {}
        }
        // Both handles are dropped here and neither task is aborted.
        true
    }

    /// The fan-out: K legs, wait for all of them, so end-to-end latency IS the
    /// max of the legs. That is not an implementation detail here; it is the
    /// experiment.
    async fn handle(self: &Arc<Self>, k: usize) -> bool {
        let mut set = Vec::with_capacity(k);
        for i in 0..k {
            set.push(self.leg(i));
        }
        futures_join_all(set).await.into_iter().any(|h| h)
    }
}

/// `join_all` without the `futures` crate: poll them concurrently on one task
/// by handing them all to `tokio::join!`-style polling. A Vec of futures is
/// driven by pinning it and polling each in turn, which is what `FuturesUnordered`
/// does with a readiness queue attached; at K <= 50 the difference does not
/// matter and the dependency does.
async fn futures_join_all<F: std::future::Future<Output = bool>>(futs: Vec<F>) -> Vec<bool> {
    use std::future::Future;
    use std::pin::Pin;
    use std::task::{Context, Poll};

    struct JoinAll<F> {
        futs: Vec<Option<Pin<Box<F>>>>,
        out: Vec<bool>,
        left: usize,
    }
    impl<F: Future<Output = bool>> Future for JoinAll<F> {
        type Output = Vec<bool>;
        fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Vec<bool>> {
            let this = self.get_mut();
            for i in 0..this.futs.len() {
                if let Some(f) = this.futs[i].as_mut() {
                    if let Poll::Ready(v) = f.as_mut().poll(cx) {
                        this.out[i] = v;
                        this.futs[i] = None;
                        this.left -= 1;
                    }
                }
            }
            if this.left == 0 {
                Poll::Ready(std::mem::take(&mut this.out))
            } else {
                Poll::Pending
            }
        }
    }
    let n = futs.len();
    JoinAll {
        futs: futs.into_iter().map(|f| Some(Box::pin(f))).collect(),
        out: vec![false; n],
        left: n,
    }
    .await
}

// ------------------------------------------------------------- the harness

#[derive(Default)]
struct Cell {
    lat_ms: Vec<f64>,
    late_ms: Vec<f64>,
    corrected_ms: Vec<f64>,
    hedged_requests: u64,
}

struct Summary {
    n: usize,
    p50: f64,
    p99: f64,
    max: f64,
    tail: f64,
    late_p99: f64,
    backend_rps: f64,
    svc_ms_per_req: f64,
    hedge_rate: f64,
}

fn summarise(cell: &Cell, wall_s: f64, backend_started: u64, backend_busy_ms: f64) -> Summary {
    let mut lat = cell.lat_ms.clone();
    lat.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let mut late = cell.late_ms.clone();
    late.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let over = lat.iter().filter(|v| **v > TAIL_THRESHOLD_MS).count();
    let den = lat.len().max(1) as f64;
    Summary {
        n: lat.len(),
        p50: pct(&lat, 0.50),
        p99: pct(&lat, 0.99),
        max: *lat.last().unwrap_or(&f64::NAN),
        tail: 100.0 * over as f64 / den,
        late_p99: pct(&late, 0.99),
        backend_rps: backend_started as f64 / wall_s.max(1e-9),
        svc_ms_per_req: backend_busy_ms / den,
        hedge_rate: 100.0 * cell.hedged_requests as f64 / den,
    }
}

struct CellResult {
    cell: Cell,
    summary: Summary,
    wall_s: f64,
    budget_denied: u64,
}

/// Open model: arrivals happen on a precomputed schedule, full stop.
///
/// The schedule is absolute and computed before the run, so the generator's own
/// overhead cannot leak into it -- a generator that sleeps for expovariate(rate)
/// BETWEEN dispatches slows down exactly when the server does, and has quietly
/// become the closed-loop generator this topic is about. Latency is measured
/// from each request's DUE time, not from when the dispatch loop got round to
/// it, for the same reason.
async fn run_open_cell(
    k: usize,
    dist: Dist,
    workers: usize,
    rate: f64,
    n: usize,
    hedge_delay: Option<Duration>,
    mode: HedgeMode,
) -> CellResult {
    let rng_arr = Rng::new(SEED);
    let backends: Vec<Arc<Backend>> = (0..k).map(|_| Arc::new(Backend::new(workers))).collect();
    let gw = Gateway::new(
        backends,
        dist,
        Arc::new(Rng::new(SEED + 1)),
        hedge_delay,
        mode,
    );
    let cell = Arc::new(Mutex::new(Cell::default()));

    let mut schedule = Vec::with_capacity(n);
    let mut acc = 0.0f64;
    for _ in 0..n {
        acc += rng_arr.expo(rate);
        schedule.push(Duration::from_secs_f64(acc));
    }

    let t0 = Instant::now();
    let mut tasks = Vec::with_capacity(n);
    for off in schedule {
        let due = t0 + off;
        sleep_until(due).await;
        cell.lock().unwrap().late_ms.push(ms_since(due));
        let gw2 = gw.clone();
        let cell2 = cell.clone();
        tasks.push(tokio::spawn(async move {
            let hedged = gw2.handle(k).await;
            let lat = ms_since(due);
            let mut c = cell2.lock().unwrap();
            c.lat_ms.push(lat);
            if hedged {
                c.hedged_requests += 1;
            }
        }));
    }
    let wall_s = t0.elapsed().as_secs_f64();

    // Everything in flight at the end is counted. Dropping it would be its own
    // flavour of omission, and the requests still running are the slow ones.
    for t in tasks {
        let _ = t.await;
    }
    if mode == HedgeMode::LeakLoser {
        // Leaked losers are still holding workers with nobody awaiting them.
        // Drain before summarising, or svc_ms/req reports a half-finished bill.
        for _ in 0..400 {
            let idle = gw.backends.iter().all(|b| {
                b.sem.available_permits() >= workers
            });
            if idle {
                break;
            }
            sleep(Duration::from_millis(10)).await;
        }
    }

    let started: u64 = gw.backends.iter().map(|b| b.started.load(Ordering::Relaxed)).sum();
    let busy_ms: f64 = gw
        .backends
        .iter()
        .map(|b| b.busy_us.load(Ordering::Relaxed) as f64 / 1000.0)
        .sum();
    let cell = std::mem::take(&mut *cell.lock().unwrap());
    let summary = summarise(&cell, wall_s, started, busy_ms);
    CellResult {
        cell,
        summary,
        wall_s,
        budget_denied: gw.budget_denied.load(Ordering::Relaxed),
    }
}

/// Closed model: `vus` virtual users, each waiting before sending again. This is
/// `ramping-vus`, the executor the rest of this layer forbids. It is permitted
/// here and only here, because seeing it lie is the point.
///
/// Two numbers are recorded per request. The raw one is what a closed-loop
/// generator reports: finish minus send. The corrected one is finish minus the
/// time the request was DUE under the nominal schedule -- because a VU stuck
/// waiting on a slow response is not sending the requests it owed, and those
/// unsent requests are exactly the ones that would have been slow.
async fn run_closed_cell(
    k: usize,
    dist: Dist,
    workers: usize,
    vus: usize,
    nominal_rate: f64,
    seconds: f64,
) -> CellResult {
    let backends: Vec<Arc<Backend>> = (0..k).map(|_| Arc::new(Backend::new(workers))).collect();
    let gw = Gateway::new(
        backends,
        dist,
        Arc::new(Rng::new(SEED + 1)),
        None,
        HedgeMode::Off,
    );
    let cell = Arc::new(Mutex::new(Cell::default()));
    let per_vu_interval = vus as f64 / nominal_rate;

    let t0 = Instant::now();
    let deadline = t0 + Duration::from_secs_f64(seconds);
    let mut tasks = Vec::with_capacity(vus);
    for v in 0..vus {
        let gw2 = gw.clone();
        let cell2 = cell.clone();
        tasks.push(tokio::spawn(async move {
            let mut j = 0u64;
            loop {
                let start = Instant::now();
                if start >= deadline {
                    return;
                }
                let due = t0
                    + Duration::from_secs_f64(
                        v as f64 / nominal_rate + j as f64 * per_vu_interval,
                    );
                gw2.handle(k).await;
                let fin = Instant::now();
                let raw = (fin - start).as_secs_f64() * 1000.0;
                let from = if due < start { due } else { start };
                let corrected = (fin - from).as_secs_f64() * 1000.0;
                let mut c = cell2.lock().unwrap();
                c.lat_ms.push(raw);
                c.corrected_ms.push(corrected);
                j += 1;
            }
        }));
    }
    for t in tasks {
        let _ = t.await;
    }
    let wall_s = t0.elapsed().as_secs_f64();
    let started: u64 = gw.backends.iter().map(|b| b.started.load(Ordering::Relaxed)).sum();
    let busy_ms: f64 = gw
        .backends
        .iter()
        .map(|b| b.busy_us.load(Ordering::Relaxed) as f64 / 1000.0)
        .sum();
    let cell = std::mem::take(&mut *cell.lock().unwrap());
    let summary = summarise(&cell, wall_s, started, busy_ms);
    CellResult {
        cell,
        summary,
        wall_s,
        budget_denied: 0,
    }
}

fn ms_since(t: Instant) -> f64 {
    Instant::now().saturating_duration_since(t).as_secs_f64() * 1000.0
}

/// Measure ONE backend directly. Everything downstream is relative to this,
/// including the hedge delay used in phase B.
async fn calibrate(dist: Dist, workers: usize, n: usize) -> (f64, f64, f64, f64, f64) {
    let rng = Arc::new(Rng::new(SEED + 7));
    let b = Arc::new(Backend::new(workers));
    let lat = Arc::new(Mutex::new(Vec::with_capacity(n)));
    let mut done = 0;
    while done < n {
        let batch = CALIB_BATCH.min(n - done);
        let mut tasks = Vec::with_capacity(batch);
        for _ in 0..batch {
            let b2 = b.clone();
            let rng2 = rng.clone();
            let lat2 = lat.clone();
            tasks.push(tokio::spawn(async move {
                let t0 = Instant::now();
                b2.call(dist, &rng2).await;
                lat2.lock().unwrap().push(ms_since(t0));
            }));
        }
        for t in tasks {
            let _ = t.await;
        }
        done += batch;
    }
    let mut v = lat.lock().unwrap().clone();
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let over = v.iter().filter(|x| **x > TAIL_THRESHOLD_MS).count();
    let mean = v.iter().sum::<f64>() / v.len() as f64;
    (
        pct(&v, 0.50),
        pct(&v, 0.95),
        pct(&v, 0.99),
        mean,
        100.0 * over as f64 / v.len() as f64,
    )
}

// ----------------------------------------------------------------- output

const HIST_EDGES_MS: [f64; 12] = [
    0.0, 5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 320.0, 640.0, 1280.0, 2560.0, 5120.0,
];

fn histogram(label: &str, vals: &[f64]) {
    if vals.is_empty() {
        return;
    }
    let mut counts = [0usize; HIST_EDGES_MS.len()];
    for v in vals {
        let mut placed = false;
        for i in 0..HIST_EDGES_MS.len() - 1 {
            if *v >= HIST_EDGES_MS[i] && *v < HIST_EDGES_MS[i + 1] {
                counts[i] += 1;
                placed = true;
                break;
            }
        }
        if !placed {
            counts[HIST_EDGES_MS.len() - 1] += 1;
        }
    }
    let peak = *counts.iter().max().unwrap_or(&1).max(&1);
    println!("  {}   (n={})", label, vals.len());
    for i in 0..HIST_EDGES_MS.len() {
        let range = if i + 1 < HIST_EDGES_MS.len() {
            format!("{:>6.0} - {:>6.0} ms", HIST_EDGES_MS[i], HIST_EDGES_MS[i + 1])
        } else {
            format!("{:>6.0} +{:8}ms", HIST_EDGES_MS[i], "")
        };
        let bar = "#".repeat(((40.0 * counts[i] as f64 / peak as f64).round()) as usize);
        println!("    {} |{:<40}| {:>6}", range, bar, counts[i]);
    }
}

fn rule(title: &str) {
    println!();
    println!("{}", "=".repeat(78));
    println!("{}", title);
    println!("{}", "=".repeat(78));
}

fn cell_rate(k: usize) -> f64 {
    MAX_RATE.min(MAX_BACKEND_CALLS_PER_S / k as f64)
}

// ------------------------------------------------------------------- main

#[tokio::main(flavor = "multi_thread")]
async fn main() {
    let sigma = lognormal_sigma();
    let lognormal = Dist::LogNormal {
        mu: (BACKEND_P50_MS / 1000.0).ln(),
        sigma,
    };
    let bimodal = Dist::Bimodal {
        fast_mu: (BACKEND_P50_MS / 1000.0).ln(),
        fast_sigma: 0.15, // tight: the fast mode never reaches the floor
        slow_floor: TAIL_THRESHOLD_MS / 1000.0,
        slow_extra: 0.050,
        p_slow: 0.01,
    };
    let dists = [lognormal, bimodal];

    rule("Layer 5 - Topic 6: fan-out, hedging and coordinated omission (Rust)");
    println!("  backend p50 configured   {:.1} ms", BACKEND_P50_MS);
    println!(
        "  backend p99 configured   {:.1} ms   (p99/p50 = {:.0}x, log-normal sigma = {:.4})",
        TAIL_THRESHOLD_MS, TAIL_RATIO, sigma
    );
    println!(
        "  tail threshold t         {:.1} ms   chosen so P(one leg > t) = 1% for BOTH distributions, by construction",
        TAIL_THRESHOLD_MS
    );
    println!("  predicted below          1 - 0.99^K, arithmetic rather than measurement");

    // ------------------------------------------------------------ calibration
    rule("CALIBRATION: one backend, unsaturated, measured directly");
    println!(
        "  {:<12}{:>9}{:>9}{:>9}{:>9}{:>13}",
        "distribution", "p50", "p95", "p99", "mean", "P(leg > t)"
    );
    let mut calib = Vec::new();
    for d in dists.iter() {
        let c = calibrate(*d, STAT_WORKERS, CALIB_SAMPLES).await;
        println!(
            "  {:<12}{:>7.1}ms{:>7.1}ms{:>7.1}ms{:>7.1}ms{:>12.2}%",
            d.name(),
            c.0,
            c.1,
            c.2,
            c.3,
            c.4
        );
        calib.push(c);
    }
    println!();
    println!("  P(leg > t) is the measured check on the configured 1%. The hedge delay");
    println!("  in phase B is the MEASURED p95 above, not the analytic one.");

    // ---------------------------------------------------------------- phase A
    rule("PHASE A: fan-out to K backends, wait for all, no hedging");
    println!(
        "  backends have {} workers each -- they do not queue, so the only",
        STAT_WORKERS
    );
    println!("  mechanism acting on these numbers is the arithmetic of maxima.");
    println!();
    println!(
        "  {:<11}{:>4}{:>7}{:>7}{:>10}{:>10}{:>10}{:>11}{:>10}{:>14}",
        "dist", "K", "rate", "n", "e2e_p50", "e2e_p99", "e2e_max", "predicted", "measured",
        "gen_late_p99"
    );
    let mut baseline: Vec<(String, usize, f64, f64, f64, f64)> = Vec::new();
    for d in dists.iter() {
        for k in K_VALUES {
            let rate = cell_rate(k);
            let r = run_open_cell(k, *d, STAT_WORKERS, rate, SAMPLES_PER_CELL, None, HedgeMode::Off)
                .await;
            let s = &r.summary;
            let predicted = 100.0 * (1.0 - 0.99f64.powi(k as i32));
            println!(
                "  {:<11}{:>4}{:>7.0}{:>7}{:>8.1}ms{:>8.1}ms{:>8.1}ms{:>10.1}%{:>9.1}%{:>12.2}ms",
                d.name(),
                k,
                rate,
                s.n,
                s.p50,
                s.p99,
                s.max,
                predicted,
                s.tail,
                s.late_p99
            );
            baseline.push((
                d.name().to_string(),
                k,
                s.p50,
                s.p99,
                s.backend_rps,
                s.svc_ms_per_req,
            ));
        }
        println!();
    }

    // ---------------------------------------------------------------- phase B
    rule("PHASE B: hedging at the measured backend p95, under a 5% token bucket");
    println!("  Three rows per configuration, identical except for what happens to the");
    println!("  losing copy: nothing (no hedge), dropped by select!, or spawned and left");
    println!("  to run -- the shape of the bug you get when `spawn` replaces ownership.");
    println!();
    println!("  svc_ms/req is the backend service time actually consumed per request. It is");
    println!("  the column that separates the last two rows: they issue the same calls, and");
    println!("  only one of them stops paying for the copy it threw away.");
    println!();
    println!(
        "  {:<10}{:>3} {:<26}{:>9}{:>9}{:>11}{:>7}{:>11}{:>8}{:>7}",
        "dist", "K", "mode", "e2e_p50", "e2e_p99", "be_rps", "+load", "svc_ms/req", "hedge%",
        "denied"
    );
    for (di, d) in dists.iter().enumerate() {
        let hedge_delay_ms = calib[di].1;
        for k in HEDGE_K {
            let rate = cell_rate(k);
            let b = baseline
                .iter()
                .find(|x| x.0 == d.name() && x.1 == k)
                .unwrap()
                .clone();
            println!(
                "  {:<10}{:>3} {:<26}{:>7.1}ms{:>7.1}ms{:>10.0}/s{:>7}{:>11.1}{:>8}{:>7}",
                d.name(),
                k,
                "no hedge",
                b.2,
                b.3,
                b.4,
                "-",
                b.5,
                "-",
                "-"
            );
            for mode in [HedgeMode::DropLoser, HedgeMode::LeakLoser] {
                let label = if mode == HedgeMode::DropLoser {
                    "hedge @p95, loser dropped"
                } else {
                    "hedge @p95, loser LEAKED"
                };
                let r = run_open_cell(
                    k,
                    *d,
                    STAT_WORKERS,
                    rate,
                    SAMPLES_PER_CELL,
                    Some(Duration::from_secs_f64(hedge_delay_ms / 1000.0)),
                    mode,
                )
                .await;
                let s = &r.summary;
                let load_pct = 100.0 * (s.backend_rps / b.4 - 1.0);
                println!(
                    "  {:<10}{:>3} {:<26}{:>7.1}ms{:>7.1}ms{:>10.0}/s{:>6.1}%{:>11.1}{:>7.1}%{:>7}",
                    "", "", label, s.p50, s.p99, s.backend_rps, load_pct, s.svc_ms_per_req,
                    s.hedge_rate, r.budget_denied
                );
            }
            println!(
                "  {:<10}{:>3}  hedge delay = measured p95 = {:.1} ms",
                "", "", hedge_delay_ms
            );
            println!();
        }
    }

    // ---------------------------------------------------------------- phase C
    rule("PHASE C: the same server measured twice -- open model vs closed loop");
    let mean_service_s = calib[0].3 / 1000.0;
    let capacity = CO_WORKERS as f64 / mean_service_s;
    let rate = CO_RHO * capacity;
    println!(
        "  K = {}, log-normal, and this time each backend has only {} workers.",
        CO_K, CO_WORKERS
    );
    println!("  measured mean service time  {:.1} ms", mean_service_s * 1000.0);
    println!(
        "  => capacity per backend     {:.1} rps ({} workers / mean service)",
        capacity, CO_WORKERS
    );
    println!(
        "  => nominal offered rate     {:.1} rps  (rho = {:.2})",
        rate, CO_RHO
    );
    println!();
    println!("  rho is deliberately below 1. Above capacity the open model's queue grows");
    println!("  without bound and its p99 becomes a statement about how long you ran,");
    println!("  not about the server. Below capacity both numbers mean something.");

    // A short unsaturated pass to size the VU pool by Little's Law.
    let warm = run_open_cell(CO_K, lognormal, STAT_WORKERS, rate, 600, None, HedgeMode::Off).await;
    let base_mean_e2e_s =
        (warm.cell.lat_ms.iter().sum::<f64>() / warm.cell.lat_ms.len() as f64) / 1000.0;
    let vus = ((rate * base_mean_e2e_s).round() as usize).max(1);
    println!();
    println!(
        "  unsaturated e2e mean at K={}: {:.1} ms (p99 {:.1} ms)",
        CO_K,
        base_mean_e2e_s * 1000.0,
        warm.summary.p99
    );
    println!(
        "  => closed loop gets {} VUs, from Little's Law: {:.1} rps x {:.1} ms.",
        vus,
        rate,
        base_mean_e2e_s * 1000.0
    );
    println!("     At the healthy latency those VUs issue the nominal rate exactly. That");
    println!("     is the whole trick: the generator is calibrated on a good day.");

    let open = run_open_cell(
        CO_K,
        lognormal,
        CO_WORKERS,
        rate,
        (rate * CO_SECONDS) as usize,
        None,
        HedgeMode::Off,
    )
    .await;
    let closed = run_closed_cell(CO_K, lognormal, CO_WORKERS, vus, rate, CO_SECONDS).await;
    let mut corrected = closed.cell.corrected_ms.clone();
    corrected.sort_by(|a, b| a.partial_cmp(b).unwrap());

    println!();
    println!(
        "  {:<34}{:>7}{:>11}{:>10}{:>10}{:>11}",
        "model", "n", "achieved", "p50", "p99", "max"
    );
    println!(
        "  {:<34}{:>7}{:>9.0}/s{:>8.1}ms{:>8.1}ms{:>9.1}ms",
        "open  (arrival schedule)",
        open.summary.n,
        open.summary.n as f64 / open.wall_s,
        open.summary.p50,
        open.summary.p99,
        open.summary.max
    );
    println!(
        "  {:<34}{:>7}{:>9.0}/s{:>8.1}ms{:>8.1}ms{:>9.1}ms",
        format!("closed ({} VUs), as reported", vus),
        closed.summary.n,
        closed.summary.n as f64 / closed.wall_s,
        closed.summary.p50,
        closed.summary.p99,
        closed.summary.max
    );
    println!(
        "  {:<34}{:>7}{:>11}{:>8.1}ms{:>8.1}ms{:>9.1}ms",
        "closed, omission-corrected",
        corrected.len(),
        "",
        pct(&corrected, 0.50),
        pct(&corrected, 0.99),
        corrected.last().copied().unwrap_or(f64::NAN)
    );
    println!();
    println!(
        "  open-model generator lateness p99: {:.2} ms",
        open.summary.late_p99
    );
    println!("  (if that number is large the generator itself fell behind and is now");
    println!("   coordinating omission too, arrival schedule or not -- k6's warning");
    println!("   about not being able to allocate enough VUs is the same tell.)");
    println!();
    histogram("open model  ", &open.cell.lat_ms);
    println!();
    histogram("closed loop ", &closed.cell.lat_ms);
    println!();
    println!("  Same server. Same nominal rate. Read the two histograms' right-hand");
    println!("  ends against each other, then read the closed loop's raw p99 against");
    println!("  its corrected p99.");
    println!();
}
