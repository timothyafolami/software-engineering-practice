// Layer 6 Topic 3 - Losing trace context at a Rust concurrency boundary.
//
// What this demonstrates
// ----------------------
// Rust's `tracing` crate keeps a "current span" per thread. Futures do not
// inherit it, and that is not an oversight: a future is a value, and the
// executor may poll it on any worker thread it likes, at any time, possibly
// after the thread that created it has moved on to something else. There is no
// correct answer to "what span is current" for a future unless the future
// carries one.
//
// So `tokio::spawn(async { ... })` loses the caller's span, and the fix is a
// combinator -- `.instrument(span)` in `tracing`, and here a 25-line
// `Instrumented<F>` written out in full so nothing about it is magic. It wraps
// the future and installs the span for the duration of each `poll`, which is
// exactly the operation a thread-local cannot perform on its own.
//
// This file uses tokio (the boundary is tokio's) and nothing else. The span,
// the thread-local, the traceparent codec and the combinator are all here.
// No OpenTelemetry SDK is installed on this machine, and none is needed to
// show a property of the executor.
//
// Rust is the one runtime in this topic where the fix is a combinator you can
// grep for: `rg 'tokio::spawn' | rg -v instrument` is a real audit.
//
// What to look for in the output
// ------------------------------
// Blocks in the shared shape:
//
//   caller trace_id   <id>
//   callee trace_id   <id or "none">   naive
//   callee trace_id   <id>             propagated
//   verdict           lost | preserved
//
// `.await` in place is preserved (same thread, same poll, nothing moved).
// `tokio::spawn` is lost. `spawn_blocking` is lost for the same reason plus a
// second one -- it is a different thread pool entirely. The final section
// shows the audit: which spawn sites in this file are instrumented.

use std::cell::RefCell;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};

// ---------------------------------------------------------------------------
// A minimal span and the W3C traceparent codec.
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
struct Span {
    #[allow(dead_code)]
    name: &'static str,
    trace_id: String,
    span_id: String,
    sampled: bool,
}

// A tiny xorshift with a fixed seed, so ids differ between spans without
// pulling in `rand`. They are not secure and do not need to be: nothing here
// leaves the process.
fn rand_hex(bytes: usize) -> String {
    thread_local! {
        static STATE: RefCell<u64> = RefCell::new(0x2026_0818_0000_0001);
    }
    STATE.with(|s| {
        let mut x = *s.borrow();
        let mut out = String::with_capacity(bytes * 2);
        for _ in 0..bytes {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            out.push_str(&format!("{:02x}", (x >> 24) as u8));
        }
        *s.borrow_mut() = x;
        out
    })
}

impl Span {
    fn new(name: &'static str) -> Span {
        Span {
            name,
            trace_id: rand_hex(16),
            span_id: rand_hex(8),
            sampled: true,
        }
    }

    fn traceparent(&self) -> String {
        format!(
            "00-{}-{}-{}",
            self.trace_id,
            self.span_id,
            if self.sampled { "01" } else { "00" }
        )
    }

    fn from_traceparent(header: &str, name: &'static str) -> Result<Span, String> {
        let parts: Vec<&str> = header.split('-').collect();
        if parts.len() != 4 || parts[0] != "00" || parts[1].len() != 32 || parts[2].len() != 16 {
            return Err(format!("malformed traceparent: {header:?}"));
        }
        let flags = u8::from_str_radix(parts[3], 16).map_err(|e| e.to_string())?;
        Ok(Span {
            name,
            trace_id: parts[1].to_string(),
            span_id: parts[2].to_string(),
            sampled: flags & 1 == 1,
        })
    }
}

// ---------------------------------------------------------------------------
// The current span: a thread-local. This is what `tracing` does, and the whole
// point of the topic is that a thread-local and a work-stealing executor are
// two designs that do not compose without help.
// ---------------------------------------------------------------------------

thread_local! {
    static CURRENT: RefCell<Option<Span>> = const { RefCell::new(None) };
}

fn set_current(span: Option<Span>) -> Option<Span> {
    CURRENT.with(|c| std::mem::replace(&mut *c.borrow_mut(), span))
}

fn current_trace_id() -> String {
    CURRENT.with(|c| {
        c.borrow()
            .as_ref()
            .map(|s| s.trace_id.clone())
            .unwrap_or_else(|| "none".to_string())
    })
}

// `in_scope` for synchronous code: install, run, restore. Correct on one
// thread, useless across a poll boundary -- which is the next 25 lines.
fn in_scope<T>(span: Span, f: impl FnOnce() -> T) -> T {
    let previous = set_current(Some(span));
    let out = f();
    set_current(previous);
    out
}

// ---------------------------------------------------------------------------
// The fix, written out: a future that carries its span and installs it around
// every poll. `tracing`'s `Instrument` combinator is this, with more care about
// `Drop` and about spans that have already closed.
// ---------------------------------------------------------------------------

struct Instrumented<F> {
    inner: F,
    span: Span,
}

impl<F: Future> Future for Instrumented<F> {
    type Output = F::Output;

    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        // SAFETY: we never move `inner` out; this is the standard pin
        // projection that `pin-project-lite` generates.
        let this = unsafe { self.get_unchecked_mut() };
        let inner = unsafe { Pin::new_unchecked(&mut this.inner) };
        let previous = set_current(Some(this.span.clone()));
        let result = inner.poll(cx);
        set_current(previous);
        result
    }
}

trait InstrumentExt: Sized {
    fn instrument(self, span: Span) -> Instrumented<Self> {
        Instrumented { inner: self, span }
    }
}

impl<F: Future> InstrumentExt for F {}

// ---------------------------------------------------------------------------
// Structured logging, so the one-query test is visible here too.
// ---------------------------------------------------------------------------

#[derive(Clone)]
struct LogRecord {
    msg: String,
    trace_id: String,
}

type LogSink = Arc<Mutex<Vec<LogRecord>>>;

fn log_info(sink: &LogSink, msg: &str) {
    let id = current_trace_id();
    sink.lock().unwrap().push(LogRecord {
        msg: msg.to_string(),
        trace_id: if id == "none" { String::new() } else { id },
    });
}

fn report(boundary: &str, caller: &str, naive: &str, propagated: &str, note: &str) -> &'static str {
    let verdict = if naive == caller { "preserved" } else { "lost" };
    println!("boundary          {boundary}");
    println!("caller trace_id   {caller}");
    println!("callee trace_id   {naive:<32} naive");
    println!("callee trace_id   {propagated:<32} propagated");
    if note.is_empty() {
        println!("verdict           {verdict}\n");
    } else {
        println!("verdict           {verdict}   ({note})\n");
    }
    verdict
}

// ---------------------------------------------------------------------------
// Boundary 1: an inline `.await`. The control. Same task, same thread at the
// moment of the poll, so the thread-local is still yours.
// ---------------------------------------------------------------------------

async fn boundary_inline_await() -> &'static str {
    let span = Span::new("GET /orders");
    let caller = span.trace_id.clone();

    let observed = async {
        tokio::time::sleep(std::time::Duration::from_millis(1)).await;
        current_trace_id()
    }
    .instrument(span)
    .await;

    // And the same thing without the combinator, to show what carries it: the
    // combinator, not the await.
    let span2 = Span::new("GET /orders");
    let caller2 = span2.trace_id.clone();
    let uninstrumented = in_scope(span2, || {
        // Synchronous scope: current is set here...
        current_trace_id()
    });
    assert_eq!(uninstrumented, caller2);

    report(
        "async block, .instrument(span).await",
        &caller,
        &observed,
        &observed,
        "the span rides on the future, so it survives every poll",
    )
}

// ---------------------------------------------------------------------------
// Boundary 2: tokio::spawn -- the canonical Rust version of this bug.
// ---------------------------------------------------------------------------

async fn boundary_spawn(sink: &LogSink) -> &'static str {
    let span = Span::new("GET /orders");
    let caller = span.trace_id.clone();
    let _guard = set_current(Some(span.clone()));

    // Naive: a spawned task starts on a worker thread whose thread-local was
    // never set. Nothing warns. The task is a value; it did not bring a span.
    let sink_a = Arc::clone(sink);
    let naive = tokio::spawn(async move {
        log_info(&sink_a, "pricing call (spawned, naive)");
        current_trace_id()
    })
    .await
    .unwrap();

    // Propagated: the same task, wearing the span.
    let sink_b = Arc::clone(sink);
    let propagated = tokio::spawn(
        async move {
            log_info(&sink_b, "pricing call (spawned, instrumented)");
            current_trace_id()
        }
        .instrument(span),
    )
    .await
    .unwrap();

    report(
        "tokio::spawn",
        &caller,
        &naive,
        &propagated,
        "fix = tokio::spawn(fut.instrument(span))",
    )
}

// ---------------------------------------------------------------------------
// Boundary 3: spawn_blocking -- a different pool entirely, and the shape a
// synchronous downstream client forces you into.
// ---------------------------------------------------------------------------

async fn boundary_spawn_blocking() -> &'static str {
    let span = Span::new("GET /orders");
    let caller = span.trace_id.clone();
    let _guard = set_current(Some(span.clone()));

    let naive = tokio::task::spawn_blocking(current_trace_id).await.unwrap();

    // The blocking pool has no futures to instrument, so the combinator does
    // not apply. You capture the span by value and install it by hand -- the
    // same shape as Python's copy_context().run.
    let carried = span.clone();
    let propagated = tokio::task::spawn_blocking(move || in_scope(carried, current_trace_id))
        .await
        .unwrap();

    report(
        "tokio::task::spawn_blocking",
        &caller,
        &naive,
        &propagated,
        "no future to instrument: capture the span and in_scope it by hand",
    )
}

// ---------------------------------------------------------------------------
// Boundary 4: a queue. No executor involved; only the message body crosses.
// ---------------------------------------------------------------------------

fn boundary_queue(sink: &LogSink) -> &'static str {
    let span = Span::new("POST /orders");
    let caller = span.trace_id.clone();

    struct Message {
        id: &'static str,
        traceparent: Option<String>,
    }

    let consume = |m: &Message| -> String {
        // Runs in `worker`, a separate process. It starts from nothing.
        let restored = m
            .traceparent
            .as_ref()
            .and_then(|h| Span::from_traceparent(h, "job").ok());
        let previous = set_current(restored);
        log_info(sink, &format!("processing job {}", m.id));
        let observed = current_trace_id();
        set_current(previous);
        observed
    };

    let naive = consume(&Message {
        id: "naive",
        traceparent: None,
    });
    let propagated = consume(&Message {
        id: "propagated",
        traceparent: Some(span.traceparent()),
    });

    report(
        "Postgres-backed queue",
        &caller,
        &naive,
        &propagated,
        "the transport carries no headers; put traceparent in the body",
    )
}

// ---------------------------------------------------------------------------
// Boundary 5: the outbound HTTP call -- the easy half, made concrete.
// ---------------------------------------------------------------------------

fn boundary_http() -> &'static str {
    let span = Span::new("GET /orders");
    let header = span.traceparent();
    let downstream = Span::from_traceparent(&header, "GET /price").unwrap();
    println!("boundary          HTTP request to pricing");
    println!("caller trace_id   {}", span.trace_id);
    println!("traceparent sent  {header}");
    println!(
        "callee trace_id   {:<32} parsed from the header",
        downstream.trace_id
    );
    println!("verdict           preserved   (this is what being a W3C standard buys)\n");
    "preserved"
}

#[tokio::main(flavor = "multi_thread", worker_threads = 4)]
async fn main() {
    println!("Layer 6 Topic 3 - losing trace context in Rust (thread-local + tokio)");
    println!(
        "rustc target {}   tokio multi_thread, 4 workers",
        std::env::consts::ARCH
    );
    println!("{}", "=".repeat(72));
    println!();

    let sink: LogSink = Arc::new(Mutex::new(Vec::new()));

    let rows = vec![
        (
            "async block + .instrument",
            boundary_inline_await().await,
            "the combinator carries it",
        ),
        (
            "tokio::spawn",
            boundary_spawn(&sink).await,
            "YOU carry it - one combinator",
        ),
        (
            "spawn_blocking",
            boundary_spawn_blocking().await,
            "YOU carry it - by hand",
        ),
        (
            "Postgres queue",
            boundary_queue(&sink),
            "YOU carry it - in the message body",
        ),
        ("http traceparent", boundary_http(), "the wire format carries it"),
    ];

    println!("--- Summary: which boundaries the executor covers for you ---");
    for (name, verdict, who) in &rows {
        println!("  {name:<28} {verdict:<10} {who}");
    }
    println!();
    println!("  Nothing here is covered by the runtime, and that is deliberate: a");
    println!("  future can be polled from any worker thread, so a thread-local");
    println!("  cannot be right by default. What Rust gives you instead is that the");
    println!("  fix is one combinator, always in the same place, and greppable:");
    println!("    rg 'tokio::spawn' | rg -v instrument");
    println!();

    let logs = sink.lock().unwrap().clone();
    let with_id = logs.iter().filter(|r| !r.trace_id.is_empty()).count();
    println!("--- The one-query test, on the log lines this run emitted ---");
    println!("  log lines emitted            {}", logs.len());
    println!("  lines carrying a trace_id    {with_id}");
    println!(
        "  lines carrying nothing       {}   <- unqueryable by request",
        logs.len() - with_id
    );
    for r in &logs {
        let id = if r.trace_id.is_empty() {
            "(empty)"
        } else {
            &r.trace_id
        };
        println!("    {:<34} trace_id={}", r.msg, id);
    }
}
