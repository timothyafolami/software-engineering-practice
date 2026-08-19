// Layer 2 · Topic 1 - The connection with no client library in the way.
//
// Rust is here for a specific reason: it has no HTTP client in std, so
// there is no pool doing anything on your behalf, and "reuse the
// connection" stops being a configuration flag and becomes a visible
// property of the code -- a `TcpStream` you either keep in scope or drop.
// The borrow checker makes that lifetime impossible to lose track of,
// which is the compile-time version of the bug Python and Node hide at
// runtime: `httpx.AsyncClient()` inside a handler is a `TcpStream` dropped
// at the end of every request, and nothing in Python tells you.
//
// Both variants speak the same HTTP/1.1 to the same in-process server.
// COLD opens a fresh TcpStream per request (and sends `Connection: close`,
// which is what any client that does not pool effectively does). WARM
// keeps one stream and writes 200 requests down it.
//
// What to look for in the output: connections accepted by the server, and
// the per-request cost. On loopback the gap is small and honest -- there
// is no round trip to save. The connection count is the number that
// transfers to a real link.
//
// Run: cargo run --release

use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Instant;

const REQUESTS: usize = 200;
const BODY: &str = "{\"ok\":true}";

fn main() -> std::io::Result<()> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let addr = listener.local_addr()?;
    let accepted = Arc::new(AtomicUsize::new(0));

    {
        let accepted = Arc::clone(&accepted);
        thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(stream) = stream else { continue };
                // One increment per accept(2). A keep-alive connection
                // carrying 200 requests increments this exactly once.
                accepted.fetch_add(1, Ordering::Relaxed);
                thread::spawn(move || serve(stream));
            }
        });
    }

    println!("{}", "=".repeat(78));
    println!("Rust: the connection as a value you either keep or drop");
    println!("{}", "=".repeat(78));
    println!("  server 127.0.0.1:{}   {} requests\n", addr.port(), REQUESTS);

    let before = accepted.load(Ordering::Relaxed);
    let cold = drive_cold(addr.port())?;
    let cold_conns = accepted.load(Ordering::Relaxed) - before;
    report("COLD - a fresh TcpStream per request, dropped at the end", &cold, cold_conns);

    println!();
    let before = accepted.load(Ordering::Relaxed);
    let warm = drive_warm(addr.port())?;
    let warm_conns = accepted.load(Ordering::Relaxed) - before;
    report("WARM - one TcpStream held across all requests", &warm, warm_conns);

    println!();
    println!("  The Rust-specific observation:");
    println!("    In the COLD function the stream is created inside the loop body,");
    println!("    so it is dropped -- and the socket closed -- at the end of every");
    println!("    iteration. The compiler knows that. It is the same fact as");
    println!("    `async with httpx.AsyncClient()` inside a FastAPI handler, except");
    println!("    that here the lifetime is written down in the code rather than");
    println!("    buried in a library's pool. Nothing here prevents the bug; what");
    println!("    Rust removes is the ability to be UNSURE which one you wrote.");
    Ok(())
}

fn serve(mut stream: TcpStream) {
    let peer = match stream.try_clone() {
        Ok(clone) => clone,
        Err(_) => return,
    };
    let mut reader = BufReader::new(peer);
    loop {
        let mut close_requested = false;
        let mut saw_request_line = false;
        // Read one request's headers. No body: these are all GETs.
        loop {
            let mut line = String::new();
            match reader.read_line(&mut line) {
                Ok(0) => return, // peer closed
                Ok(_) => {}
                Err(_) => return,
            }
            if !saw_request_line {
                saw_request_line = true;
            }
            if line.to_ascii_lowercase().starts_with("connection: close") {
                close_requested = true;
            }
            if line == "\r\n" || line == "\n" {
                break;
            }
        }
        if !saw_request_line {
            return;
        }
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n{}\r\n{}",
            BODY.len(),
            if close_requested { "Connection: close\r\n" } else { "" },
            BODY
        );
        if stream.write_all(response.as_bytes()).is_err() {
            return;
        }
        if close_requested {
            return;
        }
    }
}

fn read_one_response(reader: &mut impl BufRead) -> std::io::Result<()> {
    let mut content_length = 0usize;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line)? == 0 {
            return Ok(());
        }
        let lowered = line.to_ascii_lowercase();
        if let Some(rest) = lowered.strip_prefix("content-length:") {
            content_length = rest.trim().parse().unwrap_or(0);
        }
        if line == "\r\n" || line == "\n" {
            break;
        }
    }
    let mut body = vec![0u8; content_length];
    reader.read_exact(&mut body)?;
    Ok(())
}

fn drive_cold(port: u16) -> std::io::Result<Vec<f64>> {
    let mut latencies = Vec::with_capacity(REQUESTS);
    for _ in 0..REQUESTS {
        let started = Instant::now();
        // Created here => dropped here. One connection, one request.
        let mut stream = TcpStream::connect(("127.0.0.1", port))?;
        stream.set_nodelay(true)?;
        write!(
            stream,
            "GET /thing HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
        )?;
        let mut reader = BufReader::new(stream.try_clone()?);
        read_one_response(&mut reader)?;
        latencies.push(started.elapsed().as_secs_f64() * 1000.0);
    }
    Ok(latencies)
}

fn drive_warm(port: u16) -> std::io::Result<Vec<f64>> {
    let mut latencies = Vec::with_capacity(REQUESTS);
    // Created ONCE, outside the loop. This is the whole fix, in one line of
    // scope placement.
    let mut stream = TcpStream::connect(("127.0.0.1", port))?;
    stream.set_nodelay(true)?;
    let mut reader = BufReader::new(stream.try_clone()?);
    for _ in 0..REQUESTS {
        let started = Instant::now();
        write!(stream, "GET /thing HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")?;
        read_one_response(&mut reader)?;
        latencies.push(started.elapsed().as_secs_f64() * 1000.0);
    }
    Ok(latencies)
}

fn report(label: &str, latencies: &[f64], connections: usize) {
    let mut sorted = latencies.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let at = |f: f64| sorted[((sorted.len() as f64 * f) as usize).min(sorted.len() - 1)];
    println!("  {label}");
    println!("    requests issued        {}", latencies.len());
    println!("    TCP connections opened {connections}");
    println!(
        "    requests per connection {:.1}",
        latencies.len() as f64 / connections.max(1) as f64
    );
    println!(
        "    latency p50 {:.3} ms   p95 {:.3} ms   p99 {:.3} ms",
        at(0.50),
        at(0.95),
        at(0.99)
    );
}
