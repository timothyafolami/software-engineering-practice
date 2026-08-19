// Layer 4 · Topic 1 — the third outcome, in Rust.
//
// WHAT THIS DEMONSTRATES
//   The same five faults and the same ledger as the Python, Node, Go, C++ and
//   Java programs. Rust's two contributions:
//
//   1. There is no HTTP client here. connect(), write() and read() are three
//      calls you make yourself, so "which phase did this die in" is not
//      something to infer from an exception name -- it is which line failed.
//      The write path is handled properly: a write that moved zero bytes is
//      provably safe, a partial write is not, and no mainstream client library
//      exposes that difference.
//
//   2. The safe/unsafe decision is encoded in the type system. `RetryPermit`
//      has a private field and no public constructor; the only way to obtain
//      one is `Outcome::retry_permit()`, which returns `Some` exclusively for
//      `ProvablyNotSent`. Retrying an ambiguous outcome is therefore not a
//      discipline problem or a review comment -- it does not compile. The exact
//      compiler error is quoted at the bottom of this file.
//
//   Sending an RST needs `setsockopt(SO_LINGER)`, which safe std does not
//   expose on this toolchain (`tcp_linger` is still unstable). That is the one
//   `unsafe` block in the file, and it is a fair illustration of where the
//   guardrails end: the kernel-facing edge.
//
// WHAT TO LOOK FOR
//   Phase 1's duplicate charges (created by the client's own retries) against
//   phase 2's, and the unresolved ambiguity that survives both.
//
// Run:  cargo run --release

use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::{Shutdown, SocketAddr, TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

const CLIENT_TIMEOUT: Duration = Duration::from_millis(300);
const SLOW_RESPONSE: Duration = Duration::from_millis(1000);
const REQUESTS_PER_MODE: usize = 4;
const MAX_ATTEMPTS: usize = 3;

const MODES: [&str; 6] = [
    "ok",
    "slow",
    "hang",
    "reset",
    "crash_after_commit",
    "refused",
];

// --- server-side truth ------------------------------------------------------

type Ledger = Arc<Mutex<Vec<String>>>;

fn commit(ledger: &Ledger, charge_id: &str) {
    ledger.lock().unwrap().push(charge_id.to_string());
}

/// Send an RST rather than a FIN by setting SO_LINGER with a zero timeout.
///
/// `TcpStream::set_linger` exists but is behind the unstable `tcp_linger`
/// feature on this toolchain, so this is a direct setsockopt(2). Declaring the
/// symbol rather than depending on the `libc` crate keeps the program buildable
/// with no network access -- and makes the FFI boundary visible, which is the
/// point of having Rust in this topic at all.
#[cfg(unix)]
fn reset_connection(stream: &TcpStream) {
    use std::os::fd::AsRawFd;

    #[repr(C)]
    struct Linger {
        l_onoff: std::ffi::c_int,
        l_linger: std::ffi::c_int,
    }

    #[cfg(target_os = "macos")]
    const SOL_SOCKET: std::ffi::c_int = 0xffff;
    #[cfg(target_os = "macos")]
    const SO_LINGER: std::ffi::c_int = 0x0080;
    #[cfg(target_os = "linux")]
    const SOL_SOCKET: std::ffi::c_int = 1;
    #[cfg(target_os = "linux")]
    const SO_LINGER: std::ffi::c_int = 13;

    extern "C" {
        fn setsockopt(
            fd: std::ffi::c_int,
            level: std::ffi::c_int,
            name: std::ffi::c_int,
            value: *const std::ffi::c_void,
            len: u32,
        ) -> std::ffi::c_int;
    }

    let linger = Linger {
        l_onoff: 1,
        l_linger: 0,
    };
    // SAFETY: `stream` owns a valid open fd for the duration of this call, and
    // `linger` is a correctly-sized, correctly-aligned struct linger.
    unsafe {
        setsockopt(
            stream.as_raw_fd(),
            SOL_SOCKET,
            SO_LINGER,
            &linger as *const Linger as *const std::ffi::c_void,
            std::mem::size_of::<Linger>() as u32,
        );
    }
    let _ = stream.shutdown(Shutdown::Both);
}

fn serve(mut stream: TcpStream, ledger: Ledger, held: Arc<Mutex<Vec<TcpStream>>>) {
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut buf = [0u8; 4096];
    let n = match stream.read(&mut buf) {
        Ok(0) | Err(_) => return,
        Ok(n) => n,
    };
    let text = String::from_utf8_lossy(&buf[..n]);
    let line = text.lines().next().unwrap_or("");
    let path = line.split_whitespace().nth(1).unwrap_or("");
    let parts: Vec<&str> = path.splitn(4, '/').collect(); // ["", "charge", mode, id]
    if parts.len() < 4 {
        return;
    }
    let (mode, charge_id) = (parts[2], parts[3]);

    match mode {
        "ok" => {
            commit(&ledger, charge_id);
            reply(&mut stream, charge_id);
        }
        "slow" => {
            commit(&ledger, charge_id);
            thread::sleep(SLOW_RESPONSE);
            reply(&mut stream, charge_id);
        }
        "hang" => {
            // Accepted, committed, never answered. Parked so the socket stays
            // open -- dropping it here would close it and defeat the fault.
            commit(&ledger, charge_id);
            held.lock().unwrap().push(stream);
        }
        "reset" => {
            commit(&ledger, charge_id);
            reset_connection(&stream);
        }
        "crash_after_commit" => {
            // The case no timeout tuning can fix: durable work, dead reporter.
            commit(&ledger, charge_id);
            drop(stream);
        }
        _ => {}
    }
}

fn reply(stream: &mut TcpStream, charge_id: &str) {
    let body = format!("{{\"charge_id\":\"{charge_id}\"}}");
    let head = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    let _ = stream.write_all(head.as_bytes());
    let _ = stream.write_all(body.as_bytes());
}

// --- the outcome type -------------------------------------------------------
//
// In its own module on purpose. Privacy in Rust is per-module, not per-type, so
// a `RetryPermit` declared beside the code that consumes it would be
// constructible by that code and the guarantee would be decorative. The module
// boundary is what makes it real.
mod outcome {
    #[derive(Debug)]
    pub enum Outcome {
        Succeeded(u16),
        /// The request provably never reached the server. Retrying cannot
        /// duplicate work, because no work was requested.
        ProvablyNotSent { phase: &'static str, detail: String },
        /// The request may or may not have been executed. Nothing observable on
        /// this side will ever tell us which.
        Unknown { phase: &'static str, detail: String },
    }

    /// Permission to retry. The unit field is private to this module, so no
    /// code outside it can build one -- `Outcome::retry_permit` is the only
    /// source, and it only yields one for `ProvablyNotSent`.
    pub struct RetryPermit(());

    impl Outcome {
        pub fn retry_permit(&self) -> Option<RetryPermit> {
            match self {
                Outcome::ProvablyNotSent { .. } => Some(RetryPermit(())),
                _ => None,
            }
        }

        pub fn label(&self) -> String {
            match self {
                Outcome::Succeeded(code) => format!("SUCCESS({code})"),
                Outcome::ProvablyNotSent { phase, detail } => {
                    format!("SAFE({detail} [{phase}])")
                }
                Outcome::Unknown { phase, detail } => {
                    format!("AMBIGUOUS({detail} [{phase}])")
                }
            }
        }

        pub fn is_unknown(&self) -> bool {
            matches!(self, Outcome::Unknown { .. })
        }
    }
}

use outcome::Outcome;

/// Issue one request, reporting which phase failed.
///
/// The three phases are three calls. Nothing here is inferred from an error
/// name: `connect` failing means the bytes never left, a write that moved zero
/// bytes means the same, and anything after that is unknowable.
fn attempt(addr: SocketAddr, path: &str) -> Outcome {
    let mut stream = match TcpStream::connect_timeout(&addr, CLIENT_TIMEOUT) {
        Ok(s) => s,
        Err(e) => {
            return Outcome::ProvablyNotSent {
                phase: "connect",
                detail: format!("{:?}", e.kind()),
            }
        }
    };
    let _ = stream.set_write_timeout(Some(CLIENT_TIMEOUT));
    let _ = stream.set_read_timeout(Some(CLIENT_TIMEOUT));

    let request = format!("GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n");
    let bytes = request.as_bytes();
    let mut written = 0usize;
    while written < bytes.len() {
        match stream.write(&bytes[written..]) {
            Ok(0) => break,
            Ok(n) => written += n,
            Err(e) => {
                // The distinction no HTTP client exposes: zero bytes written is
                // provably safe. One byte written is not, because the peer may
                // have a complete request from a previous attempt's framing or
                // may complete the read from its own buffer.
                return if written == 0 {
                    Outcome::ProvablyNotSent {
                        phase: "write",
                        detail: format!("{:?}", e.kind()),
                    }
                } else {
                    Outcome::Unknown {
                        phase: "partial-write",
                        detail: format!("{:?}", e.kind()),
                    }
                };
            }
        }
    }
    if written < bytes.len() {
        return Outcome::Unknown {
            phase: "partial-write",
            detail: "short write".into(),
        };
    }

    let mut response = Vec::new();
    match stream.read_to_end(&mut response) {
        Err(e) => Outcome::Unknown {
            phase: "read",
            detail: format!("{:?}", e.kind()),
        },
        Ok(0) => Outcome::Unknown {
            phase: "read",
            detail: "closed with no response".into(),
        },
        Ok(_) => {
            let text = String::from_utf8_lossy(&response);
            let code = text
                .split_whitespace()
                .nth(1)
                .and_then(|c| c.parse::<u16>().ok())
                .unwrap_or(0);
            if code == 0 {
                Outcome::Unknown {
                    phase: "read",
                    detail: "unparseable response".into(),
                }
            } else {
                Outcome::Succeeded(code)
            }
        }
    }
}

// --- phases -----------------------------------------------------------------

struct PhaseResult {
    duplicates: usize,
    unresolved: usize,
}

fn run_phase(
    tag: &str,
    name: &str,
    note: &str,
    server: SocketAddr,
    closed: SocketAddr,
    ledger: &Ledger,
    retry_unknown: bool,
) -> PhaseResult {
    let before = ledger.lock().unwrap().len();
    let mut unresolved = 0usize;

    println!();
    println!("  {name}");
    println!("  {note}");
    println!(
        "  {:<20} {:<44} {:>9} {:>12}",
        "fault", "client verdict", "attempts", "ledger rows"
    );

    for mode in MODES {
        let mode_before = ledger.lock().unwrap().len();
        let mut attempts = 0usize;
        let mut counts: BTreeMap<String, usize> = BTreeMap::new();
        let addr = if mode == "refused" { closed } else { server };

        for i in 0..REQUESTS_PER_MODE {
            let charge_id = format!("{tag}-{mode}-{i}");
            let path = format!("/charge/{mode}/{charge_id}");
            let mut last = Outcome::Unknown {
                phase: "none",
                detail: "not attempted".into(),
            };
            for _ in 0..MAX_ATTEMPTS {
                attempts += 1;
                last = attempt(addr, &path);
                match last {
                    Outcome::Succeeded(_) => break,
                    _ => match last.retry_permit() {
                        // The permit is the only key to this branch. There is no
                        // way to reach it holding an Unknown.
                        Some(permit) => {
                            let _ = permit;
                            continue;
                        }
                        None => {
                            if retry_unknown {
                                // Phase 1 only: the shape of the bug, spelled out.
                                // Real code writes this as `Err(_) => continue`
                                // and never notices it made the choice.
                                continue;
                            }
                            break;
                        }
                    },
                }
            }
            if last.is_unknown() {
                unresolved += 1;
            }
            *counts.entry(last.label()).or_insert(0) += 1;
        }

        let rows = ledger.lock().unwrap().len() - mode_before;
        let summary: Vec<String> = counts.iter().map(|(k, n)| format!("{n}x {k}")).collect();
        println!(
            "  {:<20} {:<44} {:>9} {:>12}",
            mode,
            summary.join(", "),
            attempts,
            rows
        );
    }

    let rows = ledger.lock().unwrap();
    let written = &rows[before..];
    let mut seen: BTreeMap<&String, usize> = BTreeMap::new();
    for id in written {
        *seen.entry(id).or_insert(0) += 1;
    }
    let duplicates: usize = seen.values().map(|n| n.saturating_sub(1)).sum();
    println!("  ledger rows written this phase : {}", written.len());
    println!("  DUPLICATE CHARGES              : {duplicates}   <- created by this client's retries");
    println!("  unresolved ambiguous outcomes  : {unresolved}   <- caller cannot tell whether these happened");
    PhaseResult {
        duplicates,
        unresolved,
    }
}

fn main() {
    let ledger: Ledger = Arc::new(Mutex::new(Vec::new()));
    let held: Arc<Mutex<Vec<TcpStream>>> = Arc::new(Mutex::new(Vec::new()));

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind ledger");
    let server_addr = listener.local_addr().unwrap();
    {
        let ledger = Arc::clone(&ledger);
        let held = Arc::clone(&held);
        thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let ledger = Arc::clone(&ledger);
                let held = Arc::clone(&held);
                thread::spawn(move || serve(stream, ledger, held));
            }
        });
    }

    // A port nothing is listening on, so connect() gets ECONNREFUSED.
    let closed_addr = {
        let probe = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = probe.local_addr().unwrap();
        drop(probe);
        addr
    };

    println!("{}", "=".repeat(78));
    println!("Layer 4 · Topic 1 — partial failure and the ambiguous result (Rust)");
    println!("{}", "=".repeat(78));
    println!("  ledger        : {server_addr}  (in-process, holds server-side truth)");
    println!("  closed port   : {closed_addr}  (for the connect-refused case)");
    println!(
        "  client timeout: {:?}   slow response: {:?}   max attempts: {}",
        CLIENT_TIMEOUT, SLOW_RESPONSE, MAX_ATTEMPTS
    );
    println!("  phase detection: which of connect/write/read returned the error");

    let naive = run_phase(
        "p1",
        "phase 1 — retry on any error",
        "`Err(_) => continue`, which is what the shape of this loop invites",
        server_addr,
        closed_addr,
        &ledger,
        true,
    );
    let fixed = run_phase(
        "p2",
        "phase 2 — retry only with a RetryPermit",
        "the permit only exists for ProvablyNotSent; Unknown has no path to a retry",
        server_addr,
        closed_addr,
        &ledger,
        false,
    );

    println!();
    println!("{}", "-".repeat(78));
    println!(
        "  duplicate charges    phase 1: {:<6} phase 2: {}",
        naive.duplicates, fixed.duplicates
    );
    println!(
        "  unresolved ambiguity phase 1: {:<6} phase 2: {}",
        naive.unresolved, fixed.unresolved
    );
    println!();
    println!("  The type system removed the duplicates by making the wrong branch");
    println!("  unreachable. It cannot remove the ambiguity -- that is a property of");
    println!("  the network, not of the program. Topic 2 is what makes retrying an");
    println!("  ambiguous outcome safe.");

    held.lock().unwrap().clear();
}

// --- what the compiler refuses ----------------------------------------------
//
// Append this to the file and `cargo build --release` fails. The error below is
// this toolchain's actual output (rustc 1.97.1), copied verbatim, not a
// paraphrase -- reproduce it yourself before believing it.
//
//     fn retry_anything(_outcome: &Outcome) -> Option<outcome::RetryPermit> {
//         Some(outcome::RetryPermit(()))
//     }
//
//     error[E0603]: tuple struct constructor `RetryPermit` is private
//        --> src/main.rs:502:19
//         |
//     194 |     pub struct RetryPermit(());
//         |                            -- a constructor is private if any of the fields is private
//     ...
//     502 |     Some(outcome::RetryPermit(()))
//         |                   ^^^^^^^^^^^ private tuple struct constructor
//         |
//     note: the tuple struct constructor `RetryPermit` is defined here
//        --> src/main.rs:194:5
//         |
//     194 |     pub struct RetryPermit(());
//         |     ^^^^^^^^^^^^^^^^^^^^^^^^^^^
//
// This is the argument for Rust in a topic about correctness under failure. In
// every other language in this lab, "do not retry an ambiguous result" is a
// comment, a code review, or a lint someone can silence. Here it is a build
// failure, and the fix the compiler suggests (`pub ()`) is the one line a
// reviewer would catch.
