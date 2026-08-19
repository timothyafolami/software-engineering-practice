// Layer 1 - Rust's concurrency model has no single default: async code
// only runs at all because of a runtime crate like tokio, and that runtime
// makes explicit choices you have to know about. Here we use the
// `current_thread` flavor -- one OS thread runs every task -- so a
// std::thread::sleep() called directly inside an async fn blocks that one
// thread completely, exactly like the Python asyncio and naive Node cases.
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const TICK_INTERVAL: Duration = Duration::from_millis(100);
const BLOCK_FOR: Duration = Duration::from_secs(1);
const LEAD_IN: Duration = Duration::from_millis(200);
const LEAD_OUT: Duration = Duration::from_millis(200);

#[tokio::main(flavor = "current_thread")]
async fn main() {
    // NOTE: counting total ticks alone is not enough here. tokio's
    // `interval()` defaults to `MissedTickBehavior::Burst`, which means if
    // the ticker task couldn't be polled for a while, the next time it CAN
    // run it fires all of its missed ticks back-to-back immediately rather
    // than waiting out real time for each one. That "catches up" the count
    // to nearly what you'd expect even though the ticker was fully stalled
    // -- so we record the wall-clock timestamp of every tick and look at
    // the *gap* between consecutive ticks instead. A stalled ticker still
    // shows one enormous gap, even with its count fully caught up.
    let timestamps = Arc::new(Mutex::new(Vec::<f64>::new()));
    let timestamps_writer = timestamps.clone();
    let start = Instant::now();

    let ticker = tokio::spawn(async move {
        let mut interval = tokio::time::interval(TICK_INTERVAL);
        loop {
            interval.tick().await;
            timestamps_writer.lock().unwrap().push(start.elapsed().as_secs_f64());
        }
    });

    tokio::time::sleep(LEAD_IN).await;

    // BAD: a blocking call made directly inside an async task. On the
    // current_thread runtime there is only one thread, and this owns it
    // completely for the full duration -- tokio's timer wheel can't even
    // be polled, so `ticker` above cannot make progress.
    std::thread::sleep(BLOCK_FOR);

    tokio::time::sleep(LEAD_OUT).await;
    ticker.abort();
    // Give the aborted task's already-queued wakeup a moment to land so we
    // don't race the final timestamp push.
    tokio::time::sleep(Duration::from_millis(10)).await;

    let elapsed = start.elapsed().as_secs_f64();
    let ts = timestamps.lock().unwrap();
    let mut max_gap = ts.first().copied().unwrap_or(elapsed);
    for w in ts.windows(2) {
        max_gap = max_gap.max(w[1] - w[0]);
    }
    let expected = elapsed / TICK_INTERVAL.as_secs_f64();
    println!(
        "[bad] ticks counted: {}  over {:.2}s  (expected ~{:.0} if never blocked)  max gap between ticks: {:.2}s",
        ts.len(),
        elapsed,
        expected,
        max_gap
    );
}
