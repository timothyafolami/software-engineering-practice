// Layer 2 · Topic 3 - Rust: a timeout is one future racing another, and the
// loser is DROPPED.
//
// That single sentence is the sharpest definition of a timeout in this lab,
// and it has a consequence the other five runtimes do not force you to think
// about. Dropping a future cancels it at whatever await point it happened to
// be parked on -- which may be the middle of writing a request. The bytes
// already on the wire stay on the wire. The peer has no idea a request was
// abandoned. If you then reuse that connection, your next response is not the
// answer to your next request.
//
// That is cancellation safety, and nothing in the type system will warn you.
//
// The HTTP here is written by hand over tokio's TcpStream so that the moment
// of cancellation is visible; a client library would hide it and quietly
// close the socket for you. Both halves are the lesson: what the library does
// for you, and what it is protecting you from.
//
// Three phases:
//   A. A deadline budget spent down three sequential hops.
//   B. What firing the timeout does to the request already in flight.
//   C. Cancellation mid-write, then reuse of the same connection -- and the
//      response that comes back belongs to the previous request.
//
// What to look for in the output:
//   - phase A: hop 3 is never started, because its answer would arrive too
//     late to use
//   - phase B: the server's FINISHED counter rises for the request the client
//     abandoned. Cancellation is local to your task
//   - phase C: "asked for /marker, got path=/fast". That mismatch is the
//     whole argument for never returning a cancelled connection to a pool
//
// Run: cargo run --release
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::time::{sleep, timeout};

const SLOW: Duration = Duration::from_millis(400); // how long the server holds /slow
const OUTER_BUDGET: Duration = Duration::from_millis(900);
const RESERVE: Duration = Duration::from_millis(100);
const PER_HOP_CAP: Duration = Duration::from_millis(500);

#[derive(Default)]
struct Counters {
    accepted: AtomicU64,
    started: AtomicU64,
    finished: AtomicU64,
}

/// A minimal HTTP/1.1 server: read until the end of the headers, reply with
/// the request path in the body. Small enough to be obviously correct, and
/// deliberately keep-alive so phase C can reuse the connection.
async fn serve(listener: TcpListener, c: Arc<Counters>) {
    loop {
        let Ok((mut sock, _)) = listener.accept().await else { return };
        c.accepted.fetch_add(1, Ordering::Relaxed);
        let c = c.clone();
        tokio::spawn(async move {
            let mut buf = vec![0u8; 4096];
            let mut have = 0usize;
            loop {
                // Find the end of a request's headers in whatever we have.
                let end = find_headers_end(&buf[..have]);
                let Some(end) = end else {
                    match sock.read(&mut buf[have..]).await {
                        Ok(0) | Err(_) => return,
                        Ok(n) => {
                            have += n;
                            continue;
                        }
                    }
                };

                let head = String::from_utf8_lossy(&buf[..end]).to_string();
                let path = head
                    .lines()
                    .next()
                    .and_then(|l| l.split_whitespace().nth(1))
                    .unwrap_or("/")
                    .to_string();

                buf.copy_within(end..have, 0);
                have -= end;

                if path.starts_with("/slow") {
                    c.started.fetch_add(1, Ordering::Relaxed);
                    // No cancellation awareness here, exactly like every
                    // handler you have ever deployed.
                    sleep(SLOW).await;
                    c.finished.fetch_add(1, Ordering::Relaxed);
                }

                let body = format!("path={path}");
                let resp = format!(
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: keep-alive\r\n\r\n{}",
                    body.len(),
                    body
                );
                if sock.write_all(resp.as_bytes()).await.is_err() {
                    return;
                }
            }
        });
    }
}

fn find_headers_end(b: &[u8]) -> Option<usize> {
    b.windows(4).position(|w| w == b"\r\n\r\n").map(|i| i + 4)
}

/// One request/response on a fresh connection.
async fn call(addr: &str, path: &str) -> std::io::Result<String> {
    let mut sock = TcpStream::connect(addr).await?;
    write_request(&mut sock, path).await?;
    read_response(&mut sock).await
}

async fn write_request(sock: &mut TcpStream, path: &str) -> std::io::Result<()> {
    let req = format!("GET {path} HTTP/1.1\r\nHost: lab\r\n\r\n");
    sock.write_all(req.as_bytes()).await
}

async fn read_response(sock: &mut TcpStream) -> std::io::Result<String> {
    let mut buf = vec![0u8; 2048];
    let n = sock.read(&mut buf).await?;
    Ok(String::from_utf8_lossy(&buf[..n]).to_string())
}

fn body_of(resp: &str) -> &str {
    resp.split("\r\n\r\n").nth(1).unwrap_or("")
}

/// The pattern: an absolute instant, a reserve never spent upstream, a cap.
struct Deadline {
    at: Instant,
    reserve: Duration,
}

impl Deadline {
    fn new(total: Duration, reserve: Duration) -> Self {
        Self { at: Instant::now() + total, reserve }
    }
    fn remaining(&self) -> Duration {
        self.at.saturating_duration_since(Instant::now())
    }
    /// Returns None when there is not enough left to be worth starting a call.
    fn for_call(&self, cap: Duration) -> Option<Duration> {
        let left = self.remaining().checked_sub(self.reserve)?;
        if left.is_zero() { None } else { Some(left.min(cap)) }
    }
}

async fn phase_a(addr: &str) {
    println!("A. A budget, spent down three sequential hops");
    println!("    promised to our caller     {:5} ms", OUTER_BUDGET.as_millis());
    println!("    reserved for our own work  {:5} ms", RESERVE.as_millis());
    println!("    each hop's flat default    {:5} ms  <- what a flat config would use\n", PER_HOP_CAP.as_millis());

    let dl = Deadline::new(OUTER_BUDGET, RESERVE);
    let t0 = Instant::now();

    for hop in 1..=3 {
        let Some(slice) = dl.for_call(PER_HOP_CAP) else {
            println!("    hop {hop}  slice      0 ms  -> NOT STARTED: its answer would arrive after");
            println!("                              our caller has stopped waiting. Failing now is");
            println!("                              correct, and it is the line people skip.");
            break;
        };
        let outcome = match timeout(slice, call(addr, "/slow")).await {
            Ok(Ok(_)) => "ok".to_string(),
            Ok(Err(e)) => format!("io error: {e}"),
            Err(_) => "Elapsed (future dropped)".to_string(),
        };
        println!(
            "    hop {hop}  slice {:6} ms  -> {outcome}  ({} ms elapsed, {} ms left)",
            slice.as_millis(),
            t0.elapsed().as_millis(),
            dl.remaining().as_millis()
        );
    }

    println!(
        "\n    total spent {} ms against a {} ms promise, {} ms left to answer",
        t0.elapsed().as_millis(),
        OUTER_BUDGET.as_millis(),
        dl.remaining().as_millis()
    );
}

async fn phase_b(addr: &str, c: &Counters) {
    println!("\nB. What a fired timeout does to the request already in flight");
    sleep(SLOW + Duration::from_millis(100)).await; // let phase A's abandoned hop land
    let before = c.finished.load(Ordering::Relaxed);

    let t0 = Instant::now();
    let res = timeout(Duration::from_millis(100), call(addr, "/slow")).await;
    println!("    client gave up after   {} ms", t0.elapsed().as_millis());
    println!("    result                 {:?}   <- tokio::time::error::Elapsed", res.is_err());

    sleep(SLOW + Duration::from_millis(200)).await;
    println!(
        "    server FINISHED this request anyway: {} -> {}",
        before,
        c.finished.load(Ordering::Relaxed)
    );
    println!("    The future was dropped at its await point. Nothing was sent to the");
    println!("    server saying so -- the socket closing is the only hint it gets, and it");
    println!("    was not reading. Your timeout protects you, not your dependency.");
}

async fn phase_c(addr: &str) {
    println!("\nC. Cancellation mid-write, and the connection you must not reuse");

    let mut sock = TcpStream::connect(addr).await.expect("connect");

    // A request written in two pieces with a pause in the middle. This is not
    // contrived: any chunked body, any large POST, any TLS record boundary
    // gives you an await point in the middle of "write the request".
    let write_half = async {
        sock.write_all(b"GET /fast HTTP/1.1\r\nHost: lab\r\n").await?;
        sleep(Duration::from_millis(300)).await; // the await point that gets cancelled
        sock.write_all(b"\r\n").await?;
        read_response(&mut sock).await
    };

    let outcome = timeout(Duration::from_millis(100), write_half).await;
    println!("    timed out mid-write:   {}", outcome.is_err());
    println!("    bytes already sent:    \"GET /fast HTTP/1.1\\r\\nHost: lab\\r\\n\"  (no blank line)");
    println!("    The peer is not waiting for a timeout. It is waiting for the rest of a");
    println!("    request it believes is still coming.");

    // Now do exactly what a connection pool would do: hand this socket to the
    // next caller, who wants something entirely different.
    write_request(&mut sock, "/marker").await.expect("write");
    let resp = read_response(&mut sock).await.expect("read");
    let body = body_of(&resp);

    println!("\n    reusing that same connection, we ask for   /marker");
    println!("    the response body says                     {body}");
    if body != "path=/marker" {
        println!("    A RESPONSE FOR A DIFFERENT REQUEST. The server glued our leftover");
        println!("    bytes to the new ones and answered the first request line it found.");
        println!("    Every response on this connection is now off by one, forever, and");
        println!("    nothing in the type system, the borrow checker or the error type");
        println!("    said a word. This is why every real client destroys a connection it");
        println!("    cancelled -- and why 'is this future cancellation-safe' is a design");
        println!("    question you have to answer yourself.");
    } else {
        println!("    The connection came back clean this time -- the write completed");
        println!("    before the timeout fired. Lower the timeout or raise the mid-write");
        println!("    sleep and run it again; the race is real, it just did not land.");
    }
}

#[tokio::main]
async fn main() {
    let c = Arc::new(Counters::default());
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let addr = listener.local_addr().unwrap().to_string();
    tokio::spawn(serve(listener, c.clone()));

    println!("{}", "=".repeat(78));
    println!("Rust: a timeout is a race, and the loser is dropped mid-flight");
    println!("{}", "=".repeat(78));
    println!("  server holds /slow for {} ms   reqwest has no default total timeout\n", SLOW.as_millis());

    phase_a(&addr).await;
    phase_b(&addr, &c).await;
    phase_c(&addr).await;

    println!("\n  For this topic's table:");
    println!("    what a fired timeout does to the in-flight request:");
    println!("      drops the future at its current await point. Anything already written");
    println!("      to the socket stays written; the server runs to completion.");
    println!("    connection reused after?");
    println!("      only if the cancelled operation was cancellation-safe. It usually is");
    println!("      not, which is why libraries close the connection instead of asking.");
    println!("\n  connections accepted during this run: {}", c.accepted.load(Ordering::Relaxed));
}
