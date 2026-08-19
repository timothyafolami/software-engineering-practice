// Layer 1 - The fix: tokio::task::spawn_blocking hands the blocking call to
// a dedicated blocking-thread pool that every tokio runtime keeps around
// (yes, even the single-threaded `current_thread` flavor), so the async
// executor thread stays free to keep polling the ticker's timer.
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const TICK_INTERVAL: Duration = Duration::from_millis(100);
const BLOCK_FOR: Duration = Duration::from_secs(1);
const LEAD_IN: Duration = Duration::from_millis(200);
const LEAD_OUT: Duration = Duration::from_millis(200);

#[tokio::main(flavor = "current_thread")]
async fn main() {
    // Same gap-tracking as bad_blocking.rs, for a fair comparison -- see
    // that file's comment on why raw tick count alone can mislead here.
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

    // GOOD: the only change from bad_blocking -- run the blocking call on
    // tokio's blocking thread pool and await the JoinHandle instead of
    // calling it inline. The current_thread executor stays free the whole
    // time, so it keeps polling the ticker's timer normally.
    tokio::task::spawn_blocking(|| std::thread::sleep(BLOCK_FOR))
        .await
        .unwrap();

    tokio::time::sleep(LEAD_OUT).await;
    ticker.abort();
    tokio::time::sleep(Duration::from_millis(10)).await;

    let elapsed = start.elapsed().as_secs_f64();
    let ts = timestamps.lock().unwrap();
    let mut max_gap = ts.first().copied().unwrap_or(elapsed);
    for w in ts.windows(2) {
        max_gap = max_gap.max(w[1] - w[0]);
    }
    let expected = elapsed / TICK_INTERVAL.as_secs_f64();
    println!(
        "[good] ticks counted: {}  over {:.2}s  (expected ~{:.0} if never blocked)  max gap between ticks: {:.2}s",
        ts.len(),
        elapsed,
        expected,
        max_gap
    );
}
