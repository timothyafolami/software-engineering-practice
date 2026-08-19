// Layer 10 - Topic 1: STREAM-style bandwidth ceiling in Rust.
//
// What this demonstrates
//     The same measurement as cpp/stream.cpp, written idiomatically. The
//     point is a null result: at --release, over a loop the optimiser can
//     prove is in range, bounds checking and the ownership model cost
//     nothing. When you are bound by physics the language stops mattering,
//     and this is the file that proves it rather than asserting it.
//
// What to look for
//     - Rust and C++ within ~10% at the same thread count. A larger gap
//       means the loop was written badly (indexing element by element
//       instead of zipping slices, so the bounds check survives), not that
//       Rust is slower at moving bytes.
//     - 1 thread vs all threads: whichever is higher is your ceiling. Do
//       not assume more threads wins; on a machine where one core already
//       saturates the memory controller, extra threads only add contention.
//
// Byte accounting matches the C++ and Python versions: copy and scale are
// 2N (a read and a write), add and triad are 3N.
//
// Run with no arguments:
//     cargo run --release --manifest-path rust/stream/Cargo.toml

use std::thread;
use std::time::Instant;

const BYTES_PER_ARRAY: usize = 512 * 1024 * 1024;
const ELEMS: usize = BYTES_PER_ARRAY / std::mem::size_of::<f64>();
const REPS: usize = 7;
const SCALAR: f64 = 3.0;

/// One STREAM kernel, expressed over slice chunks so it can be run on one
/// thread or split across many without changing the inner loop.
#[derive(Clone, Copy)]
enum Kernel {
    Copy,
    Scale,
    Add,
    Triad,
}

impl Kernel {
    fn label(self) -> &'static str {
        match self {
            Kernel::Copy => "copy   b[i] = a[i]",
            Kernel::Scale => "scale  b[i] = q*a[i]",
            Kernel::Add => "add    c[i] = a[i]+b[i]",
            Kernel::Triad => "triad  c[i] = a[i]+q*b[i]",
        }
    }

    fn byte_factor(self) -> usize {
        match self {
            Kernel::Copy | Kernel::Scale => 2,
            Kernel::Add | Kernel::Triad => 3,
        }
    }

    /// Zipped iteration, not indexing: this is the form the optimiser can
    /// prove in-range, so no bounds check survives into the hot loop.
    fn apply(self, a: &[f64], b: &mut [f64], c: &mut [f64]) {
        match self {
            Kernel::Copy => {
                for (dst, src) in b.iter_mut().zip(a.iter()) {
                    *dst = *src;
                }
            }
            Kernel::Scale => {
                for (dst, src) in b.iter_mut().zip(a.iter()) {
                    *dst = SCALAR * *src;
                }
            }
            Kernel::Add => {
                for (dst, (x, y)) in c.iter_mut().zip(a.iter().zip(b.iter())) {
                    *dst = *x + *y;
                }
            }
            Kernel::Triad => {
                for (dst, (x, y)) in c.iter_mut().zip(a.iter().zip(b.iter())) {
                    *dst = *x + SCALAR * *y;
                }
            }
        }
    }
}

fn run_once(kernel: Kernel, threads: usize, a: &[f64], b: &mut [f64], c: &mut [f64]) {
    if threads <= 1 {
        kernel.apply(a, b, c);
        return;
    }
    let chunk = ELEMS.div_ceil(threads);
    thread::scope(|scope| {
        let mut a_rest = a;
        let mut b_rest = &mut b[..];
        let mut c_rest = &mut c[..];
        while !a_rest.is_empty() {
            let n = chunk.min(a_rest.len());
            let (a_head, a_tail) = a_rest.split_at(n);
            let (b_head, b_tail) = b_rest.split_at_mut(n);
            let (c_head, c_tail) = c_rest.split_at_mut(n);
            a_rest = a_tail;
            b_rest = b_tail;
            c_rest = c_tail;
            scope.spawn(move || kernel.apply(a_head, b_head, c_head));
        }
    });
}

struct Measurement {
    first_gbps: f64,
    best_gbps: f64,
    best_ms: f64,
}

fn measure(kernel: Kernel, threads: usize, a: &[f64], b: &mut [f64], c: &mut [f64]) -> Measurement {
    let mut times = Vec::with_capacity(REPS);
    for _ in 0..REPS {
        let t0 = Instant::now();
        run_once(kernel, threads, a, b, c);
        times.push(t0.elapsed().as_secs_f64());
    }
    let bytes = (kernel.byte_factor() * BYTES_PER_ARRAY) as f64;
    let best = times[1..].iter().copied().fold(f64::INFINITY, f64::min);
    Measurement {
        first_gbps: bytes / times[0] / 1e9,
        best_gbps: bytes / best / 1e9,
        best_ms: best * 1e3,
    }
}

fn main() {
    let hw = thread::available_parallelism().map(|n| n.get()).unwrap_or(1);

    println!(
        "elements per array : {} f64 ({:.0} MiB)",
        ELEMS,
        BYTES_PER_ARRAY as f64 / (1024.0 * 1024.0)
    );
    println!(
        "working set        : {:.2} GiB across 3 arrays",
        3.0 * BYTES_PER_ARRAY as f64 / (1024.0 * 1024.0 * 1024.0)
    );
    println!("hardware threads   : {hw}");
    println!("reps               : {REPS} (rep 0 reported separately: first touch)\n");

    let a = vec![1.0f64; ELEMS];
    let mut b = vec![2.0f64; ELEMS];
    let mut c = vec![0.0f64; ELEMS];
    // First touch every page of `c` before any timing. `vec![0.0; n]` is
    // allocated with calloc, which hands back lazily-mapped zero pages: the
    // memory is not really there until something writes to it. Skip this and
    // the kernels that write `c` (add, triad) pay the page-fault handler
    // inside the timed region and read low by a factor of several -- which
    // looks exactly like "Rust is slower than C++" and is not.
    for x in c.iter_mut() {
        *x = 0.5;
    }

    let kernels = [Kernel::Copy, Kernel::Scale, Kernel::Add, Kernel::Triad];
    let thread_counts: Vec<usize> = if hw > 1 { vec![1, hw] } else { vec![1] };

    let mut ceiling = 0.0f64;
    let mut ceiling_threads = 1usize;

    for threads in thread_counts {
        println!("--- {threads} thread{} ---", if threads == 1 { "" } else { "s" });
        println!(
            "{:<28} {:>6} {:>11} {:>11} {:>9}",
            "kernel", "bytes", "rep0 GB/s", "best GB/s", "best ms"
        );
        for kernel in kernels {
            let m = measure(kernel, threads, &a, &mut b, &mut c);
            println!(
                "{:<28} {:>5}N {:>11.1} {:>11.1} {:>9.1}",
                kernel.label(),
                kernel.byte_factor(),
                m.first_gbps,
                m.best_gbps,
                m.best_ms
            );
            if m.best_gbps > ceiling {
                ceiling = m.best_gbps;
                ceiling_threads = threads;
            }
        }
        println!();
    }

    // Read the buffers so nothing above can be optimised away.
    let sink = c[0] + c[ELEMS / 2] + c[ELEMS - 1] + b[ELEMS - 1];
    std::hint::black_box(sink);

    println!(
        "best sustained     : {ceiling:.1} GB/s at {ceiling_threads} thread{}",
        if ceiling_threads == 1 { "" } else { "s" }
    );
    println!("Compare against cpp/stream.cpp at the same thread count. Within");
    println!("~10% is the expected -- and the interesting -- result.");
}
