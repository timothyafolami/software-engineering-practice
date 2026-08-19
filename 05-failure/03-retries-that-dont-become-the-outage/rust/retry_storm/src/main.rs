// Layer 5 - Topic 3: retry amplification, in one Rust process.
//
// Rust makes the state machine explicit. tower::retry takes a Policy trait
// with two methods -- retry() and clone_request() -- and the second one is
// the interesting half, because it forces you to answer "can this request
// even BE replayed?" before the code compiles. A streaming body is not
// clonable, so the type system asks the idempotency question that topic 7
// answers, at compile time, whether you were ready for it or not.
//
// The Policy trait below is that shape, cut down to what this experiment
// needs. No mainstream Rust crate ships a retry budget either; a shared
// AtomicI64 token bucket is the natural implementation, and it is here.
//
// WHAT THIS DEMONSTRATES
//
//   gateway -> service_b -> service_c -> database, each hop retrying up to
//   3 times. The database refuses connections for a window in the middle
//   of the run. The leaf counter counts DATABASE CALLS, so the theoretical
//   worst case is 3 hops x 3 attempts = 27x the offered rate.
//
//     A naive       exponential backoff, no jitter, no budget
//     B + jitter    full jitter: sleep = random(0, min(cap, base * 2**n))
//     C + budget    a 10% token bucket at every hop, Envoy-style
//     D edge only   only the hop adjacent to the database retries, and it
//                   marks the error non-retryable on the way up
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. `amp` during the fault, and -- much more importantly -- what it does
//      AFTER the fault clears. Once retries have built a queue, the queue
//      causes the next round of retries, and that loop can sustain itself
//      with the fault long gone. Read YOUR run: `mean amp from 16s onward`
//      and `success after` are the two numbers, and this program is not
//      going to promise you which way they land. The chain is BISTABLE at
//      these constants -- 150 rps offered against 200 rps of leaf capacity
//      -- so whether the backlog is small enough to work off when the fault
//      clears decides it. Rerunning, or running the same policy in another
//      language in this folder, can land in the other basin. That is the
//      finding, not flakiness, and it is topic 4 arriving uninvited.
//      What is NOT bistable is variant C. Look at it first.
//   2. Variant C's retry traffic falling to zero on its own as failures
//      climb. Nobody decides that; the bucket runs dry.
//   3. Variant D's peak being one hop's attempts, not three hops' product.
//   4. The synchronised-cohort histogram at the end, which is the only
//      place in this file where jitter looks like a good idea.
//
// RUN
//   cargo run --release

use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::sync::Semaphore;
use tokio::time::{sleep, timeout};

// ------------------------------------------------------------------ config

const OFFERED_RPS: f64 = 150.0;
const DURATION: Duration = Duration::from_secs(24);
const FAULT_ON: Duration = Duration::from_secs(5);
const FAULT_OFF: Duration = Duration::from_secs(12);
const REPORT_BUCKET: Duration = Duration::from_secs(2);

const ATTEMPTS: usize = 3;
const BASE_BACKOFF: Duration = Duration::from_millis(50);
const BACKOFF_CAP: Duration = Duration::from_millis(400);
const ATTEMPT_TIMEOUT: Duration = Duration::from_millis(300);
const REQUEST_BUDGET: Duration = Duration::from_millis(1500);

const LEAF_POOL: usize = 8;
const LEAF_SERVICE: Duration = Duration::from_millis(40);

const BUDGET_RATIO: f64 = 0.10;
const BUDGET_FLOOR: f64 = 3.0;

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

// ------------------------------------------------------------ retry budget

/// A token bucket that permits retries only while retries stay under some
/// fraction of successes -- Envoy's budget_percent, gRPC's retryThrottling,
/// the 10% Yandex reported settling on. No mainstream Rust crate ships one.
///
/// The property is qualitative rather than numeric: at low failure rates
/// this is indistinguishable from an ordinary retrying client, and as
/// failures climb its retry traffic goes to ZERO by itself. Backoff delays
/// amplification; only this bounds it.
struct RetryBudget {
    /// Milli-tokens, so the 0.1-per-success refill fits in an integer atomic
    /// and the whole thing stays lock-free.
    tokens: AtomicI64,
    ceiling: i64,
}

impl RetryBudget {
    fn new() -> Self {
        RetryBudget {
            tokens: AtomicI64::new((BUDGET_FLOOR * 1000.0) as i64),
            ceiling: ((BUDGET_FLOOR + 100.0) * 1000.0) as i64,
        }
    }

    /// Refills on SUCCESSES, never on wall-clock. A clock-refilled bucket
    /// gives an idle service free retries it never earned, and hands a
    /// service in total outage a steady drip of amplification forever.
    fn deposit(&self) {
        let add = (BUDGET_RATIO * 1000.0) as i64;
        let _ = self.tokens.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |t| {
            Some((t + add).min(self.ceiling))
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

// -------------------------------------------------------------- the errors

#[derive(Debug, Clone, Copy, PartialEq)]
enum CallError {
    /// Transient: connect error, connect timeout, 429, 503. Worth retrying.
    Unavailable,
    /// Deadline gone. Retrying spends budget nobody has.
    Expired,
    /// Variant D's whole mechanism: "I already spent the attempts, do not
    /// spend yours."
    NonRetryable,
}

// ------------------------------------------------------------- the policy

/// tower::retry's Policy, cut down to what this experiment needs. The two
/// methods are the point.
trait Policy {
    /// Should this failure be retried, and how long should we wait first?
    fn retry(&self, attempt: usize, err: CallError) -> Option<Duration>;

    /// Can this request even BE replayed? tower makes you answer this to
    /// compile, which is the type system asking topic 7's question. A
    /// streaming body is not clonable and a non-idempotent write should not
    /// be, so returning None here is a design statement, not a limitation.
    fn clone_request(&self) -> Option<()>;
}

struct ExponentialBackoff {
    jitter: bool,
    rng: Arc<Rng>,
}

impl Policy for ExponentialBackoff {
    fn retry(&self, attempt: usize, err: CallError) -> Option<Duration> {
        // (1) Only retry what is genuinely transient.
        if err != CallError::Unavailable || attempt + 1 >= ATTEMPTS {
            return None;
        }
        let bounded = (BASE_BACKOFF * (1 << attempt) as u32).min(BACKOFF_CAP);
        Some(if self.jitter {
            // Full jitter, the AWS Builders' Library recommendation: spread
            // a synchronised cohort across the WHOLE interval rather than
            // around a common centre.
            bounded.mul_f64(self.rng.next_f64())
        } else {
            bounded
        })
    }

    fn clone_request(&self) -> Option<()> {
        Some(())   // a GET. Topic 7 is about earning this answer for a POST.
    }
}

// ---------------------------------------------------------------- metrics

#[derive(Default)]
struct Metrics {
    leaf_received: AtomicI64,
    ok: AtomicI64,
    failed: AtomicI64,
    retries: AtomicI64,
    budget_denied: AtomicI64,
    samples: Mutex<Vec<Sample>>,
}

#[derive(Clone, Copy)]
struct Sample {
    t: f64,
    received: f64,
    amp: f64,
    success: f64,
}

// ------------------------------------------------------------------ leaf

struct Leaf {
    sem: Semaphore,
    m: Arc<Metrics>,
    faulty: AtomicBool,
}

impl Leaf {
    async fn call(&self) -> Result<(), CallError> {
        // THE COUNTER THAT MATTERS. Requests RECEIVED, not requests
        // succeeded. Divided by the client's offered rate it is the live
        // amplification factor.
        self.m.leaf_received.fetch_add(1, Ordering::Relaxed);

        if self.faulty.load(Ordering::Relaxed) {
            // Connection refused: fast, cheap, and therefore the worst kind
            // of failure for a retrying client, because the retry arrives
            // almost immediately.
            return Err(CallError::Unavailable);
        }
        let _permit = self.sem.acquire().await.unwrap();
        sleep(LEAF_SERVICE).await;
        Ok(())
    }
}

// ------------------------------------------------------------- the chain

struct Chain {
    leaf: Arc<Leaf>,
    m: Arc<Metrics>,
    policy: ExponentialBackoff,
    budgets: Option<[Arc<RetryBudget>; 3]>,
    edge_only: bool,
}

impl Chain {
    async fn with_retries<F, Fut>(&self, hop: usize, deadline: Instant, call: F)
        -> Result<(), CallError>
    where
        F: Fn() -> Fut,
        Fut: std::future::Future<Output = Result<(), CallError>>,
    {
        let budget = self.budgets.as_ref().map(|b| b[hop].clone());
        let mut last = CallError::Unavailable;

        for attempt in 0..ATTEMPTS {
            if attempt > 0 {
                let Some(wait) = self.policy.retry(attempt - 1, last) else {
                    return Err(last);
                };
                // clone_request is what tower asks BEFORE the retry, and the
                // honest place to refuse one you cannot replay.
                if self.policy.clone_request().is_none() {
                    return Err(last);
                }
                // (4) The budget, checked BEFORE the sleep, so a denied retry
                // costs nothing at all -- not even the wait.
                if let Some(b) = &budget {
                    if !b.withdraw() {
                        self.m.budget_denied.fetch_add(1, Ordering::Relaxed);
                        return Err(last);
                    }
                }
                self.m.retries.fetch_add(1, Ordering::Relaxed);

                // (3) A hard cap that fits inside the caller's budget. A
                // retry policy allowed to outlive its caller's deadline is
                // generating topic 2's zombie work on purpose.
                if Instant::now() + wait > deadline {
                    return Err(last);
                }
                sleep(wait).await;
            }

            let now = Instant::now();
            if now >= deadline {
                return Err(last);
            }
            let per_attempt = ATTEMPT_TIMEOUT.min(deadline - now);
            match timeout(per_attempt, call()).await {
                Ok(Ok(())) => {
                    if let Some(b) = &budget {
                        b.deposit();
                    }
                    return Ok(());
                }
                Ok(Err(CallError::NonRetryable)) => return Err(CallError::NonRetryable),
                Ok(Err(e)) => last = e,
                Err(_) => last = CallError::Unavailable,
            }
        }
        Err(last)
    }

    async fn service_c(&self, deadline: Instant) -> Result<(), CallError> {
        let r = self.with_retries(2, deadline, || self.leaf.call()).await;
        match r {
            // THE STRUCTURAL FIX. The hop next to the failure has already
            // spent its attempts; saying so upward turns the worst case from
            // 3**3 back into 3. It composes cleanly with topic 2 and is far
            // easier to reason about than any amount of tuning.
            Err(e) if self.edge_only && e != CallError::NonRetryable => {
                Err(CallError::NonRetryable)
            }
            other => other,
        }
    }

    async fn service_b(&self, deadline: Instant) -> Result<(), CallError> {
        self.with_retries(1, deadline, || self.service_c(deadline)).await
    }

    async fn gateway(&self) {
        let deadline = Instant::now() + REQUEST_BUDGET;
        match self.with_retries(0, deadline, || self.service_b(deadline)).await {
            Ok(()) => self.m.ok.fetch_add(1, Ordering::Relaxed),
            Err(_) => self.m.failed.fetch_add(1, Ordering::Relaxed),
        };
    }
}

// ------------------------------------------------------------- the driver

async fn run_variant(jitter: bool, budgeted: bool, edge_only: bool) -> Arc<Metrics> {
    let m = Arc::new(Metrics::default());
    let leaf = Arc::new(Leaf {
        sem: Semaphore::new(LEAF_POOL),
        m: m.clone(),
        faulty: AtomicBool::new(false),
    });
    let chain = Arc::new(Chain {
        leaf: leaf.clone(),
        m: m.clone(),
        policy: ExponentialBackoff { jitter, rng: Arc::new(Rng::new(777)) },
        // One bucket per hop, shared across every request that hop handles.
        // Per-request state would defeat the whole idea: the budget exists to
        // make one client's retries visible to the next client's.
        budgets: budgeted.then(|| {
            [Arc::new(RetryBudget::new()), Arc::new(RetryBudget::new()), Arc::new(RetryBudget::new())]
        }),
        edge_only,
    });

    let arrivals = Rng::new(20250503);
    let begin = Instant::now();
    let end = begin + DURATION;
    let mut at = begin;
    let mut last_bucket = begin;
    let (mut last_received, mut last_ok, mut last_total) = (0i64, 0i64, 0i64);
    let mut handles = Vec::new();

    loop {
        at += arrivals.exp(OFFERED_RPS);
        if at > end {
            break;
        }
        let now = Instant::now();
        if at > now {
            sleep(at - now).await;
        }
        let t = begin.elapsed();
        leaf.faulty.store(t >= FAULT_ON && t < FAULT_OFF, Ordering::Relaxed);

        let c = chain.clone();
        handles.push(tokio::spawn(async move { c.gateway().await }));

        if last_bucket.elapsed() >= REPORT_BUCKET {
            let span = last_bucket.elapsed().as_secs_f64();
            let received = (m.leaf_received.load(Ordering::Relaxed) - last_received) as f64 / span;
            let ok = m.ok.load(Ordering::Relaxed);
            let total = ok + m.failed.load(Ordering::Relaxed);
            let done = total - last_total;
            m.samples.lock().unwrap().push(Sample {
                t: t.as_secs_f64(),
                received,
                amp: received / OFFERED_RPS,
                success: if done > 0 { 100.0 * (ok - last_ok) as f64 / done as f64 } else { 0.0 },
            });
            last_bucket = Instant::now();
            last_received = m.leaf_received.load(Ordering::Relaxed);
            last_ok = ok;
            last_total = total;
        }
    }
    for h in handles {
        let _ = h.await;
    }
    m
}

// ------------------------------------------------------------- reporting

fn render(label: &str, m: &Metrics) -> (String, f64, f64, f64) {
    println!("\n=== {} ===", label);
    println!("     t   leaf rps      amp   success                 amplification");
    let samples = m.samples.lock().unwrap().clone();
    let peak = samples.iter().map(|s| s.amp).fold(0.0, f64::max);
    let scale = peak.max(1.0);
    for s in &samples {
        let n = (34.0 * s.amp / scale).round() as usize;
        let fault = if s.t >= FAULT_ON.as_secs_f64() && s.t < FAULT_OFF.as_secs_f64() {
            " FAULT"
        } else {
            "      "
        };
        println!("  {:5.1} {:10.1} {:8.2} {:8.1}%{} |{}", s.t, s.received, s.amp, s.success, fault, "#".repeat(n));
    }
    let after: Vec<&Sample> = samples.iter().filter(|s| s.t >= FAULT_OFF.as_secs_f64() + 4.0).collect();
    let mean = |v: Vec<f64>| if v.is_empty() { 0.0 } else { v.iter().sum::<f64>() / v.len() as f64 };
    let tail = mean(after.iter().map(|s| s.amp).collect());
    let tail_success = mean(after.iter().map(|s| s.success).collect());
    println!(
        "  peak amp {:.2}x   mean amp from {:.0}s onward {:.2}x   success after {:.1}%   retries {}   budget-denied {}",
        peak, FAULT_OFF.as_secs_f64() + 4.0, tail, tail_success,
        m.retries.load(Ordering::Relaxed), m.budget_denied.load(Ordering::Relaxed)
    );
    (label.to_string(), peak, tail, tail_success)
}

/// Why the table above makes jitter look useless, and why it is not.
///
/// In the sweep, arrivals are a Poisson process: every client fails at a
/// different moment already, so their retries were never going to collide.
/// Jitter has nothing to decorrelate, and full jitter's shorter average wait
/// actually lets MORE attempts fit inside the budget -- which is why variant
/// B can amplify harder than variant A.
///
/// Production is not that. Production is a thousand clients that were all
/// talking to the same dependency when it fell over at the same instant.
fn synchronised_cohort() {
    let rng = Rng::new(20250503);
    const CLIENTS: usize = 1000;
    let delay = (BASE_BACKOFF * 2).min(BACKOFF_CAP);

    let histogram = |title: &str, draw: &dyn Fn() -> Duration| {
        let buckets_n = 10;
        let width = BACKOFF_CAP / buckets_n as u32;
        let mut buckets = vec![0usize; buckets_n];
        for _ in 0..CLIENTS {
            let b = ((draw().as_secs_f64() / width.as_secs_f64()) as usize).min(buckets_n - 1);
            buckets[b] += 1;
        }
        println!("\n  {}", title);
        for (i, count) in buckets.iter().enumerate() {
            println!(
                "   {:5}-{:<5}ms |{} {}",
                (width * i as u32).as_millis(),
                (width * (i + 1) as u32).as_millis(),
                "#".repeat((48.0 * *count as f64 / CLIENTS as f64).round() as usize),
                count
            );
        }
        println!(
            "   peak instantaneous retry rate: {:.0} rps from {} clients",
            *buckets.iter().max().unwrap() as f64 / width.as_secs_f64(),
            CLIENTS
        );
    };

    println!("\n{}", "=".repeat(78));
    println!("Why the table above makes jitter look pointless: 1000 clients, one");
    println!("simultaneous failure, arrival times of their first retry.");
    histogram("no jitter -- sleep = min(cap, base * 2**n)", &|| delay);
    histogram("full jitter -- sleep = random(0, min(cap, base * 2**n))",
              &|| delay.mul_f64(rng.next_f64()));
    println!("\n  Same number of retries either way. Jitter does not reduce the");
    println!("  area, it reduces the PEAK, and the peak is what a service trying");
    println!("  to recover actually has to survive. The benefit is about");
    println!("  correlation, not about randomness, which is exactly why it is");
    println!("  invisible in a single-process test with independent arrivals.");
}

#[tokio::main]
async fn main() {
    println!("Retry amplification through gateway -> service_b -> service_c -> database, in Rust.");
    println!(
        "Offered {:.0} rps for {}s, database refuses connections from t={}s to t={}s.",
        OFFERED_RPS, DURATION.as_secs(), FAULT_ON.as_secs(), FAULT_OFF.as_secs()
    );
    println!(
        "{} attempts per hop over 3 hops = {}x worst case at the leaf; the leaf's real capacity is {}/{:.3} = {:.0} rps.",
        ATTEMPTS, ATTEMPTS.pow(3), LEAF_POOL, LEAF_SERVICE.as_secs_f64(),
        LEAF_POOL as f64 / LEAF_SERVICE.as_secs_f64()
    );
    println!("amp = database calls per second / offered rps. Watch what it does AFTER the fault clears.");

    let mut rows = Vec::new();
    rows.push(render("A naive: exponential backoff, no jitter", &*run_variant(false, false, false).await));
    rows.push(render("B + full jitter", &*run_variant(true, false, false).await));
    rows.push(render("C + 10% retry budget at every hop", &*run_variant(true, true, false).await));
    rows.push(render("D retry at the edge only", &*run_variant(true, false, true).await));

    println!("\n{}", "=".repeat(78));
    println!("{:<44}{:>10}{:>11}{:>14}", "variant", "peak amp", "amp after", "success after");
    println!("{}", "-".repeat(78));
    for (label, peak, tail, tail_success) in &rows {
        println!("{:<44}{:>9.2}x{:>10.2}x{:>13.1}%", label, peak, tail, tail_success);
    }

    println!();
    println!("The 27x worst case does not appear, and why it does not is the useful");
    println!("part: the per-attempt timeout and the request budget expire before the");
    println!("deepest retries can be attempted. Timeouts cap amplification by accident.");
    println!("Do not rely on an accident.");
    println!();
    println!("Variant B amplifying harder than A is not a bug in the experiment.");
    println!("Arrivals here are a Poisson process, so nothing was synchronised for");
    println!("jitter to decorrelate -- and full jitter's shorter average wait lets more");
    println!("attempts fit inside the same budget. Keep reading.");
    println!();
    println!("C is the only variant whose retry traffic falls as failures climb, and the");
    println!("only one that is a bound rather than a delay. D gets most of the same");
    println!("benefit structurally, by making the answer to 'which layer owns retries' a");
    println!("single layer.");

    synchronised_cohort();
}
