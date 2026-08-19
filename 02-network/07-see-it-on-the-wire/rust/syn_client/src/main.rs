// Layer 2 · Topic 7 - Rust's contribution to the SYN table.
//
// std::net only, and the HTTP written out by hand, because this row is about
// ONE number -- how many TCP connections were opened -- and a client library
// would answer it for us rather than letting us watch it.
//
// One TcpStream, kept, reused for every request: the hand-rolled equivalent of
// cloning a reqwest::Client instead of rebuilding it.
//
//   LAB_URL=http://127.0.0.1:8000/work cargo run --release
use std::env;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::time::Instant;

fn main() {
    let url = env::var("LAB_URL").unwrap_or_else(|_| "http://127.0.0.1:8000/work".into());
    let n: usize = env::var("LAB_REQUESTS").ok().and_then(|s| s.parse().ok()).unwrap_or(30);

    let rest = url.strip_prefix("http://").expect("this row speaks cleartext HTTP only");
    let (host_port, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };

    let t0 = Instant::now();
    let stream = TcpStream::connect(host_port).expect("connect");
    let mut reader = BufReader::new(stream.try_clone().expect("clone"));
    let mut writer = stream;

    for _ in 0..n {
        write!(writer, "GET {path} HTTP/1.1\r\nHost: {host_port}\r\n\r\n").expect("write");
        writer.flush().expect("flush");

        // Read the status line and headers, then exactly Content-Length bytes.
        // Getting this wrong is how you leave a body in the buffer and read it
        // as the next response -- Topic 3's off-by-one, which is why the
        // length is honoured here rather than guessed at.
        let mut len = 0usize;
        loop {
            let mut line = String::new();
            reader.read_line(&mut line).expect("read");
            if line == "\r\n" || line.is_empty() {
                break;
            }
            if let Some(v) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                len = v.trim().parse().unwrap_or(0);
            }
        }
        let mut body = vec![0u8; len];
        reader.read_exact(&mut body).expect("body");
    }

    println!(
        "one TcpStream reused for {} requests in {} ms",
        n,
        t0.elapsed().as_millis()
    );
}
