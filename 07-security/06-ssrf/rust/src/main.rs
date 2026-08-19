// Layer 7 · Topic 6 — SSRF: validate the connection, not the string (Rust).
//
// `cargo run` (no deps, offline). The README's Rust path: reqwest's
// redirect::Policy is an explicit constructor argument -- Policy::none() is a
// visible decision, not a flag you might miss -- and a custom Resolve owns DNS.
// The compile-time story is weaker than Topics 2/4: nothing in the type system
// stops you fetching a bad URL, so validation is ordinary logic. This program
// is that logic, using std::net to classify the resolved address.
//
// The finding: string_blocklist ALLOWs every internal target via an encoding it
// did not enumerate; resolve_and_pin BLOCKs them by classifying the IP.
use std::collections::HashMap;
use std::net::{Ipv4Addr, Ipv6Addr};

fn fake_dns() -> HashMap<&'static str, &'static str> {
    HashMap::from([
        ("internal-admin", "10.7.0.10"),
        ("metadata", "10.7.0.169"),
        ("allowed.test", "93.184.216.34"),
        ("a.rebind.lab.test", "10.7.0.10"),
        ("localhost", "127.0.0.1"),
    ])
}

const STRING_DENY: [&str; 3] = ["localhost", "127.0.0.1", "169.254.169.254"];

fn host_of(url: &str) -> String {
    let rest = url.splitn(2, "://").nth(1).unwrap_or(url);
    let authority = rest.split('/').next().unwrap_or("");
    let authority = authority.rsplit('@').next().unwrap_or(authority); // drop userinfo
    if let Some(a) = authority.strip_prefix('[') {
        return a.split(']').next().unwrap_or("").to_string(); // [IPv6]
    }
    authority.split(':').next().unwrap_or("").to_string() // strip :port
}

fn canonical_ip(host: &str, dns: &HashMap<&str, &str>) -> Option<String> {
    let host = dns.get(host).copied().unwrap_or(host);
    if host.chars().all(|c| c.is_ascii_digit()) && !host.is_empty() {
        let n: u32 = host.parse().ok()?; // "0", "2130706433"
        return Some(Ipv4Addr::from(n).to_string());
    }
    if host.parse::<Ipv4Addr>().is_ok() || host.contains(':') {
        return Some(host.to_string());
    }
    None
}

fn is_denied(ip: &str) -> bool {
    if let Ok(v4) = ip.parse::<Ipv4Addr>() {
        return v4.is_private() || v4.is_loopback() || v4.is_link_local()
            || v4.is_unspecified() || v4.octets()[0] == 0;
    }
    if let Ok(v6) = ip.parse::<Ipv6Addr>() {
        if v6.is_loopback() || v6.is_unspecified() {
            return true;
        }
        let s = v6.segments()[0];
        return (s & 0xffc0) == 0xfe80 || (s & 0xfe00) == 0xfc00; // link-local / unique-local
    }
    true // fail closed
}

fn verdict_blocklist(url: &str) -> &'static str {
    let low = url.to_lowercase();
    if STRING_DENY.iter().any(|d| low.contains(d)) { "BLOCK" } else { "ALLOW" }
}

fn main() {
    let dns = fake_dns();
    let payloads = [
        "http://internal-admin:8000/secrets",
        "http://10.7.0.169/latest/meta-data/iam/...",
        "http://0/secrets",
        "http://2130706433/",
        "http://[::1]:8000/",
        "http://ok.test@10.7.0.10/secrets",
        "http://a.rebind.lab.test/secrets",
    ];

    println!("Layer 7 · Topic 6 — SSRF: string blocklist vs resolve-and-pin\n");
    println!("   {:<44}{:<11}{:<13}{}", "payload", "blocklist", "resolve+pin", "resolved");
    let (mut rb, mut rp) = (0, 0);
    for url in payloads {
        let v1 = verdict_blocklist(url);
        let (v2, shown) = match canonical_ip(&host_of(url), &dns) {
            None => ("BLOCK", "unresolvable".to_string()),
            Some(ip) => (if is_denied(&ip) { "BLOCK" } else { "ALLOW" }, ip),
        };
        if v1 == "ALLOW" { rb += 1; }
        if v2 == "ALLOW" { rp += 1; }
        println!("   {:<44}{:<11}{:<13}{}", url, v1, v2, shown);
    }
    println!("\n   internal targets reached -- string_blocklist: {}/{}   resolve_and_pin: {}/{}",
             rb, payloads.len(), rp, payloads.len());
    println!("\nIMDS v1 vs v2: v1 returns credentials to a plain GET; v2 refuses without");
    println!("a PUT-obtained token -> 0 bytes. v2 raises the bar, it is not the fix.");
    println!("\nRead: the STRING is not the ADDRESS. reqwest::redirect::Policy::none() plus");
    println!("a custom Resolve keeps the address you validated the one you connect to.");
}
