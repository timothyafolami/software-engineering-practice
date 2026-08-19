// Layer 10 - Topic 2: does hanging up actually free the KV blocks? (Rust)
//
// What this demonstrates
//     The same experiment as the Python, Node and Go versions, in the
//     runtime where cancellation is not an API at all -- it is dropping the
//     future. A stub model server streams 40 tokens at 100ms each while
//     watching for its caller to leave; a gateway sits in front with two
//     handlers; a client hangs up after 500ms against each.
//
//       /naive       awaits the forwarding future to completion. Nothing
//                    is watching the client socket, so nothing ever drops
//                    anything, and the upstream generation finishes for a
//                    response written into a closed socket.
//       /cancelling  `tokio::select!` between the forwarding future and a
//                    read on the client socket. When the client closes,
//                    the read completes with 0 bytes, select! wins that
//                    branch and DROPS the other one. Dropping the future
//                    drops the TcpStream it owned, which closes the
//                    upstream connection. There is no cancel() call in
//                    this file, because there is nothing to call.
//
// What to look for
//     - No cancellation token, no signal, no listener. The strongest
//       guarantee of the six runtimes, and the one needing the least
//       discipline: you cannot forget to release a resource whose owner
//       was destroyed.
//     - The matching hazard, which this file is small enough to make
//       obvious: the drop happens at whatever await point the future was
//       parked on. If the forwarding future had written half a row to a
//       database, that half stays written. "Cancellation is free" is true
//       of memory and false of external state.
//
// One dependency (tokio), declared in Cargo.toml. Binds 127.0.0.1 only.
// Runs with no arguments:
//     cargo run --release --manifest-path rust/cancel_propagation/Cargo.toml

use std::sync::Mutex;
use std::time::{Duration, Instant};

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::time::sleep;

const TOKENS: usize = 40;
const TOKEN_INTERVAL: Duration = Duration::from_millis(100); // 4.0s of "decode"
const CLIENT_HANGS_UP_AFTER: Duration = Duration::from_millis(500);

#[derive(Clone, Copy)]
struct Observation {
    aborted: bool,
    tokens: usize,
    seconds: f64,
}

static LEDGER: Mutex<Vec<Observation>> = Mutex::new(Vec::new());

/// The stub model server: stream tokens, and notice when the caller leaves.
/// A read that completes with 0 bytes is EOF, and EOF is the only signal a
/// real engine gets that a sequence's KV blocks can go back on the free list.
async fn upstream_connection(mut sock: TcpStream) {
    let mut head = [0u8; 2048];
    let _ = sock.read(&mut head).await;

    let (mut rd, mut wr) = sock.split();
    if wr
        .write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
        .await
        .is_err()
    {
        return;
    }

    let start = Instant::now();
    let mut sent = 0usize;
    let mut aborted = false;
    let mut scratch = [0u8; 1024];

    for i in 0..TOKENS {
        tokio::select! {
            n = rd.read(&mut scratch) => {
                if matches!(n, Ok(0) | Err(_)) {
                    aborted = true;
                    break;
                }
            }
            _ = sleep(TOKEN_INTERVAL) => {}
        }
        if wr
            .write_all(format!("data: token {i}\n\n").as_bytes())
            .await
            .is_err()
        {
            aborted = true;
            break;
        }
        sent = i + 1;
    }

    LEDGER.lock().unwrap().push(Observation {
        aborted,
        tokens: sent,
        seconds: start.elapsed().as_secs_f64(),
    });
}

/// Connect upstream, forward every byte to the client. Owns the upstream
/// TcpStream, so dropping this future closes that connection -- which is
/// the entire mechanism the /cancelling handler relies on.
async fn forward(upstream_port: u16, client_wr: &mut (impl AsyncWriteExt + Unpin)) {
    let Ok(mut up) = TcpStream::connect(("127.0.0.1", upstream_port)).await else {
        return;
    };
    if up
        .write_all(b"POST /completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n")
        .await
        .is_err()
    {
        return;
    }
    let mut buf = [0u8; 4096];
    loop {
        match up.read(&mut buf).await {
            Ok(0) | Err(_) => return,
            Ok(n) => {
                if client_wr.write_all(&buf[..n]).await.is_err() {
                    return;
                }
            }
        }
    }
}

async fn gateway_connection(mut sock: TcpStream, upstream_port: u16) {
    let mut head = [0u8; 2048];
    let n = match sock.read(&mut head).await {
        Ok(n) if n > 0 => n,
        _ => return,
    };
    let cancelling = String::from_utf8_lossy(&head[..n]).contains("/cancelling");

    let (mut client_rd, mut client_wr) = sock.split();

    if !cancelling {
        // Nothing is reading client_rd, so the client's FIN is never
        // observed and this future runs to completion.
        //
        // Buffering into a Vec first, rather than streaming, is deliberate:
        // it removes a second safety net. If this forwarded byte by byte,
        // the first write to a departed client would fail, `forward` would
        // return, the upstream TcpStream would drop, and the upstream would
        // be torn down that way even with no select! in sight. That net is
        // real and worth knowing about -- but it only fires once the
        // gateway has something to write, which for a slow first token can
        // be seconds after the client left.
        let mut sink = Vec::new();
        forward(upstream_port, &mut sink).await;
        let _ = client_wr.write_all(&sink).await;
        return;
    }

    let mut scratch = [0u8; 1024];
    tokio::select! {
        _ = forward(upstream_port, &mut client_wr) => {}
        // Ok(0) is the client's FIN. Winning this branch drops the other
        // future, and with it the upstream TcpStream. That drop is the fix.
        _ = client_rd.read(&mut scratch) => {}
    }
}

/// A raw socket, on purpose: a client library's timeout raises in your code
/// without necessarily closing the TCP connection, so the server would see
/// nothing and the experiment would measure something else entirely.
async fn hang_up_on(gateway_port: u16, path: &str) {
    let Ok(mut sock) = TcpStream::connect(("127.0.0.1", gateway_port)).await else {
        return;
    };
    let req = format!("POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n");
    let _ = sock.write_all(req.as_bytes()).await;
    let mut buf = [0u8; 4096];
    let _ = tokio::time::timeout(CLIENT_HANGS_UP_AFTER, async {
        while let Ok(n) = sock.read(&mut buf).await {
            if n == 0 {
                break;
            }
        }
    })
    .await;
    drop(sock); // the hang-up
}

#[tokio::main]
async fn main() -> std::io::Result<()> {
    let upstream_ln = TcpListener::bind("127.0.0.1:0").await?;
    let upstream_port = upstream_ln.local_addr()?.port();
    tokio::spawn(async move {
        while let Ok((sock, _)) = upstream_ln.accept().await {
            tokio::spawn(upstream_connection(sock));
        }
    });

    let gateway_ln = TcpListener::bind("127.0.0.1:0").await?;
    let gateway_port = gateway_ln.local_addr()?.port();
    tokio::spawn(async move {
        while let Ok((sock, _)) = gateway_ln.accept().await {
            tokio::spawn(gateway_connection(sock, upstream_port));
        }
    });

    println!("Rust / tokio - cancellation on client disconnect");
    println!(
        "  upstream streams {TOKENS} tokens x {}ms = {:.1}s of decode",
        TOKEN_INTERVAL.as_millis(),
        TOKENS as f64 * TOKEN_INTERVAL.as_secs_f64()
    );
    println!(
        "  client hangs up after {:.1}s\n",
        CLIENT_HANGS_UP_AFTER.as_secs_f64()
    );
    println!(
        "  {:<14} {:<16} {:>14} {:>13} {:>8}",
        "handler", "upstream saw", "tokens decoded", "upstream ran", "wasted"
    );
    println!("  {}", "-".repeat(70));

    for path in ["/naive", "/cancelling"] {
        LEDGER.lock().unwrap().clear();
        hang_up_on(gateway_port, path).await;

        let deadline = Instant::now() + TOKEN_INTERVAL * (TOKENS as u32) + Duration::from_secs(1);
        let obs = loop {
            if let Some(o) = LEDGER.lock().unwrap().first().copied() {
                break o;
            }
            if Instant::now() > deadline {
                break Observation { aborted: false, tokens: 0, seconds: f64::NAN };
            }
            sleep(Duration::from_millis(50)).await;
        };

        let wasted = (obs.seconds - CLIENT_HANGS_UP_AFTER.as_secs_f64()).max(0.0);
        println!(
            "  {:<14} {:<16} {:>14} {:>12.2}s {:>7.2}s",
            path,
            if obs.aborted { "cancelled" } else { "nothing" },
            obs.tokens,
            obs.seconds,
            wasted
        );
    }

    println!();
    println!("  'wasted' is decode time spent on a response nobody read. On a");
    println!("  loaded server those KV blocks stayed allocated the whole time,");
    println!("  so the scheduler could not admit somebody who was still waiting.");
    println!();
    println!("  There is no cancel() anywhere in this file. select! dropped the");
    println!("  losing future, the drop closed the socket, and the socket close");
    println!("  is what the engine saw.");
    Ok(())
}
