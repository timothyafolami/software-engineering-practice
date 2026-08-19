// Layer 1 - Blocking vs non-blocking IO, side by side in one binary.
// std::net::TcpStream is genuinely blocking: the OS thread calling read()
// is parked by the kernel and does nothing else until data arrives. Rust's
// std makes no attempt to hide that. tokio::net::TcpStream, in contrast,
// registers the socket with the OS's readiness API (epoll via `mio`) and
// suspends only the async task, freeing its underlying worker thread to
// run other tasks in the meantime -- the same fundamental mechanism as
// Python's asyncio and Go's netpoller, just with the cost/benefit tradeoff
// made explicit by which type you import.
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::thread;
use std::time::{Duration, Instant};

const RESPONSE_DELAY: Duration = Duration::from_millis(100);
const N: usize = 20;

fn start_server() -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    thread::spawn(move || {
        for stream in listener.incoming() {
            if let Ok(mut stream) = stream {
                thread::spawn(move || {
                    let mut buf = [0u8; 1024];
                    let _ = stream.read(&mut buf);
                    thread::sleep(RESPONSE_DELAY);
                    let _ = stream.write_all(b"ok");
                });
            }
        }
    });
    addr
}

fn blocking_request(addr: SocketAddr) {
    let mut stream = TcpStream::connect(addr).unwrap();
    stream.write_all(b"ping").unwrap();
    let mut buf = [0u8; 1024];
    let _ = stream.read(&mut buf);
}

fn bench_serial(addr: SocketAddr) -> f64 {
    let start = Instant::now();
    for _ in 0..N {
        blocking_request(addr);
    }
    start.elapsed().as_secs_f64()
}

async fn async_request(addr: SocketAddr) {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let mut stream = tokio::net::TcpStream::connect(addr).await.unwrap();
    stream.write_all(b"ping").await.unwrap();
    let mut buf = [0u8; 1024];
    let _ = stream.read(&mut buf).await;
}

async fn bench_concurrent(addr: SocketAddr) -> f64 {
    let start = Instant::now();
    let handles: Vec<_> = (0..N).map(|_| tokio::spawn(async_request(addr))).collect();
    for h in handles {
        h.await.unwrap();
    }
    start.elapsed().as_secs_f64()
}

#[tokio::main]
async fn main() {
    let addr = start_server();
    let t_serial = bench_serial(addr);
    let t_concurrent = bench_concurrent(addr).await;
    println!("N={} requests, {:?} server delay each", N, RESPONSE_DELAY);
    println!(
        "serial (std::net, blocking):  {:.3}s  (~{:.0}ms/req)",
        t_serial,
        t_serial / N as f64 * 1000.0
    );
    println!("concurrent (tokio tasks):     {:.3}s", t_concurrent);
}
