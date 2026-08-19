// Layer 2 · Topic 5 - Rust: the same blocking getaddrinfo, on a pool that was
// chosen for you correctly.
//
// There is no asynchronous name resolution in POSIX worth using. std's
// ToSocketAddrs calls the blocking getaddrinfo, exactly like Python's socket
// module and exactly like C's. Every runtime in this topic is wrapping that
// same call in a thread pool of some shape; what differs is WHICH pool.
//
//   Python asyncio  the DEFAULT ThreadPoolExecutor -- shared with every
//                   run_in_executor call in your codebase (see the Python
//                   file in this topic: 451x resolution latency when that
//                   pool is busy)
//   Node            libuv's pool -- shared with file IO and crypto, four
//                   threads by default
//   tokio           spawn_blocking's DEDICATED blocking pool, separate from
//                   the reactor's worker threads
//
// That last line is the whole reason a Rust service degrades more gracefully
// during a DNS stall than an asyncio one. It is not a language property. It
// is one default, chosen once, in a runtime crate.
//
// This program proves it by keeping a ticker running -- exactly the Layer 1
// Topic 3 instrument -- while resolution happens two ways on a runtime with
// ONE worker thread:
//
//   A. std::net::ToSocketAddrs called directly inside an async task. The
//      blocking syscall owns the single worker. The ticker stops.
//   B. tokio::net::lookup_host, which is the same syscall behind
//      spawn_blocking. The worker stays free. The ticker keeps ticking.
//
// What to look for in the output:
//   - the max gap between ticks in A versus B. The tick COUNT recovers in
//     both cases because tokio's interval bursts missed ticks; the timing
//     does not, which is the Layer 1 lesson arriving here again.
//   - do NOT compare the two "ms resolving" figures directly. A runs first and
//     warms your OS resolver's cache, so B is measuring a cache hit. The
//     comparison this program can honestly make is the max-gap column: what
//     the same work did to everything else on the runtime.
//
// Run: cargo run --release
use std::net::ToSocketAddrs;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

const NAMES: [&str; 4] = ["example.com", "www.example.com", "example.org", "localhost"];
const TICK: Duration = Duration::from_millis(50);

struct Ticks {
    count: AtomicU64,
    max_gap_ms: AtomicU64,
}

async fn ticker(t: Arc<Ticks>, stop: Arc<tokio::sync::Notify>) {
    let mut interval = tokio::time::interval(TICK);
    let mut last = Instant::now();
    loop {
        tokio::select! {
            _ = interval.tick() => {
                let gap = last.elapsed().as_millis() as u64;
                last = Instant::now();
                t.count.fetch_add(1, Ordering::Relaxed);
                t.max_gap_ms.fetch_max(gap, Ordering::Relaxed);
            }
            _ = stop.notified() => return,
        }
    }
}

/// A: the blocking call, made where it must not be made.
async fn resolve_on_the_worker() -> usize {
    let mut n = 0;
    for name in NAMES {
        // std::net's resolver. This is getaddrinfo, on this thread, right now.
        // The runtime has one worker; nothing else on it runs until this
        // returns. Nothing in the type system objects.
        if let Ok(addrs) = (name, 80u16).to_socket_addrs() {
            n += addrs.count();
        }
    }
    n
}

/// B: the same syscall, on the blocking pool, because tokio put it there.
async fn resolve_on_the_blocking_pool() -> usize {
    let mut n = 0;
    for name in NAMES {
        if let Ok(addrs) = tokio::net::lookup_host((name, 80u16)).await {
            n += addrs.count();
        }
    }
    n
}

async fn measure<F, Fut>(label: &str, f: F)
where
    F: FnOnce() -> Fut,
    Fut: std::future::Future<Output = usize>,
{
    let ticks = Arc::new(Ticks { count: AtomicU64::new(0), max_gap_ms: AtomicU64::new(0) });
    let stop = Arc::new(tokio::sync::Notify::new());
    let handle = tokio::spawn(ticker(ticks.clone(), stop.clone()));

    // Let the ticker establish a rhythm before we interfere with it.
    tokio::time::sleep(Duration::from_millis(200)).await;

    let t0 = Instant::now();
    let addrs = f().await;
    let elapsed = t0.elapsed();

    tokio::time::sleep(Duration::from_millis(200)).await;
    stop.notify_one();
    let _ = handle.await;

    println!(
        "    {label:<34} {:>6} ms resolving   ticks {:>3}   max gap {:>5} ms   ({} addresses)",
        elapsed.as_millis(),
        ticks.count.load(Ordering::Relaxed),
        ticks.max_gap_ms.load(Ordering::Relaxed),
        addrs
    );
}

// ONE worker thread, deliberately. A multi-threaded runtime would hide the
// effect behind spare workers -- which is how this bug survives code review
// on a developer's eight-core laptop and appears on a one-core container.
#[tokio::main(flavor = "current_thread")]
async fn main() {
    println!("{}", "=".repeat(78));
    println!("Rust: getaddrinfo blocks. tokio just puts it somewhere it cannot hurt you");
    println!("{}", "=".repeat(78));
    println!("  runtime flavor: current_thread (one worker for everything)");
    println!("  ticker interval: {} ms   names resolved per run: {:?}", TICK.as_millis(), NAMES);
    println!();
    println!("  If a name below does not resolve, the row still measures what it is");
    println!("  supposed to measure -- a failed lookup blocks the calling thread for");
    println!("  just as long as a successful one, often longer.");
    println!();

    measure("A. ToSocketAddrs on the worker", resolve_on_the_worker).await;
    measure("B. lookup_host (blocking pool)", resolve_on_the_blocking_pool).await;

    println!();
    println!("  Read the max-gap column -- not the tick count, and not the two");
    println!("  \"ms resolving\" figures against each other: A ran first and warmed the");
    println!("  OS resolver's cache, so B is timing a cache hit. Run them in the other");
    println!("  order and the resolving times swap. The max gap does not.");
    println!("    tokio's interval defaults to MissedTickBehavior::Burst, so ticks that");
    println!("    could not fire while the worker was blocked all fire at once the");
    println!("    moment it frees up. The COUNT recovers; the TIMING does not. If you");
    println!("    only monitored completed work here you would see nothing wrong --");
    println!("    which is exactly Layer 1 Topic 3's finding, arriving again through a");
    println!("    completely different door.");
    println!();
    println!("  What this does NOT fix:");
    println!("    spawn_blocking's pool is bounded too (512 threads by default). It is a");
    println!("    wider queue, not the absence of a queue. Saturate it with your own");
    println!("    blocking work and resolution starts queueing again -- the same shape");
    println!("    as Python's default executor, just much harder to fill.");
    println!();
    println!("  And the thing to carry to the rest of this topic: none of this caches.");
    println!("  std does not, tokio does not. Swap in hickory-dns and you get an async");
    println!("  resolver that honours record TTLs, at the price of no longer consulting");
    println!("  /etc/hosts or nsswitch unless you configure it to -- which is the same");
    println!("  trade Node's dns.resolve() makes against dns.lookup().");
}
