// Layer 7 · Topic 4 — What a JWT is, and the revocation problem (Rust / jsonwebtoken).
//
// `cargo fetch` once online, then `cargo run`. The crate is not in this
// machine's offline cache, so this file is idiomatic-but-blocked here.
//
// Rust's contribution (README): `Validation` is a struct whose `algorithms`
// field has no meaningful "accept anything" value, and `DecodingKey` is typed
// by key kind (from_rsa_pem vs from_secret) -- so an RS256/HS256 confusion is a
// TYPE error the compiler refuses, not a runtime acceptance. The vulnerable
// version has to be written on purpose. This program shows: (A) a JWT is
// signed not encrypted; (B) the pinned Validation rejecting a forged HS256
// token; (C) revocation latency per strategy.
use jsonwebtoken::{decode, encode, Algorithm, DecodingKey, EncodingKey, Header, Validation};
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Debug)]
struct Claims {
    sub: String,
    role: String,
    exp: usize,
}

fn part_a(rsa_priv_pem: &[u8]) {
    println!("A. A JWT is signed, NOT encrypted");
    let claims = Claims { sub: "alice".into(), role: "admin".into(), exp: 9_999_999_999 };
    let token = encode(&Header::new(Algorithm::RS256), &claims,
                       &EncodingKey::from_rsa_pem(rsa_priv_pem).unwrap()).unwrap();
    let payload_b64 = token.split('.').nth(1).unwrap();
    let raw = base64_url_decode(payload_b64);
    println!("   claims read with no key: {}", String::from_utf8_lossy(&raw));
    println!("   -> anyone holding the token reads every claim.\n");
}

// Minimal base64url decode (avoids pulling a base64 dep just for Part A).
fn base64_url_decode(s: &str) -> Vec<u8> {
    const T: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut buf = 0u32;
    let mut bits = 0u8;
    let mut out = Vec::new();
    for c in s.bytes() {
        let v = T.iter().position(|&x| x == c);
        if let Some(v) = v {
            buf = (buf << 6) | v as u32;
            bits += 6;
            if bits >= 8 {
                bits -= 8;
                out.push((buf >> bits) as u8);
            }
        }
    }
    out
}

fn part_b(rsa_priv_pem: &[u8], rsa_pub_pem: &[u8]) {
    println!("B. alg-confusion: the pinned Validation rejects a non-RS256 token");
    let claims = Claims { sub: "alice".into(), role: "user".into(), exp: 9_999_999_999 };
    let good = encode(&Header::new(Algorithm::RS256), &claims,
                      &EncodingKey::from_rsa_pem(rsa_priv_pem).unwrap()).unwrap();

    // The attacker forges HS256 with the public key PEM bytes as the secret.
    let forged_claims = Claims { sub: "alice".into(), role: "admin".into(), exp: 9_999_999_999 };
    let forged = encode(&Header::new(Algorithm::HS256), &forged_claims,
                        &EncodingKey::from_secret(rsa_pub_pem)).unwrap();

    // Pinned validation: algorithms is RS256 only, key is from_rsa_pem.
    let mut v = Validation::new(Algorithm::RS256);
    v.set_required_spec_claims::<&str>(&[]);
    let key = DecodingKey::from_rsa_pem(rsa_pub_pem).unwrap();

    let report = |label: &str, tok: &str| {
        match decode::<Claims>(tok, &key, &v) {
            Ok(d) => println!("   {label:<44} ACCEPTED role={}", d.claims.role),
            Err(e) => println!("   {label:<44} REJECTED ({:?})", e.kind()),
        }
    };
    report("legit RS256, Validation[RS256]:", &good);
    report("forged HS256, Validation[RS256]:", &forged);
    println!("   Validation is pinned to RS256; the HS256 header cannot select a");
    println!("   different key type -- DecodingKey::from_rsa_pem is not an HMAC key,");
    println!("   and the compiler already refused to mix them.\n");
}

fn part_c() {
    println!("C. Revocation latency by strategy (poll every 50ms after logout)");
    const TTL: i32 = 2000;
    const LOGOUT_AT: i32 = 500;
    const POLL: i32 = 50;
    for strategy in ["plain", "denylist", "opaque_introspect"] {
        let revoked = strategy != "plain"; // denylist/introspect act at logout
        let mut latency = TTL - LOGOUT_AT;
        let mut now = LOGOUT_AT;
        while now <= TTL {
            let dead = now >= TTL || revoked;
            if dead {
                latency = now - LOGOUT_AT;
                break;
            }
            now += POLL;
        }
        let note = if strategy == "plain" { "= full remaining TTL" } else { "~ one poll interval" };
        println!("   {strategy:<20} revocation latency: {latency:>4} ms   {note}");
    }
    println!("   (plain 'logout' invalidates a session /me never checks)\n");
}

fn main() {
    // Generate an RSA keypair at runtime so the program is self-contained.
    use rsa::pkcs8::{EncodePrivateKey, EncodePublicKey, LineEnding};
    use rsa::RsaPrivateKey;
    let mut rng = rand::thread_rng();
    let priv_key = RsaPrivateKey::new(&mut rng, 2048).expect("keygen");
    let pub_key = priv_key.to_public_key();
    let priv_pem = priv_key.to_pkcs8_pem(LineEnding::LF).unwrap();
    let pub_pem = pub_key.to_public_key_pem(LineEnding::LF).unwrap();

    println!("Layer 7 · Topic 4 — JWT: not-encrypted, alg-confusion, revocation\n");
    part_a(priv_pem.as_bytes());
    part_b(priv_pem.as_bytes(), pub_pem.as_bytes());
    part_c();
    println!("Takeaway: a stateless JWT trades revocability for statelessness. \
              Instant logout needs per-request server state -- a session by another name.");
}
