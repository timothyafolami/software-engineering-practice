// Layer 10 - Topic 4: the reference implementation. (Rust)
//
// What this demonstrates
//     Rust is the deliberate contrast to cpp/softmax.cpp. There is no
//     stable -ffast-math, f32 arithmetic is IEEE-strict, reassociation
//     never happens, and an FMA appears only where you wrote `mul_add`.
//     The same source therefore produces the same bits at every
//     optimisation level and on every target that has IEEE f32 -- which
//     makes this the file to check the other five against when they
//     disagree.
//
//     Four sections, all printed as exact hex bits:
//
//       1. naive vs max-subtracted softmax, same as everywhere else;
//       2. the same values summed forwards and backwards -- Rust will not
//          reassociate them for you, so any difference here is real
//          non-associativity and not a compiler decision;
//       3. `a * b + c` against `a.mul_add(b, c)`. C++ may substitute the
//          second for the first silently at -O2; Rust never does. The
//          difference between the two lines is the size of the effect the
//          C++ build is free to introduce without telling you;
//       4. partitioned summation across W, so this file also reproduces
//          the batch-invariance finding and can be diffed against
//          golang/parallel_sum.go and java/ParallelSum.java.
//
// What to look for
//     - Section 3: `a*b + c` and `a.mul_add(b, c)` differ in the last
//       bits, because mul_add rounds once and the separate operations
//       round twice. Both are correct. Only one of them is what you wrote.
//     - Section 4 against the Go and Java outputs. The W=1 result should
//       be reproducible across all three languages given the same input,
//       and the point of this file is that Rust's cannot silently change
//       between builds.
//     - Rebuild with `cargo run` (debug) instead of `--release` and diff.
//       Nothing in the numeric output should move. If it does, that is a
//       finding worth chasing, not noise.
//
// No dependencies. Runs with no arguments:
//     cargo run --release --manifest-path rust/strict_fp/Cargo.toml

const N: usize = 10_000_000;
const VOCAB: usize = 1024;
const SEED: u64 = 20260818;

/// Deterministic xorshift so every run of this file is comparable, and so
/// the input is not itself a source of variation.
struct Rng(u64);

impl Rng {
    fn next_f64(&mut self) -> f64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }

    /// Box-Muller. The shape matters; the source does not.
    fn normal(&mut self) -> f64 {
        let u = self.next_f64().max(1e-12);
        (-2.0 * u.ln()).sqrt() * (2.0 * std::f64::consts::PI * self.next_f64()).cos()
    }
}

fn bits(x: f32) -> String {
    format!("0x{:08x}", x.to_bits())
}

fn softmax_naive(x: &[f32]) -> (f32, f32) {
    let mut total = 0.0f32;
    let e: Vec<f32> = x.iter().map(|v| v.exp()).collect();
    for v in &e {
        total += v;
    }
    let max_p = e.iter().fold(0.0f32, |a, v| a.max(v / total));
    (total / total, max_p)
}

fn softmax_stable(x: &[f32]) -> (f32, f32) {
    let m = x.iter().copied().fold(f32::NEG_INFINITY, f32::max);
    let mut total = 0.0f32;
    // Exact, not approximate: multiplying numerator and denominator by
    // exp(-max) is an identity that moves the largest exponent to exp(0).
    let e: Vec<f32> = x.iter().map(|v| (v - m).exp()).collect();
    for v in &e {
        total += v;
    }
    let max_p = e.iter().fold(0.0f32, |a, v| a.max(v / total));
    (total / total, max_p)
}

fn sum_partitioned(data: &[f32], w: usize) -> f32 {
    if w <= 1 {
        let mut s = 0.0f32;
        for v in data {
            s += v;
        }
        return s;
    }
    let chunk = data.len().div_ceil(w);
    let partials: Vec<f32> = data
        .chunks(chunk)
        .map(|c| {
            let mut s = 0.0f32;
            for v in c {
                s += v;
            }
            s
        })
        .collect();
    // Combined in index order, so this is deterministic for a given w. The
    // only variable in the whole function is how many pieces there were.
    let mut total = 0.0f32;
    for p in partials {
        total += p;
    }
    total
}

fn main() {
    println!("Rust - IEEE-strict floating point, the reference implementation");
    println!("  no -ffast-math exists on stable, no reassociation, FMA only on request");
    println!("  seed {SEED}\n");

    // ---- 1. softmax ------------------------------------------------------
    let mut rng = Rng(SEED);
    println!("Softmax, naive vs max-subtracted (f32)");
    println!("{}", "-".repeat(78));
    println!(
        "  {:>6} {:>14} {:>14} {:>14} {:>14}",
        "peak", "naive sum", "naive max p", "stable sum", "stable max p"
    );
    for peak in [50.0f32, 200.0, 800.0] {
        let mut x: Vec<f32> = (0..VOCAB).map(|_| rng.normal() as f32).collect();
        x[0] = peak;
        let (ns, np) = softmax_naive(&x);
        let (ss, sp) = softmax_stable(&x);
        println!(
            "  {peak:>6.0} {ns:>14} {:>14} {ss:>14} {:>14}",
            bits(np),
            bits(sp)
        );
    }
    println!("\n  A naive sum of NaN is NaN, and NaN as a probability is a sampler");
    println!("  reading from garbage. The stable form costs one subtraction.");

    // ---- 2. summation order ---------------------------------------------
    let mut rng = Rng(SEED);
    let small: Vec<f32> = (0..VOCAB).map(|_| rng.normal() as f32).collect();
    let mut fwd = 0.0f32;
    for v in &small {
        fwd += v;
    }
    let mut rev = 0.0f32;
    for v in small.iter().rev() {
        rev += v;
    }
    println!("\nSame values, opposite summation order");
    println!("{}", "-".repeat(78));
    println!("  forward  {fwd:>16.9}  {}", bits(fwd));
    println!("  reverse  {rev:>16.9}  {}", bits(rev));
    println!("  identical bits: {}", if fwd == rev { "yes" } else { "NO" });
    println!("  Rust did not reorder anything here. This is plain");
    println!("  non-associativity, which every language in this topic has.");

    // ---- 3. mul_add ------------------------------------------------------
    let (a, b, c) = (1.000_000_1f32, 3.141_592_7f32, -3.141_592_9f32);
    let separate = a * b + c;
    let fused = a.mul_add(b, c);
    println!("\na*b + c   vs   a.mul_add(b, c)");
    println!("{}", "-".repeat(78));
    println!("  a = {a}, b = {b}, c = {c}");
    println!("  a*b + c            {separate:>16.9e}  {}", bits(separate));
    println!("  a.mul_add(b, c)    {fused:>16.9e}  {}", bits(fused));
    println!(
        "  identical bits: {}",
        if separate == fused { "yes" } else { "NO" }
    );
    println!("  mul_add rounds once; the separate operations round twice. Both");
    println!("  are correct. In C++ at -O2 the compiler may substitute one for");
    println!("  the other and not mention it -- that substitution is what");
    println!("  cpp/softmax.cpp's -ffast-math diff is showing you. Rust makes it");
    println!("  a function call, so it is in the source or it did not happen.");

    // ---- 4. partitioned sum ---------------------------------------------
    let mut rng = Rng(SEED);
    let data: Vec<f32> = (0..N).map(|_| (rng.next_f64() + 0.5) as f32).collect();
    let exact: f64 = data.iter().map(|v| *v as f64).sum();

    println!("\nPartition-order nondeterminism, f32 -- compare against Go and Java");
    println!("{}", "-".repeat(78));
    println!("  {N} values ~U(0.5, 1.5), f64 reference sum {exact:.6}");
    println!("  {:>8} {:>20} {:>14} {:>14}", "workers", "sum", "bits", "rel err");
    let mut seen: Vec<u32> = Vec::new();
    let mut values: Vec<f32> = Vec::new();
    for w in [1usize, 2, 4, 8, 16, 32, 64] {
        let s = sum_partitioned(&data, w);
        if !seen.contains(&s.to_bits()) {
            seen.push(s.to_bits());
        }
        values.push(s);
        println!(
            "  {w:>8} {s:>20.6} {:>14} {:>14.3e}",
            bits(s),
            ((s as f64) - exact).abs() / exact
        );
    }
    values.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let spread = ((values[values.len() - 1] - values[0]) as f64) / exact;
    println!(
        "\n  distinct sums: {} of 7    relative spread: {spread:.3e}    \
         (f32 epsilon: {:.3e})",
        seen.len(),
        f32::EPSILON
    );
    println!("\n  Each partitioning is reproducible; which partitioning you get is");
    println!("  not, because on an inference server the batch shape picks it and");
    println!("  other people's traffic picks the batch shape. Rebuild this file at");
    println!("  any optimisation level and every number above should be unchanged.");
}
