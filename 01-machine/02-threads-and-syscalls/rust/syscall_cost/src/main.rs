// Layer 1 - What a syscall actually costs.
// Same read(/dev/zero) vs pure-loop comparison, using std::fs directly
// (which is a thin wrapper over the read(2) syscall on Linux).
use std::fs::File;
use std::hint::black_box;
use std::io::Read;
use std::time::Instant;

const N: u64 = 500_000;

fn bench_syscall() -> f64 {
    let mut f = File::open("/dev/zero").expect("open /dev/zero");
    let mut buf = [0u8; 1];
    let start = Instant::now();
    for _ in 0..N {
        f.read(&mut buf).unwrap();
    }
    start.elapsed().as_secs_f64()
}

fn bench_pure_rust() -> (f64, i64) {
    let mut total: i64 = 0;
    let start = Instant::now();
    for i in 0..N {
        // black_box forces the optimizer to treat `total` as observable, so
        // it can't prove the loop is dead and elide it entirely -- which is
        // exactly what LLVM did the first time this benchmark was written,
        // reporting an impossible 0.0 ns/iter. A benchmark that measures
        // "nothing" is its own lesson about optimizing compilers.
        total = black_box(total + (i & 0xFF) as i64);
    }
    (start.elapsed().as_secs_f64(), total)
}

fn main() {
    let t_sys = bench_syscall();
    let (t_pure, total) = bench_pure_rust();
    black_box(total);
    println!("N={}", N);
    println!(
        "read(/dev/zero) x{}:  {:6.3}s  ({:6.1} ns/call)",
        N,
        t_sys,
        t_sys / N as f64 * 1e9
    );
    println!(
        "pure rust loop:       {:6.3}s  ({:6.1} ns/iter)",
        t_pure,
        t_pure / N as f64 * 1e9
    );
    println!(
        "syscall is {:.1}x the cost of an equivalent pure-rust step",
        t_sys / t_pure
    );
}
