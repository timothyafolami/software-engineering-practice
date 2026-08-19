// Cargo compiles and runs this at build time. A benign marker stands in for
// "harvest tokens and exfiltrate". Procedural macros go one step further and
// execute at COMPILE time inside the compiler. Cargo.lock hashes deps and
// cargo-vet / cargo-deny exist precisely because this execution surface is real.
use std::io::Write;
fn main() {
    let marker = "/tmp/pwned-rs.txt";
    if let Ok(mut f) = std::fs::File::create(marker) {
        let _ = writeln!(f, "build.rs executed at build time");
    }
    println!("cargo:warning=build.rs executed -> wrote /tmp/pwned-rs.txt");
}
