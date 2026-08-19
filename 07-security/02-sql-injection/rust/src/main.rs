// Layer 7 · Topic 2 — SQL injection as a string-building failure (Rust / sqlx).
//
// `cargo run` (needs `cargo fetch` once online for sqlx, then a Postgres on
// localhost:5432). Bootstraps an ephemeral `sqli_lab`, seeds three users and
// a secret 32-char key, and runs the payload families in vulnerable and
// parameterized modes.
//
// Rust's answer is the most different in the set (README): the `query!` macro
// sends your SQL to a live database at COMPILE TIME, gets back parameter and
// result types, and fails the BUILD if they do not match -- the query is
// type-checked before the binary exists. Consequently the injectable version
// (`format!` into `sqlx::query`) is a departure from the ergonomic path, not
// the default. This file uses runtime `sqlx::query`/`bind` so it compiles
// without a database present; the compile-time-checked `query!` form is shown
// in the comment on `search_parameterized`.
//
// What to look for: tautology dumps all users when vulnerable, 0 rows (no
// error) when parameterized; UNION steals the key only when vulnerable; the
// blind channel recovers 32 chars in ~linear requests.
use sqlx::{Connection, PgConnection, Row};
use std::time::Instant;

const SECRET_KEY: &str = "S3CR3T_KEY_abcdef0123456789abcd0";
const CHARSET: &str =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_";

// THE BUG: attacker bytes formatted into the SQL text.
async fn search_vulnerable(c: &mut PgConnection, email: &str) -> Result<Vec<(i32, String, String)>, sqlx::Error> {
    let sql = format!("SELECT id, email, name FROM users WHERE email = '{email}'");
    let rows = sqlx::query(&sql).fetch_all(c).await?;
    Ok(rows.iter().map(|r| (r.get(0), r.get(1), r.get(2))).collect())
}

// Safe: the value rides in Bind. The compile-time-checked equivalent is
//   sqlx::query!("SELECT id, email, name FROM users WHERE email = $1", email)
// which would fail the BUILD if `users` or a column were misspelled.
async fn search_parameterized(c: &mut PgConnection, email: &str) -> Result<Vec<(i32, String, String)>, sqlx::Error> {
    let rows = sqlx::query("SELECT id, email, name FROM users WHERE email = $1")
        .bind(email)
        .fetch_all(c)
        .await?;
    Ok(rows.iter().map(|r| (r.get(0), r.get(1), r.get(2))).collect())
}

#[tokio::main]
async fn main() -> Result<(), sqlx::Error> {
    println!("Layer 7 · Topic 2 — SQL injection (Rust / sqlx, real Postgres)\n");

    let mut sc = match PgConnection::connect("postgres://localhost:5432/postgres").await {
        Ok(c) => c,
        Err(e) => {
            println!("Postgres unreachable -> {e}\nStart Postgres and re-run. Code is the artifact.");
            return Ok(());
        }
    };
    let _ = sqlx::query("DROP DATABASE IF EXISTS sqli_lab WITH (FORCE)").execute(&mut sc).await;
    sqlx::query("CREATE DATABASE sqli_lab").execute(&mut sc).await?;
    drop(sc);

    let mut c = PgConnection::connect("postgres://localhost:5432/sqli_lab").await?;
    sqlx::query("CREATE TABLE users (id int PRIMARY KEY, email text, name text)").execute(&mut c).await?;
    sqlx::query("CREATE TABLE api_keys (user_id int PRIMARY KEY, key text)").execute(&mut c).await?;
    for (i, n) in ["alice", "bob", "carol"].iter().enumerate() {
        sqlx::query("INSERT INTO users VALUES ($1,$2,$3)")
            .bind(i as i32 + 1).bind(format!("{n}@lab.test")).bind(*n)
            .execute(&mut c).await?;
    }
    sqlx::query("INSERT INTO api_keys VALUES (1,$1)").bind(SECRET_KEY).execute(&mut c).await?;

    println!("Payload 1 — boolean tautology  \"' OR '1'='1\"");
    let v = search_vulnerable(&mut c, "' OR '1'='1").await?;
    let p = search_parameterized(&mut c, "' OR '1'='1").await?;
    println!("   {:<14} -> {} rows: {:?}", "vulnerable", v.len(),
             v.iter().map(|r| r.2.clone()).collect::<Vec<_>>());
    println!("   {:<14} -> {} rows", "parameterized", p.len());

    println!("\nPayload 2 — UNION cross-table (steal api_keys.key)");
    let uni = "' UNION SELECT user_id, key, key FROM api_keys--";
    let v = search_vulnerable(&mut c, uni).await?;
    let leaked = v.iter().find(|r| r.1 == SECRET_KEY).map(|r| r.1.clone());
    println!("   {:<14} -> {} rows; secret leaked: {}", "vulnerable", v.len(),
             leaked.unwrap_or_else(|| "no".into()));
    let p = search_parameterized(&mut c, uni).await?;
    println!("   {:<14} -> {} rows; secret leaked: no", "parameterized", p.len());

    println!("\nPayload 3 — boolean-blind extraction of the 32-char key");
    let mut recovered = String::new();
    let mut requests = 0u32;
    let t0 = Instant::now();
    for pos in 1..=SECRET_KEY.len() {
        for ch in CHARSET.chars() {
            requests += 1;
            let payload = format!(
                "nope' OR substr((SELECT key FROM api_keys WHERE user_id=1),{pos},1)='{ch}");
            if !search_vulnerable(&mut c, &payload).await?.is_empty() {
                recovered.push(ch);
                break;
            }
        }
    }
    println!("   recovered: {recovered}");
    println!("   correct:   {}", recovered == SECRET_KEY);
    println!("   requests to recover {} chars, one char/request (measured): {}",
             SECRET_KEY.len(), requests);
    println!("   wall-clock: {} ms", t0.elapsed().as_millis());

    println!("\nTakeaway: the safe path binds; the compiler-checked query! form makes\n\
              the safe path the convenient one -- the only language here where that is true.");
    Ok(())
}
