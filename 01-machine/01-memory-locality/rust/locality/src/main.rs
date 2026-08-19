// Layer 1 - Memory & cache locality
// Same pointer-chasing benchmark: two physical layouts of the same logical
// traversal.
//
// sequential -> node i's successor lives at i+1 (cache-friendly)
// shuffled   -> node i's successor is a random other node (cache-hostile)
//
// Deliberately using plain Vec<i32> indices rather than Box<Node> pointers
// so this stays a fair comparison with the array-based versions in the
// other languages. Feel free to also try a real Box<Node> linked list here
// once you've read the "when a linked list loses to an array" bullet.

use std::time::Instant;

const N: usize = 2_000_000;
const LAPS: usize = 5;

// Tiny deterministic xorshift RNG so this has zero dependencies.
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
}

fn build(shuffled: bool) -> (Vec<i32>, Vec<i32>) {
    let values: Vec<i32> = (0..N as i32).collect();
    let mut next = vec![0i32; N];

    if !shuffled {
        for i in 0..N {
            next[i] = ((i + 1) % N) as i32;
        }
        return (values, next);
    }

    let mut perm: Vec<usize> = (0..N).collect();
    let mut rng = Rng(0x2545F4914F6CDD1D);
    for i in (1..N).rev() {
        let j = (rng.next() as usize) % (i + 1);
        perm.swap(i, j);
    }
    for i in 0..N {
        next[perm[i]] = perm[(i + 1) % N] as i32;
    }
    (values, next)
}

fn traverse(values: &[i32], next: &[i32], laps: usize) -> i64 {
    let mut total: i64 = 0;
    let mut idx: usize = 0;
    let steps = N * laps;
    for _ in 0..steps {
        total += values[idx] as i64;
        idx = next[idx] as usize;
    }
    total
}

fn bench(label: &str, shuffled: bool) {
    let (values, next) = build(shuffled);
    let start = Instant::now();
    let total = traverse(&values, &next, LAPS);
    let elapsed = start.elapsed();
    let ns_per_step = elapsed.as_nanos() as f64 / (N * LAPS) as f64;
    println!(
        "{:10}  total={:15}  time={:6.3}s  {:6.1} ns/step",
        label,
        total,
        elapsed.as_secs_f64(),
        ns_per_step
    );
}

fn main() {
    println!("N={} laps={}", N, LAPS);
    bench("sequential", false);
    bench("shuffled", true);
}
