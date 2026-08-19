// Layer 6 Topic 2 - Coordinated omission: why your load test says the p99 is fine.
//
// Why Rust: because `std` deliberately ships no async runtime, this file is the
// clearest statement of the tradeoff that produced closed-loop load testing in
// the first place. An open-loop generator must hold every issued-but-unanswered
// request. With only the standard library that is one `std::thread` each -- a
// real kernel thread with a real stack -- and the program prints how many it
// created. Go holds the same hundred in-flight requests on ~0 extra OS threads;
// Node on none at all. You would have to reach for tokio to get that here, and
// the moment you do, the compiler starts asking you which of your futures are
// Send, which is a different lab.
//
// Rust's second contribution is at the type level. The shared latency vector is
// behind an Arc<Mutex<_>> not because a linter said so but because the program
// does not compile otherwise. In the C++ version of this same file the identical
// sharing is a plain global and nothing warns you. Same experiment, same data
// race available, one language that refuses to build it.
//
// What this demonstrates
// ----------------------
//   * Service: single server, FIFO queue, 3ms per request -> ~333 req/s.
//   * Offered load: 200 req/s, a comfortable 60% of capacity.
//   * At T+2.5s exactly one request takes 500ms. One request.
//
//   * CLOSED-LOOP: 4 virtual users, send -> wait -> think 20ms -> repeat.
//     This is `k6 run --vus 4`, and almost every load test ever written.
//   * OPEN-LOOP: requests issued at a fixed 200/s regardless of what came back.
//     This is k6's constant-arrival-rate executor, or `vegeta -rate=200`.
//
// What to look for in the output
// ------------------------------
//   1. "requests started IN the stall window": ~4 closed-loop, ~100 open-loop.
//      That one line is the entire mechanism.
//   2. The p99 rows. Same service, same fault, two answers.
//   3. "OS threads spawned by the generator" against the Go and Node runs.
//
// Run:  cargo run --release          (from this directory)

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::{channel, Sender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

const SERVICE_MS: u64 = 3; // -> ~333 req/s capacity
const STALL_AFTER_MS: u64 = 2500; // when the one slow request happens
const STALL_MS: u64 = 500; // how long that one request takes
const RUN_MS: u64 = 5000;
const OPEN_RATE_PER_SEC: u64 = 200; // offered load, ~60% of capacity
const CLOSED_VUS: u64 = 4;

fn closed_think() -> Duration {
    Duration::from_micros(CLOSED_VUS * 1_000_000 / OPEN_RATE_PER_SEC)
}

/// One unit of work handed to the service. `reply` carries the completion
/// instant back to whoever is waiting.
struct Job {
    reply: Sender<Instant>,
}

/// A single server with a FIFO queue. The queue is where the latency a
/// closed-loop generator cannot see accumulates.
struct Service {
    inbox: Option<Sender<Job>>,
    worker: Option<thread::JoinHandle<()>>,
}

impl Service {
    fn start(epoch: Instant) -> Service {
        let (tx, rx) = channel::<Job>();
        let worker = thread::spawn(move || {
            let mut stalled = false;
            while let Ok(job) = rx.recv() {
                if !stalled && epoch.elapsed() >= Duration::from_millis(STALL_AFTER_MS) {
                    stalled = true;
                    thread::sleep(Duration::from_millis(STALL_MS)); // the one bad request
                } else {
                    thread::sleep(Duration::from_millis(SERVICE_MS));
                }
                let _ = job.reply.send(Instant::now());
            }
        });
        Service {
            inbox: Some(tx),
            worker: Some(worker),
        }
    }

    fn handle(&self) -> Sender<Job> {
        self.inbox.as_ref().unwrap().clone()
    }

    fn stop(&mut self) {
        drop(self.inbox.take()); // closing the channel ends the worker loop
        if let Some(w) = self.worker.take() {
            let _ = w.join();
        }
    }
}

/// One completed request, in milliseconds since the run started.
#[derive(Clone, Copy, Debug)]
struct Sample {
    sent_offset_ms: f64,
    latency_from_sent_ms: f64,
    latency_from_arrival_ms: f64,
}

fn percentile(values: &[f64], q: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let mut ordered = values.to_vec();
    ordered.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let idx = ((q * (ordered.len() - 1) as f64) + 0.5) as usize;
    ordered[idx.min(ordered.len() - 1)]
}

fn started_in_stall(samples: &[Sample]) -> usize {
    samples
        .iter()
        .filter(|s| {
            s.sent_offset_ms >= STALL_AFTER_MS as f64
                && s.sent_offset_ms < (STALL_AFTER_MS + STALL_MS) as f64
        })
        .count()
}

struct RunResult {
    samples: Vec<Sample>,
    iteration_ms: Vec<f64>,
    peak_in_flight: usize,
    threads_spawned: usize,
}

fn run_closed_loop() -> RunResult {
    let epoch = Instant::now();
    let mut service = Service::start(epoch);
    // Arc<Mutex<_>> because the borrow checker will not let several threads
    // push into one Vec any other way. This is the compile-time enforcement
    // point: the C++ version of this file shares the same state with nothing
    // stopping it being wrong.
    let samples = Arc::new(Mutex::new(Vec::<Sample>::new()));
    let iteration_ms = Arc::new(Mutex::new(Vec::<f64>::new()));

    let mut users = Vec::new();
    for _ in 0..CLOSED_VUS {
        let inbox = service.handle();
        let samples = Arc::clone(&samples);
        let iteration_ms = Arc::clone(&iteration_ms);
        users.push(thread::spawn(move || {
            while epoch.elapsed() < Duration::from_millis(RUN_MS) {
                let iter_start = Instant::now();
                let (reply_tx, reply_rx) = channel::<Instant>();
                let sent = Instant::now();
                if inbox.send(Job { reply: reply_tx }).is_err() {
                    return;
                }
                let done = match reply_rx.recv() {
                    // <- this virtual user is now blocked, and while it is
                    //    blocked it is not offering any load at all
                    Ok(d) => d,
                    Err(_) => return,
                };
                let latency = done.duration_since(sent).as_secs_f64() * 1000.0;
                samples.lock().unwrap().push(Sample {
                    sent_offset_ms: sent.duration_since(epoch).as_secs_f64() * 1000.0,
                    latency_from_sent_ms: latency,
                    latency_from_arrival_ms: latency,
                });
                thread::sleep(closed_think());
                iteration_ms
                    .lock()
                    .unwrap()
                    .push(iter_start.elapsed().as_secs_f64() * 1000.0);
            }
        }));
    }
    for u in users {
        let _ = u.join();
    }
    service.stop();

    let samples = Arc::try_unwrap(samples).unwrap().into_inner().unwrap();
    let iteration_ms = Arc::try_unwrap(iteration_ms).unwrap().into_inner().unwrap();
    RunResult {
        samples,
        iteration_ms,
        peak_in_flight: CLOSED_VUS as usize,
        threads_spawned: CLOSED_VUS as usize,
    }
}

fn run_open_loop() -> RunResult {
    let epoch = Instant::now();
    let mut service = Service::start(epoch);
    let samples = Arc::new(Mutex::new(Vec::<Sample>::new()));
    let in_flight = Arc::new(AtomicUsize::new(0));
    let peak_in_flight = Arc::new(AtomicUsize::new(0));

    let mut handles = Vec::new();
    let interval = Duration::from_nanos(1_000_000_000 / OPEN_RATE_PER_SEC);
    let mut seq: u32 = 0;
    while interval * seq < Duration::from_millis(RUN_MS) {
        let target = epoch + interval * seq;
        let now = Instant::now();
        if target > now {
            thread::sleep(target - now);
        }
        seq += 1;

        let inbox = service.handle();
        let samples = Arc::clone(&samples);
        let in_flight = Arc::clone(&in_flight);
        let peak_in_flight = Arc::clone(&peak_in_flight);
        // One OS thread per in-flight request. This is what open-loop costs
        // with only the standard library, and it is the practical reason
        // closed-loop generators became the default.
        handles.push(thread::spawn(move || {
            let now = in_flight.fetch_add(1, Ordering::SeqCst) + 1;
            peak_in_flight.fetch_max(now, Ordering::SeqCst);
            let (reply_tx, reply_rx) = channel::<Instant>();
            let sent = Instant::now();
            if inbox.send(Job { reply: reply_tx }).is_err() {
                in_flight.fetch_sub(1, Ordering::SeqCst);
                return;
            }
            if let Ok(done) = reply_rx.recv() {
                samples.lock().unwrap().push(Sample {
                    sent_offset_ms: sent.duration_since(epoch).as_secs_f64() * 1000.0,
                    latency_from_sent_ms: done.duration_since(sent).as_secs_f64() * 1000.0,
                    // Measured from when the request was DUE, not from when the
                    // generator got round to sending it.
                    latency_from_arrival_ms: done.duration_since(target).as_secs_f64() * 1000.0,
                });
            }
            in_flight.fetch_sub(1, Ordering::SeqCst);
        }));
    }
    let threads_spawned = handles.len();
    for h in handles {
        let _ = h.join();
    }
    service.stop();

    let samples = Arc::try_unwrap(samples).unwrap().into_inner().unwrap();
    RunResult {
        samples,
        iteration_ms: Vec::new(),
        peak_in_flight: peak_in_flight.load(Ordering::SeqCst),
        threads_spawned,
    }
}

fn main() {
    let bar = "=".repeat(74);
    println!("{}", bar);
    println!("COORDINATED OMISSION   (Rust std, single-server FIFO service)");
    println!("{}", bar);
    println!(
        "service capacity ~{} req/s ({}ms/request), offered load {} req/s",
        1000 / SERVICE_MS,
        SERVICE_MS,
        OPEN_RATE_PER_SEC
    );
    println!(
        "one request at T+{}ms takes {}ms instead of {}ms",
        STALL_AFTER_MS, STALL_MS, SERVICE_MS
    );
    println!("run length {}ms\n", RUN_MS);

    println!(
        "running closed-loop ({} virtual users, {}ms think time)...",
        CLOSED_VUS,
        closed_think().as_millis()
    );
    let closed = run_closed_loop();
    println!("running open-loop ({} req/s arrival rate)...\n", OPEN_RATE_PER_SEC);
    let open = run_open_loop();

    let closed_latency: Vec<f64> = closed.samples.iter().map(|s| s.latency_from_sent_ms).collect();
    let open_latency: Vec<f64> = open.samples.iter().map(|s| s.latency_from_arrival_ms).collect();

    println!("{:<38}{:>14}{:>14}", "", "CLOSED-LOOP", "OPEN-LOOP");
    println!(
        "{:<38}{:>14}{:>14}",
        "requests completed",
        closed.samples.len(),
        open.samples.len()
    );
    println!(
        "{:<38}{:>14}{:>14}",
        "requests started IN the stall window",
        started_in_stall(&closed.samples),
        started_in_stall(&open.samples)
    );
    println!(
        "{:<38}{:>14}{:>14}",
        "peak requests in flight", closed.peak_in_flight, open.peak_in_flight
    );
    println!(
        "{:<38}{:>14}{:>14}",
        "OS threads spawned by the generator", closed.threads_spawned, open.threads_spawned
    );
    println!();
    for (label, q) in [
        ("p50", 0.50),
        ("p75", 0.75),
        ("p95", 0.95),
        ("p99", 0.99),
        ("p99.9", 0.999),
        ("max", 1.0),
    ] {
        println!(
            "{:<38}{:>12.1}ms{:>12.1}ms",
            format!("latency {}", label),
            percentile(&closed_latency, q),
            percentile(&open_latency, q)
        );
    }

    println!("\nThe closed-loop column measures request duration: send -> response.");
    println!("The open-loop column measures from the moment the request was DUE.");
    println!(
        "Note the first row too: closed-loop completed {} requests to open-loop's",
        closed.samples.len()
    );
    println!(
        "{}. It did not go slower -- it asked for less, precisely while the",
        open.samples.len()
    );
    println!("service was worst.");

    println!("\nThe tell, inside the closed-loop run alone:");
    println!(
        "  request duration p99   : {:8.1}ms",
        percentile(&closed_latency, 0.99)
    );
    println!(
        "  iteration duration p99 : {:8.1}ms",
        percentile(&closed.iteration_ms, 0.99)
    );
    println!("  If iteration_duration climbs while http_req_duration does not, your");
    println!("  generator stopped asking. That is k6's version of this same line.");

    let c99 = percentile(&closed_latency, 0.99);
    let o99 = percentile(&open_latency, 0.99);
    if c99 > 0.0 {
        println!(
            "\nVERDICT: open-loop p99 is {:.1}x the closed-loop p99 for the identical",
            o99 / c99
        );
        println!("service and the identical fault.");
    }
    let hit = started_in_stall(&closed.samples);
    println!(
        "The closed-loop generator sampled the stall {} times out of {} requests",
        hit,
        closed.samples.len()
    );
    println!(
        "({:.2}%), which is why it never reaches the 99th percentile.",
        100.0 * hit as f64 / closed.samples.len().max(1) as f64
    );
    println!("\nRust footnote: the last row of the table is the cost of honesty with");
    println!("std alone -- one OS thread per in-flight request. Go and Node hold the");
    println!("same hundred in flight for nothing. That gap, not correctness, is why");
    println!("load generators are not written against std::thread.");
}
