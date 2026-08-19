//! Layer 4 Topic 3 (Part A) -- Rust's clocks, audited rather than assumed.
//!
//! WHAT THIS DEMONSTRATES: four things, in order.
//!   1. the clock inventory. Rust has exactly two clock types and they are
//!      genuinely different types: `Instant` is monotonic, and there is
//!      deliberately no way to get a calendar date out of it; `SystemTime` is the
//!      wall clock, and it will not hand you a `Duration` without making you say
//!      what should happen if time ran backwards.
//!   2. one span timed twice -- through the application's own `now()`, which
//!      reads the wall clock, and through `Instant` -- with an NTP-style step
//!      applied inside two of the spans.
//!   3. the footgun, which in this language is an ABSENCE: the bug every other
//!      runtime in this topic lets you write does not compile here. This section
//!      shows the `Result` you are forced to handle, the `Err` you get when time
//!      goes backwards, and (in comments, with the verbatim compiler errors) the
//!      three lines you cannot write at all.
//!   4. the summary line for the README's record table.
//!
//! WHAT TO LOOK FOR IN THE OUTPUT: section 3. Every other language in this topic
//! reports a negative duration and moves on. Rust reports
//! `Err(SystemTimeError(...))` and refuses to produce a number, because the
//! answer to "how long was that?" when the clock stepped backwards is genuinely
//! not a Duration.
//!
//!   cd rust/clock_audit && cargo run --release

use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const STEP_BACK: Duration = Duration::from_secs(40);
const SPANS: usize = 400;
const SPAN_WORK: Duration = Duration::from_micros(200);

// ------------------------------------------------------------- 1. inventory

/// Smallest non-zero delta this clock reports. Measured, not documented -- a
/// clock can advertise nanoseconds and tick in microseconds, and on Darwin
/// several of them do.
fn measure_resolution_ns(mut read: impl FnMut() -> u128, trials: u32) -> u128 {
    let mut smallest = u128::MAX;
    for _ in 0..trials {
        let a = read();
        loop {
            let b = read();
            if b != a {
                smallest = smallest.min(b.abs_diff(a));
                break;
            }
        }
    }
    smallest
}

fn inventory() {
    println!("------------------------------------------------------------------------------");
    println!("1. the two clock types, and what the type system will not let you mix");
    println!("------------------------------------------------------------------------------");
    println!("  {:<40}{:<12}{}", "expression", "kind", "measured resolution");

    let base = Instant::now();
    let rows: Vec<(&str, &str, Box<dyn FnMut() -> u128>)> = vec![
        (
            "SystemTime::now() since UNIX_EPOCH",
            "realtime",
            Box::new(|| {
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .expect("clock before 1970")
                    .as_nanos()
            }),
        ),
        (
            "Instant::now().duration_since(base)",
            "monotonic",
            Box::new(move || base.elapsed().as_nanos()),
        ),
        (
            "Instant::now().elapsed()",
            "monotonic",
            Box::new(move || base.elapsed().as_nanos()),
        ),
    ];
    for (name, kind, mut read) in rows {
        let res = measure_resolution_ns(&mut read, 20);
        println!("  {:<40}{:<12}{:>12} ns", name, kind, res);
    }

    println!();
    println!("  Instant  : monotonic, opaque, NOT comparable across processes or reboots.");
    println!("             There is no Instant -> calendar conversion in std. That is not");
    println!("             an oversight; it is the API refusing to let you pretend.");
    println!("  SystemTime: wall clock, settable, and its duration_since returns a Result");
    println!("             because 'that was in the future' is a legitimate answer.");
}

// ------------------------------------------------- 2. one span, two clocks

/// The application's own `now()`. Every service has one; most read the wall
/// clock. The offset stands in for an NTP step -- we never touch the system
/// clock, and `lab/README.md` explains why per-container skew is not possible
/// here anyway. Signed, because NTP steps go both ways.
struct AppClock {
    offset_ns: i128,
}

impl AppClock {
    fn new() -> Self {
        AppClock { offset_ns: 0 }
    }

    /// Nanoseconds since the epoch, as the application believes them to be.
    fn now_ns(&self) -> i128 {
        let real = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock before 1970")
            .as_nanos() as i128;
        real + self.offset_ns
    }

    fn step(&mut self, d: Duration, forward: bool) {
        let n = d.as_nanos() as i128;
        self.offset_ns += if forward { n } else { -n };
    }
}

fn burn(d: Duration) {
    let end = Instant::now() + d;
    while Instant::now() < end {
        std::hint::spin_loop();
    }
}

fn span_comparison(clock: &mut AppClock) -> (Vec<f64>, Vec<f64>) {
    let (mut wall, mut mono) = (Vec::with_capacity(SPANS), Vec::with_capacity(SPANS));
    // Fixed indices rather than a timer thread: a timer racing an 80ms loop is
    // how you get a run where the step lands between spans and the experiment
    // silently proves nothing. The README lists that outcome under "what would
    // mean the experiment is broken".
    let (step_back_at, step_fwd_at) = (SPANS / 3, 2 * SPANS / 3);

    for i in 0..SPANS {
        let w0 = clock.now_ns();
        let m0 = Instant::now();
        burn(SPAN_WORK);
        if i == step_back_at {
            clock.step(STEP_BACK, false);
        } else if i == step_fwd_at {
            clock.step(STEP_BACK, true);
        }
        let w1 = clock.now_ns();
        wall.push((w1 - w0) as f64 / 1e6);
        mono.push(m0.elapsed().as_nanos() as f64 / 1e6);
    }
    (wall, mono)
}

fn pct(v: &[f64], q: f64) -> f64 {
    let mut s = v.to_vec();
    s.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let i = ((q * s.len() as f64 + 0.5).round() as usize).saturating_sub(1);
    s[i.min(s.len() - 1)]
}

fn min_max(v: &[f64]) -> (f64, f64) {
    v.iter()
        .fold((f64::MAX, f64::MIN), |(lo, hi), &x| (lo.min(x), hi.max(x)))
}

fn span_report(wall: &[f64], mono: &[f64]) -> usize {
    println!();
    println!("------------------------------------------------------------------------------");
    println!(
        "2. {} identical spans, timed twice, with a -{}s step and a +{}s step",
        SPANS,
        STEP_BACK.as_secs(),
        STEP_BACK.as_secs()
    );
    println!("   landing INSIDE two of them");
    println!("------------------------------------------------------------------------------");
    println!(
        "  {:<30}{:>10}{:>12}{:>14}{:>14}{:>10}",
        "clock", "p50", "p99", "max", "min", "negative"
    );
    let mut negatives = 0;
    for (name, v) in [("wall (app now_ns)", wall), ("monotonic (Instant)", mono)] {
        let neg = v.iter().filter(|x| **x < 0.0).count();
        if name.starts_with("wall") {
            negatives = neg;
        }
        let (lo, hi) = min_max(v);
        println!(
            "  {:<30}{:>10.3}{:>12.3}{:>14.1}{:>14.1}{:>10}",
            name,
            pct(v, 0.50),
            pct(v, 0.99),
            hi,
            lo,
            neg
        );
    }
    println!("  (milliseconds; 'negative' counts spans that finished before they started)");

    let hot = wall
        .iter()
        .enumerate()
        .max_by(|a, b| a.1.partial_cmp(b.1).unwrap())
        .map(|(i, _)| i)
        .unwrap();
    let lo_i = hot.saturating_sub(19);
    let hi_i = (hot + 21).min(wall.len());
    let (wlo, whi) = min_max(wall);
    println!();
    println!(
        "  Two samples out of {} were touched: {:.0} ms and {:.0} ms, against a p50",
        SPANS, wlo, whi
    );
    println!(
        "  of {:.3} ms. Over all {} spans that is only the max -- one sample in {}",
        pct(wall, 0.50),
        SPANS,
        SPANS
    );
    println!("  cannot move a p99 by rank. But dashboards aggregate windows, not runs:");
    println!(
        "  over the {} spans around the step the wall-clock p99 is {:.1} ms against",
        hi_i - lo_i,
        pct(&wall[lo_i..hi_i], 0.99)
    );
    println!(
        "  a monotonic p99 of {:.3} ms. Only the clock differed.",
        pct(&mono[lo_i..hi_i], 0.99)
    );
    negatives
}

// -------------------------------------------------- 3. the Rust "footgun"

fn footguns() -> bool {
    println!();
    println!("------------------------------------------------------------------------------");
    println!("3. the footgun specific to this runtime, which is that there isn't one");
    println!("------------------------------------------------------------------------------");

    // (a) The bug every other language in this topic writes silently: subtract
    // two wall-clock readings and get a duration. Here you get a Result, and the
    // compiler makes you say what happens when the answer is "that was later".
    let earlier = SystemTime::now();
    burn(Duration::from_millis(2));
    let later = SystemTime::now();

    println!("  later.duration_since(earlier)   {:?}", later.duration_since(earlier));
    println!("  earlier.duration_since(later)   {:?}", earlier.duration_since(later));
    println!("  ^ the second one is the bug, and it is an Err rather than a negative");
    println!("    number. There is no way to reach a Duration from it by accident:");
    println!("    Duration is unsigned, so 'negative duration' is not representable.");

    let backwards = earlier.duration_since(later);
    let recovered = backwards
        .as_ref()
        .err()
        .map(|e| e.duration())
        .unwrap_or_default();
    println!();
    println!("  SystemTimeError::duration()     {:?}", recovered);
    println!("  ^ the error still carries the magnitude, so you CAN get the number --");
    println!("    but only by writing code that admits time went backwards. Compare");
    println!("    that with Go, where .UTC() moves you onto the wall clock in silence.");

    // (b) The three lines you cannot write. Uncomment any of them and the build
    // fails with the error quoted beside it -- verbatim from rustc 1.97.1 on this
    // machine. This is the section of the file to read if you take one thing away.
    //
    //   let d: Duration = later - earlier;
    //     error[E0308]: mismatched types
    //          |     let d: Duration = later - earlier;
    //          |                               ^^^^^^^ expected `Duration`, found `SystemTime`
    //     Worth reading twice: `SystemTime: Sub<Duration>` DOES exist, so the
    //     `-` resolves and then complains about the right-hand operand. The
    //     type that is missing is not `Sub`, it is a signed result -- Duration
    //     is unsigned, so "negative duration" has nowhere to live.
    //
    //   let t: SystemTime = Instant::now();
    //     error[E0308]: mismatched types
    //          |     let t: SystemTime = Instant::now();
    //          |            ----------   ^^^^^^^^^^^^^^ expected `SystemTime`, found `Instant`
    //
    //   let secs = Instant::now().duration_since(UNIX_EPOCH);
    //     error[E0308]: mismatched types
    //          |     let secs = Instant::now().duration_since(UNIX_EPOCH);
    //          |                              -------------- ^^^^^^^^^^ expected `Instant`, found `SystemTime`
    //     There is no Instant -> calendar path in std, deliberately.
    println!();
    println!("  Three lines that do not compile are commented at the bottom of this");
    println!("  function, each with the verbatim rustc error. Uncomment one and build");
    println!("  it -- reading the message from your own toolchain is worth more than");
    println!("  reading it here.");

    // (c) Instant does exactly one thing wrong-proof: it cannot be turned into a
    // date, so it cannot end up in a log line pretending to be one.
    println!();
    let i = Instant::now();
    println!("  Instant::now() Debug-prints as  {:?}", i);
    println!("  ^ an opaque platform value. Not a date, not comparable with another");
    println!("    process's Instant, and not serialisable to anything meaningful.");
    println!("    Every one of those restrictions is a bug this topic is about.");

    // (d) And the one place Rust is still on its own: Instant::now() on a machine
    // that suspended. std does not say whether Instant advances across sleep --
    // on Darwin it is CLOCK_UPTIME_RAW, which does NOT advance while suspended;
    // on Linux it is CLOCK_MONOTONIC, which also does not. CLOCK_BOOTTIME, which
    // does, is not reachable from std at all. If a lease depends on that
    // difference (Topic 7 does), you need the libc crate.
    println!();
    println!("  Not covered by the type system: whether Instant advances while the");
    println!("  machine is asleep. std does not say, it is CLOCK_UPTIME_RAW on Darwin");
    println!("  and CLOCK_MONOTONIC on Linux -- neither advances -- and CLOCK_BOOTTIME,");
    println!("  which does, is unreachable from std. Topic 7's leases care.");

    backwards.is_err()
}

fn main() {
    println!("==============================================================================");
    println!("Layer 4 Topic 3 -- Rust clock audit");
    println!("==============================================================================");
    // No rustc version string here on purpose: there is no stable way to read
    // the compiler's own version from inside the program, and printing a guess
    // would be exactly the kind of unmeasured number this layer exists to avoid.
    // `cargo --version` is one line away if you want it.
    println!(
        "  {} / {}",
        std::env::consts::OS,
        std::env::consts::ARCH
    );
    println!();

    inventory();
    let mut clock = AppClock::new();
    let (wall, mono) = span_comparison(&mut clock);
    let negatives = span_report(&wall, &mono);
    let reproduced = footguns();

    println!();
    println!("------------------------------------------------------------------------------");
    println!("4. one line for the record table in the README");
    println!("------------------------------------------------------------------------------");
    let base = Instant::now();
    let res = measure_resolution_ns(|| base.elapsed().as_nanos(), 20);
    println!(
        "  | Rust | std::time::Instant | {} ns | {} ({} negative wall-clock span{}) |",
        res,
        if reproduced { "yes -- as an Err, not a number" } else { "NO -- investigate" },
        negatives,
        if negatives == 1 { "" } else { "s" }
    );
    println!();
    println!("  The table in the README stays blank until you fill it in. This line is");
    println!("  the measurement, not the answer -- copy it across yourself.");
}
