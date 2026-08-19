// Layer 6 Topic 1 - What one unit of telemetry costs the process emitting it.
//
// Why Rust: it is the only language in this lab where a disabled log line can
// cost literally nothing, and where you can prove that from inside the program.
// Every other runtime here pays *something* for a debug call that produces no
// output -- Python pays for the f-string, Node pays for the template literal,
// Go pays for boxing the variadic arguments, Java pays for the Object[] the
// varargs call allocates. Rust's `log!`-style macros take the expression rather
// than its value, so when the level check fails the expression is never
// evaluated, and with a const level the branch itself is folded away at compile
// time. That is the compile-time-enforcement point of this whole lab, applied
// to observability.
//
// It also sets the trap this lab exists to teach. If a row below reads 0 ns/op,
// the compiler deleted the work. That is the correct answer for a disabled
// compile-time-gated macro and the WRONG answer for anything else -- it is
// exactly the mistake Layer 1 shipped, where "0 lost updates" turned out to be
// the optimizer hoisting a loop. So every measured expression here is wrapped
// in std::hint::black_box, and the accumulated sink is printed at the end. If
// you edit this file, keep both.
//
// What this demonstrates
// ----------------------
//   1. counter add       - HashMap lookup on a bounded label key + increment
//   2. span record       - struct construction, timestamps, six attributes
//   3. log line (INFO)   - manual JSON formatting into a counting sink
//   4. debug, DISABLED, function call, argument built eagerly   <- the bug
//   5. debug, DISABLED, macro, argument never evaluated         <- the fix
//   6. debug, DISABLED, macro with a const level                <- free
//
// Rows 4, 5 and 6 all emit nothing. This is why every serious logging API in
// Rust, C, and C++ is a macro rather than a function.
//
// What to look for in the output
// ------------------------------
//   - row 4 vs row 5: the same source-level intent, one order of magnitude or
//     more apart, because one of them evaluated its argument.
//   - row 6: if it is 0, read the sink line and satisfy yourself the work was
//     genuinely elided rather than accidentally skipped.
//
// Run:  cargo run --release          (from this directory)

use std::collections::HashMap;
use std::fmt::Write as _;
use std::hint::black_box;
use std::time::Instant;

const ITERATIONS: u64 = 200_000;
const WARMUP: u64 = 20_000;

#[derive(Clone, Copy, PartialEq, PartialOrd)]
enum Level {
    Debug = 20,
    Info = 30,
}

// The compile-time level. Anything below this is removed by the optimizer
// because the comparison is const-foldable. This is what `log`'s
// max_level_info feature and `tracing`'s static max level do in production.
const COMPILE_TIME_LEVEL: Level = Level::Info;

struct CountingSink {
    bytes: usize,
    lines: usize,
}

impl CountingSink {
    fn write(&mut self, line: &str) {
        self.bytes += line.len();
        self.lines += 1;
    }
}

struct Logger {
    sink: CountingSink,
    level: Level,
}

impl Logger {
    fn enabled(&self, level: Level) -> bool {
        level >= self.level
    }
    fn log(&mut self, level: Level, line: &str) {
        if self.enabled(level) {
            self.sink.write(line);
        }
    }
}

// The macro form. `$arg` is an expression, not a value: if the guard is false
// the expression is never evaluated, so an expensive argument costs nothing.
macro_rules! debug_lazy {
    ($logger:expr, $arg:expr) => {
        if $logger.enabled(Level::Debug) {
            let line = $arg;
            $logger.log(Level::Debug, &line);
        }
    };
}

// The same, but gated on a const. The `if` folds at compile time and the whole
// body -- including the call to the expensive argument -- is removed.
macro_rules! debug_static {
    ($logger:expr, $arg:expr) => {
        if (Level::Debug as u8) >= (COMPILE_TIME_LEVEL as u8) {
            let line = $arg;
            $logger.log(Level::Debug, &line);
        }
    };
}

struct Span {
    name: &'static str,
    trace_id: &'static str,
    span_id: &'static str,
    attributes: Vec<(&'static str, String)>,
    start_ns: u128,
    end_ns: u128,
}

struct Row {
    label: &'static str,
    ns_per_op: f64,
}

fn bench(label: &'static str, mut f: impl FnMut()) -> Row {
    for _ in 0..WARMUP {
        f();
    }
    let start = Instant::now();
    for _ in 0..ITERATIONS {
        f();
    }
    let elapsed = start.elapsed();
    Row {
        label,
        ns_per_op: elapsed.as_nanos() as f64 / ITERATIONS as f64,
    }
}

// Stands in for the serde_json::to_string a real debug line calls on its way
// into the logger. Deliberately not inlinable-to-nothing: it allocates.
#[inline(never)]
fn expensive_argument(order_id: &str, customer_id: &str, discount: f64) -> String {
    let mut s = String::with_capacity(160);
    let _ = write!(
        s,
        "pricing payload={{\"order_id\":\"{}\",\"customer_id\":\"{}\",\"discount\":{},\
         \"items\":[{{\"sku\":\"SKU-1\",\"qty\":2}},{{\"sku\":\"SKU-7\",\"qty\":1}}]}}",
        order_id, customer_id, discount
    );
    s
}

fn main() {
    let mut sink: u128 = 0;
    let mut counter: HashMap<&'static str, u64> = HashMap::new();
    // The runtime level is derived from argv rather than written as a literal,
    // for a reason worth understanding: with a literal, LLVM proves the level
    // is Info, proves the Debug branch in `debug_lazy!` is dead, and deletes it
    // -- and then rows 5 and 6 both read 0 and the difference between a runtime
    // guard and a compile-time guard becomes invisible. Reading it from argv
    // makes the branch genuinely dynamic, so row 5 measures a real comparison.
    // Default with no arguments is Info, so `cargo run --release` is enough.
    let level = if std::env::args().any(|a| a == "--debug") {
        Level::Debug
    } else {
        Level::Info
    };
    let mut logger = Logger {
        sink: CountingSink { bytes: 0, lines: 0 },
        level, // debug is disabled -- the production config
    };

    let label_key = "GET|/orders/{id}|200";
    let epoch = Instant::now();

    let mut rows = Vec::new();

    rows.push(bench("counter.add (3 bounded labels)", || {
        *counter.entry(black_box(label_key)).or_insert(0) += 1;
    }));

    rows.push(bench("span create + end (6 attrs)", || {
        let span = Span {
            name: "GET /orders/{id}",
            trace_id: "4bf92f3577b34da6a3ce929d0e0e4736",
            span_id: "00f067aa0ba902b7",
            attributes: vec![
                ("http.request.method", "GET".to_string()),
                ("http.route", "/orders/{id}".to_string()),
                ("http.response.status_code", "200".to_string()),
                ("db.system.name", "postgresql".to_string()),
                ("customer.id", "cus_00194".to_string()),
                ("order.id", "ord_8f31c2".to_string()),
            ],
            start_ns: epoch.elapsed().as_nanos(),
            end_ns: 0,
        };
        let mut span = black_box(span);
        span.end_ns = epoch.elapsed().as_nanos();
        sink = sink.wrapping_add(span.end_ns - span.start_ns + span.attributes.len() as u128);
        black_box(span.name.len() + span.trace_id.len() + span.span_id.len());
    }));

    rows.push(bench("log INFO, one JSON line", || {
        let mut line = String::with_capacity(128);
        let _ = write!(
            line,
            "{{\"level\":\"info\",\"msg\":\"order priced\",\"order_id\":\"{}\",\
             \"customer_id\":\"{}\",\"duration_ms\":{}}}",
            "ord_8f31c2", "cus_00194", 12.4
        );
        logger.log(Level::Info, black_box(&line));
    }));

    rows.push(bench("log DEBUG (disabled), function, eager arg", || {
        // THE BUG, in the shape Rust makes possible only if you write the
        // logger as a function taking a String. The argument is evaluated at
        // the call site, exactly as in Python, Node, Go and Java.
        let line = expensive_argument("ord_8f31c2", "cus_00194", 0.15);
        logger.log(Level::Debug, black_box(&line));
    }));

    rows.push(bench("log DEBUG (disabled), macro, runtime level", || {
        // THE FIX. The macro takes the expression. Level is a runtime field,
        // so the branch stays, but the argument is never built.
        //
        // The black_box on the logger is not decoration: without it LLVM sees
        // that `logger.level` never changes inside the loop, hoists the
        // comparison out, and this row reads 0.0 -- a benchmark that measured
        // its own loop rather than the call. Row 6 is the row that is allowed
        // to read 0.0.
        let lg = black_box(&mut logger);
        debug_lazy!(
            lg,
            expensive_argument(black_box("ord_8f31c2"), "cus_00194", 0.15)
        );
    }));

    rows.push(bench("log DEBUG (disabled), macro, const level", || {
        // Compile-time gating: the branch is const-folded and the body,
        // including the call, is gone from the binary.
        debug_static!(
            logger,
            expensive_argument(black_box("ord_8f31c2"), "cus_00194", 0.15)
        );
    }));

    let bar = "=".repeat(74);
    println!("{}", bar);
    println!(
        "COST OF EMITTING ONE UNIT OF TELEMETRY   (rustc release, n={})",
        ITERATIONS
    );
    println!("{}", bar);
    println!("{:<46}{:>12}", "operation", "ns/op");
    for r in &rows {
        println!("{:<46}{:>12.1}", r.label, r.ns_per_op);
    }

    let eager = rows[3].ns_per_op;
    let lazy = rows[4].ns_per_op;
    let stat = rows[5].ns_per_op;
    println!("\nRows 4, 5 and 6 all emit nothing at all.");
    println!("  function, eager argument : {:>8.1} ns", eager);
    println!("  macro, runtime level     : {:>8.1} ns", lazy);
    println!("  macro, const level       : {:>8.1} ns", stat);
    println!(
        "The gap between rows 4 and 5 is {:.1} ns per call of pure waste, and it",
        eager - lazy
    );
    println!("is not a Rust fact -- it is the same waste Python, Node, Go and Java");
    println!("pay in this same file. What is a Rust fact is row 6.");
    println!("\nRow 5 pays for one comparison per call. Row 6 pays for nothing: the");
    println!("branch is const-folded and the call is not in the binary at all.");
    println!("Row 6 is the only row in this file allowed to read 0.0. If row 5 reads");
    println!("0.0 as well, the black_box guarding it was removed and LLVM hoisted a");
    println!("loop-invariant comparison out of the benchmark loop -- that is the");
    println!("experiment being broken, not Rust being fast, and it is the identical");
    println!("mistake Layer 1 shipped as a finding. Check the sink line below before");
    println!("believing any zero.");

    println!(
        "\nBytes written by the INFO logs: {} over {} lines ({:.0} B/line).",
        logger.sink.bytes,
        logger.sink.lines,
        logger.sink.bytes as f64 / logger.sink.lines.max(1) as f64
    );
    println!(
        "(sink={}, counter={}, printed so nothing above can be optimised away)",
        sink,
        counter.values().sum::<u64>()
    );
}
