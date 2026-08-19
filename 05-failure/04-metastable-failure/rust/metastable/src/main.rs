// Layer 5 - Topic 4: metastable failure, in one Rust process.
//
// THE FLAGSHIP. The claim is not "overload is bad" -- everyone knows that.
// The claim is that the thing which TRIGGERS an outage and the thing which
// SUSTAINS it are different mechanisms, so removing the trigger does not end
// the outage. This file removes the trigger, keeps offered load exactly where
// it was, waits, and shows you nothing improving.
//
// Rust is the cleanest demonstration in this folder that metastability is an
// ARCHITECTURAL property and not a memory-management one. Everything Rust is
// usually credited with is present here and none of it helps:
//
//   - No GC, so the classic GC death spiral -- Java's version of this topic
//     -- is structurally impossible. It changes nothing below.
//   - Real backpressure primitives. `tokio::sync::Semaphore` is the pool, and
//     a permit is an RAII guard: when a caller's future is dropped, the
//     permit goes back on its own. `timeout(..).await` dropping the inner
//     future IS cancellation, and it is the one place in these six languages
//     where "stop doing the work" needs no cooperation from the work.
//   - `mpsc::Sender::try_send` returning `Err(TrySendError::Full)` makes
//     backpressure a type you cannot ignore.
//
// And a memory-safe, GC-free, correctly backpressured service still collapses
// when its own clients retry into it, because the feedback loop lives BETWEEN
// the components rather than inside any one of them. The single thing this
// file has to do to get into trouble is `tokio::spawn` per arrival with
// nothing bounding the spawns -- the same missing bound as everywhere else.
//
// Worth knowing the other Rust-shaped trap even though this file does not use
// it: `spawn_blocking`'s pool is bounded, but at 512 threads, which is a
// queue deep enough to hide a very long backlog behind a number that looks
// like a limit.
//
// WHAT THIS DEMONSTRATES
//   A cache in front of a database, at a 90% hit rate, comfortably stable.
//   The trigger is one instantaneous, fully reversible command: FLUSHALL.
//   The cache is BACK the moment it starts refilling -- except that it never
//   starts, because refilling requires a query to finish before its caller
//   gives up, and no query does any more.
//
//   HotOS '25 vocabulary, which this file is built to make concrete:
//     trigger                 the cache flush, over in one millisecond
//     amplification mechanism naive retries (topic 3) plus the miss rate
//                             going from 10% to 100%
//     sustaining effect       a cache that cannot refill, because fills only
//                             happen on completions that beat the deadline
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. `goodput` versus `thruput`. Throughput stays high while goodput goes
//      to zero: the process is busy, the pool is full, requests are flowing,
//      and almost none of them produce a response anybody receives.
//   2. `hit%` stuck at zero AFTER the trigger is long gone. That is the
//      sustaining effect, and it is why scenario 0 never recovers.
//   3. `tasks` -- live spawned tasks. Rust's version of Python's climbing
//      in-flight count. Permits come back correctly, futures are dropped
//      correctly, nothing leaks, and the number still climbs, because a
//      correct queue is still a queue.
//   4. Which escapes are SUFFICIENT rather than merely helpful. The verdict
//      lines at the end are computed from THIS run, not asserted here.
//
// RUN
//   cargo run --release
//
// Roughly four minutes: five scenarios, the four with an escape running
// longer because "did it recover" is a question about minutes, not seconds.

use std::collections::HashSet;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::sync::Semaphore;
use tokio::task::JoinHandle;
use tokio::time::{sleep, sleep_until, timeout, Instant};

// ---------------------------------------------------------------- config
//
// Identical to python/metastable.py's constants, deliberately: the point of
// six languages here is that the same system-level dynamic appears in all of
// them, so the constants are not allowed to drift.

const OFFERED_RPS: f64 = 180.0; // constant. It never changes. That is the point.
const KEYS: u32 = 400; // the cache keyspace
const EVICT_PER_SEC: usize = 18; // TTL churn -> equilibrium hit rate 90%

const DB_SERVICE: Duration = Duration::from_millis(200); // an uncached read
const CACHE_SERVICE: Duration = Duration::from_millis(1); // a cached one
const POOL_SIZE: usize = 6; // 6 / 0.200 = 30 misses per second of capacity

const CLIENT_TIMEOUT: Duration = Duration::from_millis(500); // longer than
const ATTEMPTS: usize = 3; // normal service time, shorter than degraded.

const TRIGGER_AT: f64 = 6.0; // redis-cli FLUSHALL
const ESCAPE_AT: f64 = 16.0; // ten seconds of watching nothing improve first
const END_AT: f64 = 30.0; // long enough to prove scenario 0 does not recover
const ESCAPE_END_AT: f64 = 50.0;
const REPORT_EVERY: f64 = 2.0;

const SHED_LIMIT: i64 = 8; // escape (c). Topic 5, borrowed early.
const BUDGET_RATIO: f64 = 0.10; // escape (b). Topic 3's token bucket.
const RAMP_BACK_SECONDS: f64 = 8.0; // escape (a) lets load back SLOWLY.
const DROP_SECONDS: f64 = 5.0;

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
    fn below(&self, n: u32) -> u32 {
        (self.next_f64() * n as f64) as u32 % n
    }
}

// --------------------------------------------------------------- the cache

/// Redis, modelled as the only thing about Redis that matters here: a set of
/// keys that are present, and the fact that emptying it is instant and
/// refilling it is not.
struct Cache {
    inner: Mutex<CacheInner>,
}

struct CacheInner {
    present: HashSet<u32>,
    hits: i64,
    misses: i64,
}

impl Cache {
    fn new() -> Self {
        Cache {
            inner: Mutex::new(CacheInner {
                present: (0..KEYS).collect(),
                hits: 0,
                misses: 0,
            }),
        }
    }

    fn get(&self, key: u32) -> bool {
        let mut g = self.inner.lock().unwrap();
        if g.present.contains(&key) {
            g.hits += 1;
            true
        } else {
            g.misses += 1;
            false
        }
    }

    fn put(&self, key: u32) {
        self.inner.lock().unwrap().present.insert(key);
    }

    /// One command. Instantaneous. Fully reversible. This is the entire
    /// trigger, and ten seconds later it will be completely irrelevant to why
    /// the system is down.
    fn flushall(&self) {
        self.inner.lock().unwrap().present.clear();
    }

    /// Ordinary TTL churn, which is what holds the hit rate at 90% instead of
    /// letting it climb to 100% and make the experiment lie.
    fn evict(&self, n: usize) {
        let mut g = self.inner.lock().unwrap();
        for _ in 0..n {
            let victim = match g.present.iter().next() {
                Some(k) => *k,
                None => return,
            };
            g.present.remove(&victim);
        }
    }

    fn take_rates(&self) -> (i64, i64) {
        let mut g = self.inner.lock().unwrap();
        let out = (g.hits, g.misses);
        g.hits = 0;
        g.misses = 0;
        out
    }
}

// ------------------------------------------------------------ the database

/// A real bounded pool. 6 connections at 200ms is 30 queries a second, and
/// nothing anybody does to the application changes that number.
///
/// The permit is an RAII guard, which is the entire Rust story on this topic:
/// when the client's `timeout` drops this future mid-`await`, the permit
/// returns to the semaphore without anybody remembering to release it, and a
/// waiter that has been dropped is simply no longer in the queue.
struct Database {
    sem: Semaphore,
    in_use: AtomicI64,
}

impl Database {
    fn new() -> Self {
        Database {
            sem: Semaphore::new(POOL_SIZE),
            in_use: AtomicI64::new(0),
        }
    }

    async fn query(&self) {
        let _permit = self.sem.acquire().await.unwrap();
        self.in_use.fetch_add(1, Ordering::Relaxed);
        // A guard so the gauge is correct even when this future is dropped
        // half way through the sleep, which under overload is the common
        // case rather than the exotic one.
        struct Gauge<'a>(&'a AtomicI64);
        impl Drop for Gauge<'_> {
            fn drop(&mut self) {
                self.0.fetch_sub(1, Ordering::Relaxed);
            }
        }
        let _gauge = Gauge(&self.in_use);
        sleep(DB_SERVICE).await;
    }
}

// ------------------------------------------------------------ retry budget

/// Topic 3's token bucket, used here only as escape (b). Milli-tokens, so the
/// 0.1-per-success refill fits in an atomic.
struct RetryBudget {
    tokens: AtomicI64,
}

impl RetryBudget {
    fn new() -> Self {
        RetryBudget {
            tokens: AtomicI64::new(3_000),
        }
    }
    fn deposit(&self) {
        let _ = self
            .tokens
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |t| {
                Some((t + (BUDGET_RATIO * 1000.0) as i64).min(103_000))
            });
    }
    fn withdraw(&self) -> bool {
        self.tokens
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |t| {
                if t >= 1000 {
                    Some(t - 1000)
                } else {
                    None
                }
            })
            .is_ok()
    }
}

// ------------------------------------------------------------- the server

struct Server {
    cache: Arc<Cache>,
    db: Arc<Database>,
    m: Arc<Metrics>,
    inflight: AtomicI64,
    budget: Mutex<Option<Arc<RetryBudget>>>, // escape (b)
    shed_limit: AtomicI64,                   // escape (c); 0 means none
}

impl Server {
    fn new(cache: Arc<Cache>, db: Arc<Database>, m: Arc<Metrics>) -> Self {
        Server {
            cache,
            db,
            m,
            inflight: AtomicI64::new(0),
            budget: Mutex::new(None),
            shed_limit: AtomicI64::new(0),
        }
    }

    fn budget(&self) -> Option<Arc<RetryBudget>> {
        self.budget.lock().unwrap().clone()
    }

    /// One attempt. Returns true if the caller got an answer in time.
    async fn handle(&self, key: u32, deadline: Instant) -> bool {
        // Escape (c), and topic 5 in one line: refuse work you have no
        // capacity for, immediately, instead of accepting it and being late.
        let lim = self.shed_limit.load(Ordering::Relaxed);
        if lim > 0 && self.inflight.load(Ordering::Relaxed) >= lim {
            self.m.shed.fetch_add(1, Ordering::Relaxed);
            return false;
        }
        self.inflight.fetch_add(1, Ordering::Relaxed);
        struct Gauge<'a>(&'a AtomicI64);
        impl Drop for Gauge<'_> {
            fn drop(&mut self) {
                self.0.fetch_sub(1, Ordering::Relaxed);
            }
        }
        let _gauge = Gauge(&self.inflight);

        if self.cache.get(key) {
            sleep(CACHE_SERVICE).await;
            return Instant::now() <= deadline;
        }
        self.db.query().await;
        let in_time = Instant::now() <= deadline;
        if in_time {
            // THE SUSTAINING EFFECT, in one `if`. The fill happens in the
            // handler, after the query returns -- and under overload the
            // handler has already been abandoned by then, so the fill never
            // happens. The cache cannot refill precisely because the database
            // is slow, and the database is slow precisely because the cache
            // is empty.
            self.cache.put(key);
        }
        in_time
    }
}

// -------------------------------------------------------------- the client

/// Topic 3's naive retry client: no jitter, no budget unless escape (b)
/// turned one on, and a per-attempt timeout that is comfortable when the
/// system is well and hopeless when it is not.
async fn client_request(server: Arc<Server>, m: Arc<Metrics>, key: u32) {
    for attempt in 0..ATTEMPTS {
        if attempt > 0 {
            if let Some(b) = server.budget() {
                if !b.withdraw() {
                    break;
                }
            }
            m.retries.fetch_add(1, Ordering::Relaxed);
        }
        let deadline = Instant::now() + CLIENT_TIMEOUT;
        // On expiry the inner future is DROPPED. Every guard it holds -- the
        // semaphore permit, the in-flight gauge -- unwinds on the way out,
        // and no code anywhere had to be told about the deadline.
        let ok = matches!(timeout(CLIENT_TIMEOUT, server.handle(key, deadline)).await, Ok(true));
        m.thruput_attempts.fetch_add(1, Ordering::Relaxed);
        if ok {
            // GOODPUT: a response delivered to a caller that was still waiting
            // for it. Not "requests handled". This is the only number in this
            // file worth alerting on.
            m.goodput.fetch_add(1, Ordering::Relaxed);
            if let Some(b) = server.budget() {
                b.deposit();
            }
            m.tasks.fetch_sub(1, Ordering::Relaxed);
            return;
        }
    }
    m.failed.fetch_add(1, Ordering::Relaxed);
    m.tasks.fetch_sub(1, Ordering::Relaxed);
}

// ------------------------------------------------------------- the harness

#[derive(Default)]
struct Metrics {
    goodput: AtomicI64,
    thruput_attempts: AtomicI64,
    retries: AtomicI64,
    failed: AtomicI64,
    shed: AtomicI64,
    tasks: AtomicI64,
}

struct Row {
    t: f64,
    offered: f64,
    thruput: f64,
    goodput: f64,
    hit: f64,
    pg: i64,
    inflight: i64,
    tasks: i64,
    retry: f64,
}

/// Offered load. Constant everywhere except escape (a), which is the only
/// intervention in this file that touches the client side at all.
fn offered_rate(t: f64, escape: &str) -> f64 {
    if escape != "a" || t < ESCAPE_AT {
        return OFFERED_RPS;
    }
    let since = t - ESCAPE_AT;
    if since < DROP_SECONDS {
        return 0.0; // take the load away
    }
    let ramp = (since - DROP_SECONDS) / RAMP_BACK_SECONDS; // ... and let it
    OFFERED_RPS * ramp.min(1.0) // back SLOWLY
}

async fn run_scenario(escape: &str) -> (Vec<Row>, f64) {
    let end_at = if escape.is_empty() { END_AT } else { ESCAPE_END_AT };
    let m = Arc::new(Metrics::default());
    let cache = Arc::new(Cache::new());
    let mut db = Arc::new(Database::new());
    let mut server = Arc::new(Server::new(cache.clone(), db.clone(), m.clone()));
    let rng = Rng::new(20250504);

    let begin = Instant::now();
    let mut last_report = begin;
    let mut last_evict = begin;
    let mut at = begin;
    let mut last = (0i64, 0i64, 0i64);
    let mut triggered = false;
    let mut escaped = false;
    let mut rows: Vec<Row> = Vec::new();
    let mut tasks: Vec<JoinHandle<()>> = Vec::new();

    loop {
        let t_planned = at.duration_since(begin).as_secs_f64();
        if t_planned > end_at {
            break;
        }
        let rate = offered_rate(t_planned, escape);
        at += if rate <= 0.0 {
            Duration::from_millis(50)
        } else {
            rng.exp(rate)
        };
        sleep_until(at).await;
        let now = Instant::now();
        let t = now.duration_since(begin).as_secs_f64();

        if !triggered && t >= TRIGGER_AT {
            cache.flushall();
            triggered = true;
        }
        if !escaped && t >= ESCAPE_AT {
            escaped = true;
            match escape {
                "b" => *server.budget.lock().unwrap() = Some(Arc::new(RetryBudget::new())),
                "c" => server.shed_limit.store(SHED_LIMIT, Ordering::Relaxed),
                "d" => {
                    // "Restart the app containers." Everything in the process
                    // goes: the tasks, the in-flight requests, the pool. The
                    // cache is external and stays exactly as cold as it was,
                    // and the clients never stopped retrying.
                    for h in tasks.drain(..) {
                        h.abort();
                    }
                    m.tasks.store(0, Ordering::Relaxed);
                    // Rebind rather than reset in place. A restart replaces
                    // the process: the new one starts with an empty pool and
                    // a zero gauge, while the dying requests unwind against
                    // the old objects. Zeroing the counters underneath them
                    // would drive the gauges NEGATIVE, which is a bug in the
                    // instrument rather than a finding.
                    db = Arc::new(Database::new());
                    server = Arc::new(Server::new(cache.clone(), db.clone(), m.clone()));
                }
                _ => {}
            }
        }

        if now.duration_since(last_evict) >= Duration::from_secs(1) {
            cache.evict(EVICT_PER_SEC);
            last_evict = now;
        }

        if rate > 0.0 {
            // No backpressure anywhere in that line. `tokio::spawn` always
            // succeeds, whatever the state of the system it is feeding. Every
            // OTHER queue in this file is bounded and correct; this one is the
            // only opt-out, and it is enough.
            m.tasks.fetch_add(1, Ordering::Relaxed);
            tasks.push(tokio::spawn(client_request(
                server.clone(),
                m.clone(),
                rng.below(KEYS),
            )));
        }

        if now.duration_since(last_report).as_secs_f64() >= REPORT_EVERY {
            let span = now.duration_since(last_report).as_secs_f64();
            let g = m.goodput.load(Ordering::Relaxed);
            let th = m.thruput_attempts.load(Ordering::Relaxed);
            let r = m.retries.load(Ordering::Relaxed);
            let (hits, misses) = cache.take_rates();
            rows.push(Row {
                t,
                offered: rate,
                thruput: (th - last.1) as f64 / span,
                goodput: (g - last.0) as f64 / span,
                hit: 100.0 * hits as f64 / (hits + misses).max(1) as f64,
                pg: db.in_use.load(Ordering::Relaxed),
                inflight: server.inflight.load(Ordering::Relaxed),
                tasks: m.tasks.load(Ordering::Relaxed),
                retry: (r - last.2) as f64 / ((th - last.1).max(1)) as f64,
            });
            last = (g, th, r);
            last_report = now;
        }
    }

    for h in tasks.drain(..) {
        h.abort();
    }
    sleep(Duration::from_millis(50)).await;
    (rows, end_at)
}

// -------------------------------------------------------------- reporting

const HEADER: &str = "      t   offered   thruput   goodput   hit%   pg  inflight   tasks  retry/req   goodput as % of offered";

fn render(title: &str, note: &str, rows: &[Row], end_at: f64) -> (f64, f64) {
    println!("\n=== {} ===", title);
    println!("    {}", note);
    println!("{}", HEADER);
    println!("{}", "-".repeat(HEADER.len()));
    for r in rows {
        let frac = r.goodput / OFFERED_RPS;
        let bar = "#".repeat((24.0 * frac.min(1.0)).round().max(0.0) as usize);
        let mark = if (r.t - TRIGGER_AT).abs() < REPORT_EVERY / 2.0 {
            "  <-- FLUSHALL"
        } else if (r.t - ESCAPE_AT).abs() < REPORT_EVERY / 2.0 {
            "  <-- escape applied"
        } else {
            ""
        };
        println!(
            "  {:5.1} {:9.1} {:9.1} {:9.1} {:6.1} {:4} {:9} {:7} {:10.2}   |{}{}",
            r.t, r.offered, r.thruput, r.goodput, r.hit, r.pg, r.inflight, r.tasks, r.retry, bar, mark
        );
    }
    let before: Vec<&Row> = rows.iter().filter(|r| r.t < TRIGGER_AT).collect();
    let after: Vec<&Row> = rows.iter().filter(|r| r.t >= end_at - 6.0).collect();
    let g_before = if before.is_empty() {
        0.0
    } else {
        before.iter().map(|r| r.goodput).sum::<f64>() / before.len() as f64
    };
    let g_after = if after.is_empty() {
        0.0
    } else {
        after.iter().map(|r| r.goodput).sum::<f64>() / after.len() as f64
    };
    println!(
        "    goodput before the trigger {:6.1} rps ({:.0}% of offered)   final 6 seconds {:6.1} rps ({:.0}% of offered)",
        g_before,
        100.0 * g_before / OFFERED_RPS,
        g_after,
        100.0 * g_after / OFFERED_RPS
    );
    (g_before, g_after)
}

/// COMPUTED from the run that just happened, never asserted here. Sufficient
/// means "goodput came back", not "the intervention did something
/// measurable" -- that distinction is the whole of step 5 in the README.
fn verdict(before: f64, after: f64) -> String {
    if before <= 1.0 {
        return "baseline never established -- see README".to_string();
    }
    let pct = 100.0 * after / before;
    if pct >= 70.0 {
        format!("SUFFICIENT   (recovered to {:.0}% of pre-trigger goodput)", pct)
    } else if pct >= 20.0 {
        format!("partial      (only {:.0}% of pre-trigger goodput)", pct)
    } else {
        format!("not sufficient ({:.0}% of pre-trigger goodput)", pct)
    }
}

#[tokio::main]
async fn main() {
    println!("Metastable failure: a cache flush that stops mattering long before the outage does.");
    println!(
        "Offered load is constant at {:.0} rps and is never raised. Cache hit rate {:.0}% when warm.",
        OFFERED_RPS,
        100.0 - 100.0 * EVICT_PER_SEC as f64 / OFFERED_RPS
    );
    let capacity = POOL_SIZE as f64 / DB_SERVICE.as_secs_f64();
    println!(
        "Database capacity is {}/{:.3} = {:.0} queries per second. Warm, the miss rate needs {} of them ({:.0}% utilised).",
        POOL_SIZE,
        DB_SERVICE.as_secs_f64(),
        capacity,
        EVICT_PER_SEC,
        100.0 * EVICT_PER_SEC as f64 / capacity
    );
    println!(
        "Cold, it needs all {:.0} -- {:.0}x capacity, before a single retry. Client timeout {}ms, {} attempts, no jitter, no budget, no shedding.",
        OFFERED_RPS,
        OFFERED_RPS / capacity,
        CLIENT_TIMEOUT.as_millis(),
        ATTEMPTS
    );
    println!(
        "FLUSHALL at t={:.0}s. Escapes, where a scenario has one, at t={:.0}s.",
        TRIGGER_AT, ESCAPE_AT
    );

    let scenarios: Vec<(&str, String, &str)> = vec![
        (
            "0 no escape: remove the trigger and wait",
            "The trigger was over in a millisecond. Watch the next 24 seconds.".to_string(),
            "",
        ),
        (
            "a drop offered load to zero, then ramp it back slowly",
            format!(
                "The one nobody wants to authorise. {:.0}s of zero, then {:.0}s of ramp. Watch the ramp, not the drop.",
                DROP_SECONDS, RAMP_BACK_SECONDS
            ),
            "a",
        ),
        (
            "b enable topic 3's 10% retry budget, load unchanged",
            "Removes the amplification. Does not remove the sustaining effect.".to_string(),
            "b",
        ),
        (
            "c enable topic 5's load shedder, load unchanged",
            format!("Admit at most {} in flight; 503 the rest, immediately.", SHED_LIMIT),
            "c",
        ),
        (
            "d restart the app, load unchanged",
            "Clears the tasks, the in-flight work and the pool. Not the cache.".to_string(),
            "d",
        ),
    ];

    let mut results: Vec<(String, f64, f64)> = Vec::new();
    for (title, note, escape) in &scenarios {
        let (rows, end_at) = run_scenario(escape).await;
        let (before, after) = render(title, note, &rows, end_at);
        results.push((title.to_string(), before, after));
    }

    println!("\n{}", "=".repeat(78));
    println!("{:<52}{:>15}{:>11}", "scenario", "goodput before", "after");
    println!("{}", "-".repeat(78));
    for (title, before, after) in &results {
        println!("{:<52}{:>14.1}{:>11.1}", title, before, after);
    }

    println!("\nScenario 0 is the whole topic. The trigger -- one FLUSHALL -- was over");
    println!("instantly and reversibly, offered load never changed by a single request,");
    println!("and goodput half a minute later is {:.1} rps -- which is what THIS run", results[0].2);
    println!("measured, not a sentence written before it. If it is not near zero, read");
    println!("the README's 'what would mean the experiment is broken' before reading");
    println!("anything else. Nothing is broken. Nothing needs rolling back. The system");
    println!("has settled into a second stable state, where the cache cannot refill");
    println!("because the database is saturated and the database is saturated because");
    println!("the cache is empty.");
    println!("\nEscapes, judged against THIS run rather than against a story:");
    for (title, before, after) in results.iter().skip(1) {
        println!("  {} {}", &title[..2], verdict(*before, *after));
    }
    println!(
        "  (scenario 0 finished at {:.1} rps of goodput, for comparison)",
        results[0].2
    );
    println!("\nWhat each escape actually touches, which is why they do not rank the way");
    println!("intuition ranks them:");
    println!("  (a) drop and ramp    removes load, not the loop. The drop always works;");
    println!("      the RAMP is the experiment. Full load returning to a cache that is");
    println!("      still empty walks straight back into the same state, so \"let it back");
    println!("      slowly\" is a QUANTITATIVE claim -- the ramp has to be slower than the");
    println!(
        "      cache can refill, which here is {:.0} keys per second against {} keys.",
        POOL_SIZE as f64 / DB_SERVICE.as_secs_f64(),
        KEYS
    );
    println!(
        "      Raise RAMP_BACK_SECONDS from {:.0} and find the threshold yourself.",
        RAMP_BACK_SECONDS
    );
    println!("  (b) retry budget     removes topic 3's amplification and leaves the");
    println!("      sustaining effect untouched. \"We turned the retries off\" is a sentence");
    println!("      people say in incidents that are still ongoing twenty minutes later.");
    println!("  (c) load shedding    is the only one that breaks the FEEDBACK LOOP: it is");
    println!("      the only intervention that lets the ADMITTED requests finish inside");
    println!("      their deadline, which is the exact condition the cache needs to");
    println!("      refill. Watch its hit% climb while retry/req falls -- that is the loop");
    println!("      running backwards.");
    println!("  (d) restart the app  clears everything the process owns and nothing the");
    println!("      clients own. The amplifier is in the clients. They did not restart.");
    println!("\nIn HotOS '25 vocabulary, worth writing down for your own system before");
    println!("you need it:");
    println!("  trigger                 a cache flush, over in one millisecond");
    println!("  amplification mechanism naive retries, plus the miss rate going from 10%");
    println!("                          to 100% on a database that was 60% utilised");
    println!("  sustaining effect       fills only happen on completions that beat the");
    println!("                          caller's deadline, and under overload none do");
}
