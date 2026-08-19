// The program body is almost beside the point: the interesting code already
// ran in build.rs, before this main() was ever reachable.
fn main() {
    println!("Layer 7 · Topic 7 — the app runs; build.rs already ran at BUILD time.");
    println!("Check: ls -l /tmp/pwned-rs.txt  (written by build.rs, not by this main)");
}
