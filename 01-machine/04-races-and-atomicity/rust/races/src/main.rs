// Layer 1 - Rust's type system makes this whole class of bug refuse to
// compile in safe code. `&mut i64` is not `Send`-shareable across threads
// unless wrapped in something that proves synchronization (Mutex, Atomic*).
// To reproduce the actual race, you have to explicitly reach for `unsafe`
// and lie to the compiler about Send/Sync -- which is itself the lesson:
// in Rust, "this could race" becomes a visible, greppable, reviewable
// signal (the `unsafe` keyword) instead of a silent possibility.
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;

const THREADS: usize = 8;
const INCREMENTS: usize = 300_000;

// A raw pointer wrapper we manually (and dangerously) assert is safe to
// send across threads, purely so this demo compiles at all.
struct SharedPtr(*mut i64);
unsafe impl Send for SharedPtr {}
unsafe impl Sync for SharedPtr {}

fn run_unsafe() -> i64 {
    let mut counter: i64 = 0;
    let shared = SharedPtr(&mut counter as *mut i64);
    let handles: Vec<_> = (0..THREADS)
        .map(|_| {
            let shared = SharedPtr(shared.0);
            thread::spawn(move || {
                // Rust 2021's precise closure captures would otherwise grab
                // just the `*mut i64` field instead of the whole `SharedPtr`
                // wrapper, which defeats the `unsafe impl Send` above --
                // rebinding the whole struct here forces the compiler to
                // capture (and move) all of `shared`.
                let shared = shared;
                for _ in 0..INCREMENTS {
                    unsafe {
                        // Read-modify-write on shared memory, no synchronization.
                        // This is undefined behavior under Rust's memory model,
                        // not just "maybe wrong" -- the compiler is allowed to
                        // assume it never happens, which is exactly why safe
                        // Rust doesn't let you write it by accident.
                        *shared.0 += 1;
                    }
                }
            })
        })
        .collect();
    for h in handles {
        h.join().unwrap();
    }
    counter
}

fn run_mutex() -> i64 {
    let counter = Arc::new(Mutex::new(0i64));
    let handles: Vec<_> = (0..THREADS)
        .map(|_| {
            let counter = Arc::clone(&counter);
            thread::spawn(move || {
                for _ in 0..INCREMENTS {
                    let mut c = counter.lock().unwrap();
                    *c += 1;
                }
            })
        })
        .collect();
    for h in handles {
        h.join().unwrap();
    }
    let result = *counter.lock().unwrap();
    result
}

fn run_atomic() -> i64 {
    let counter = Arc::new(AtomicI64::new(0));
    let handles: Vec<_> = (0..THREADS)
        .map(|_| {
            let counter = Arc::clone(&counter);
            thread::spawn(move || {
                for _ in 0..INCREMENTS {
                    counter.fetch_add(1, Ordering::SeqCst);
                }
            })
        })
        .collect();
    for h in handles {
        h.join().unwrap();
    }
    counter.load(Ordering::SeqCst)
}

fn main() {
    let expected = (THREADS * INCREMENTS) as i64;
    let unsafe_result = run_unsafe();
    let mutex_result = run_mutex();
    let atomic_result = run_atomic();
    println!("expected:                      {}", expected);
    println!(
        "unsafe (raw ptr, no sync):     {}  (lost {})",
        unsafe_result,
        expected - unsafe_result
    );
    println!("safe (Mutex<i64>):             {}", mutex_result);
    println!("safe (AtomicI64):              {}", atomic_result);
}
