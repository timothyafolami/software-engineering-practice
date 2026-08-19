// Layer 1 - OS thread vs process creation cost.
// Rust's std::thread::spawn always creates a real 1:1 OS thread (there is
// no green-thread runtime in std) — that's the actual cost floor for
// "give me a new concurrent unit of execution" without an async runtime.
use std::process::Command;
use std::thread;
use std::time::Instant;

const N: u32 = 200;

fn bench_threads() -> f64 {
    let start = Instant::now();
    for _ in 0..N {
        let h = thread::spawn(|| {});
        h.join().unwrap();
    }
    start.elapsed().as_secs_f64()
}

fn bench_processes() -> f64 {
    let start = Instant::now();
    for _ in 0..N {
        Command::new("true").status().unwrap();
    }
    start.elapsed().as_secs_f64()
}

fn main() {
    let t_thread = bench_threads();
    let t_proc = bench_processes();
    println!("N={}", N);
    println!(
        "OS thread spawn+join: {:6.3}s  ({:7.1} us/thread)",
        t_thread,
        t_thread / N as f64 * 1e6
    );
    println!(
        "process spawn+wait:   {:6.3}s  ({:7.1} us/process)",
        t_proc,
        t_proc / N as f64 * 1e6
    );
    println!("process is {:.1}x the cost of a thread", t_proc / t_thread);
}
