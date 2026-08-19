// Layer 7 · Topic 8 — Crypto hygiene and rate limiting (Rust).
//
// `cargo run --release` (`cargo fetch` once, online, for sha2 + argon2). Part A
// (hash cost) measures sha256-vs-argon2id at the OWASP low-memory baseline.
// Parts B and C are the point in Rust anyway: constant-time comparison as a
// TYPE-LEVEL property (subtle's Choice, not bool) and why std::hint::black_box
// is needed so LLVM does not optimise the timed work away.
use argon2::password_hash::{PasswordHash, PasswordHasher, PasswordVerifier, SaltString};
use argon2::{Algorithm, Argon2, Params, Version};
use sha2::{Digest, Sha256};
use std::hint::black_box;
use std::time::Instant;

// Deterministic xorshift RNG so the program needs no rand crate.
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u8 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        (self.0 & 0xff) as u8
    }
    fn bytes(&mut self, n: usize) -> Vec<u8> {
        (0..n).map(|_| self.next()).collect()
    }
}

// Short-circuits on first mismatch -> timing depends on the secret.
fn naive_eq(a: &[u8], b: &[u8]) -> u8 {
    if a.len() != b.len() {
        return 0;
    }
    for i in 0..a.len() {
        if a[i] != b[i] {
            return 0;
        }
    }
    1
}

// Constant-time: always touches every byte, XOR-accumulates the difference.
// (subtle::ConstantTimeEq does this and returns a Choice so you cannot `if` on
// it -- a branch would put the timing difference right back.)
fn ct_eq(a: &[u8], b: &[u8]) -> u8 {
    if a.len() != b.len() {
        return 0;
    }
    let mut diff = 0u8;
    for i in 0..a.len() {
        diff |= a[i] ^ b[i];
    }
    ((diff == 0) as u8)
}

fn part_b(rng: &mut Rng) {
    println!("B. Timing signal: naive short-circuit vs constant-time");
    let secret = rng.bytes(32);
    let candidate = |rng: &mut Rng, matching: usize| {
        let mut c = rng.bytes(32);
        c[..matching].copy_from_slice(&secret[..matching]);
        if matching < 32 {
            c[matching] = secret[matching] ^ 0xFF;
        }
        c
    };
    let avg_ns = |f: &dyn Fn(&[u8], &[u8]) -> u8, cand: &[u8], reps: u64| -> f64 {
        let t0 = Instant::now();
        let mut sink = 0u64;
        for _ in 0..reps {
            sink += black_box(f(black_box(&secret), black_box(cand))) as u64;
        }
        black_box(sink);
        t0.elapsed().as_nanos() as f64 / reps as f64
    };
    println!("   matching leading bytes ->        avg ns/op");
    for (label, f) in [
        ("naive_eq", &naive_eq as &dyn Fn(&[u8], &[u8]) -> u8),
        ("ct_eq (constant)", &ct_eq as &dyn Fn(&[u8], &[u8]) -> u8),
    ] {
        let mut out = format!("   {label:<18}");
        for k in [0usize, 8, 16, 31] {
            let cand = candidate(rng, k);
            out += &format!(" k={k}:{:.2}", avg_ns(f, &cand, 3_000_000));
        }
        println!("{out}");
    }
    println!("   (naive trends up with k; ct_eq flat. subtle's Choice type stops you");
    println!("    branching on the result; black_box stops LLVM deleting the work.)\n");
}

fn part_c() {
    println!("C. Rate limiting: attempts-to-first-success and effective limit");
    const LIST: usize = 1000;
    const CORRECT_AT: usize = 500;
    const CONFIGURED: i32 = 10;
    use std::collections::HashMap;

    let run = |mode: &str, workers: usize, source_ips: usize| -> (i32, bool) {
        let workers = workers.max(1);
        let source_ips = source_ips.max(1);
        let mut allowed = 0;
        let mut reached = false;
        let mut buckets: HashMap<String, i32> = HashMap::new();
        for i in 1..=LIST {
            let ip = i % source_ips;
            let permitted = if mode == "off" {
                true
            } else {
                let key = match mode {
                    "redis_token_bucket" => "account".to_string(),
                    "inproc" => format!("w{}", i % workers),
                    _ => format!("ip{ip}"),
                };
                let b = buckets.entry(key).or_insert(CONFIGURED);
                if *b > 0 {
                    *b -= 1;
                    true
                } else {
                    false
                }
            };
            if permitted {
                allowed += 1;
                if i == CORRECT_AT {
                    reached = true;
                }
            }
        }
        (allowed, reached)
    };

    for (mode, w, ips, note) in [
        ("off", 1, 1, "no limit"),
        ("redis_token_bucket", 1, 1, "shared bucket, configured=10"),
        ("inproc", 1, 1, "in-proc, 1 worker"),
        ("inproc", 4, 1, "in-proc, 4 workers -> effective 4x"),
        ("ip_keyed", 1, 50, "IP-keyed, attacker uses 50 IPs"),
    ] {
        let (allowed, reached) = run(mode, w, ips);
        let msg = if reached { "reached password" } else { "password NOT reached" };
        println!("   {mode:<18} {note:<34} allowed={allowed:<4} {msg}");
    }
    println!("\n   effective/configured: inproc workers=4 allows ~{} vs configured {} -> 4x.",
             4 * CONFIGURED, CONFIGURED);
    println!("   IP-keyed with 50 IPs lets the password through -> keying on IP is a fake fix.\n");
}

// Fast hash (sha256) vs a slow password hash (argon2id at OWASP m=19MiB,t=2,p=1),
// then the crack-time model: with V verifications/sec and an attacker rig N times
// your single core over a candidate list of K, first crack is K / (V*N) seconds.
fn part_a() {
    println!("A. Hash cost (verifications/sec, measured)");
    let password = b"correct horse battery staple";

    // sha256 -- a fast hash, the wrong tool for passwords.
    let reps: u64 = 200_000;
    let t0 = Instant::now();
    for _ in 0..reps {
        let mut h = Sha256::new();
        h.update(black_box(&password[..]));
        black_box(h.finalize());
    }
    let sha_vps = reps as f64 / t0.elapsed().as_secs_f64();
    println!("   sha256          {:>14.0} verify/sec", sha_vps);

    // argon2id at the OWASP low-memory baseline (m=19456 KiB, t=2, p=1).
    let params = Params::new(19456, 2, 1, None).expect("argon2 params");
    let argon2 = Argon2::new(Algorithm::Argon2id, Version::V0x13, params);
    let salt = SaltString::from_b64("cGFzc3dvcmRzYWx0dg").expect("salt");
    let hash = argon2
        .hash_password(b"pw", &salt)
        .expect("hash")
        .to_string();
    let parsed = PasswordHash::new(&hash).expect("parse hash");
    let reps: u64 = 20;
    let t0 = Instant::now();
    for _ in 0..reps {
        black_box(argon2.verify_password(b"pw", &parsed).is_ok());
    }
    let arg_vps = reps as f64 / t0.elapsed().as_secs_f64();
    println!("   argon2id(19MiB) {:>14.1} verify/sec", arg_vps);

    let n = 10_000f64; // attacker rig is 10,000x your single core (GPU farm)
    let k = 1_000_000f64; // candidate list size
    println!(
        "   crack-time model: attacker rig N={:.0}x, list K={:.0} candidates",
        n, k
    );
    println!("      sha256:   {:.6} s to first crack", k / (sha_vps * n));
    let ratio = sha_vps / arg_vps;
    println!(
        "      argon2id: {:.1} s to first crack  -- ~{:.0}x slower per verify than \
         sha256; the same attacker takes {:.0}x longer, and argon2id's memory-hardness \
         also blunts the GPU 'N' far more than a plain hash does",
        k / (arg_vps * n),
        ratio,
        ratio
    );
    println!();
}

fn main() {
    println!("Layer 7 · Topic 8 — hash cost, timing signal, rate limiting\n");
    part_a();
    let mut rng = Rng(0x9E3779B97F4A7C15);
    part_b(&mut rng);
    part_c();
    println!("Takeaway: password hash must be SLOW (argon2id), a secret compare \
              CONSTANT-TIME (a Choice, not a bool), and a rate limit keyed on the \
              account with SHARED state.");
}
