// Layer 7 · Topic 2 — SQL injection as a string-building failure (Go / pgx).
//
// One command: `go run sqli.go` (needs a reachable Postgres; the lab machine
// has one on localhost:5432). Bootstraps an ephemeral `sqli_lab` database,
// seeds three users and a secret 32-char key, and runs the four payload
// families against a /search endpoint in vulnerable and parameterized modes.
//
// Go's specifics (README): pgx uses the extended protocol -- Query(ctx, sql,
// args...) prepares and binds, sending values in the Bind message. The trap
// is the placeholder dialect: database/sql documents `?`, Postgres wants
// `$1`; people who switch drivers reach for the wrong token, get an error,
// and "fix" it by building the string with fmt.Sprintf -- which is a VISIBLE
// extra call, so at least the vulnerable version is harder to write by
// accident than an f-string's two characters.
//
// What to look for: tautology dumps all users when vulnerable, 0 rows (no
// error) when parameterized; UNION steals the key only when vulnerable; the
// blind channel recovers 32 chars in ~linear requests.
package main

import (
	"context"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

const (
	superDSN  = "postgres://localhost:5432/postgres"
	labDSN    = "postgres://localhost:5432/sqli_lab"
	secretKey = "S3CR3T_KEY_abcdef0123456789abcd0" // 32 chars
	charset   = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
)

func bootstrap(ctx context.Context) error {
	sc, err := pgx.Connect(ctx, superDSN)
	if err != nil {
		return err
	}
	sc.Exec(ctx, "DROP DATABASE IF EXISTS sqli_lab WITH (FORCE)")
	if _, err = sc.Exec(ctx, "CREATE DATABASE sqli_lab"); err != nil {
		return err
	}
	sc.Close(ctx)

	c, err := pgx.Connect(ctx, labDSN)
	if err != nil {
		return err
	}
	defer c.Close(ctx)
	c.Exec(ctx, "CREATE TABLE users (id int PRIMARY KEY, email text, name text)")
	c.Exec(ctx, "CREATE TABLE api_keys (user_id int PRIMARY KEY, key text)")
	for i, n := range []string{"alice", "bob", "carol"} {
		c.Exec(ctx, "INSERT INTO users VALUES ($1,$2,$3)", i+1, n+"@lab.test", n)
	}
	_, err = c.Exec(ctx, "INSERT INTO api_keys VALUES (1, $1)", secretKey)
	return err
}

type row struct{ id int; email, name string }

func query(ctx context.Context, c *pgx.Conn, sql string, args ...any) ([]row, error) {
	rs, err := c.Query(ctx, sql, args...)
	if err != nil {
		return nil, err
	}
	defer rs.Close()
	var out []row
	for rs.Next() {
		var r row
		if err := rs.Scan(&r.id, &r.email, &r.name); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rs.Err()
}

// THE BUG: fmt.Sprintf into the query -- a visible extra call, and the leak.
func searchVulnerable(ctx context.Context, c *pgx.Conn, email string) ([]row, error) {
	return query(ctx, c, fmt.Sprintf("SELECT id, email, name FROM users WHERE email = '%s'", email))
}
func searchParameterized(ctx context.Context, c *pgx.Conn, email string) ([]row, error) {
	return query(ctx, c, "SELECT id, email, name FROM users WHERE email = $1", email)
}

func main() {
	ctx := context.Background()
	fmt.Println("Layer 7 · Topic 2 — SQL injection (Go / pgx, real Postgres)")
	fmt.Println()
	if err := bootstrap(ctx); err != nil {
		fmt.Printf("Postgres unreachable -> %v\nStart Postgres and re-run. Code is the artifact.\n", err)
		os.Exit(0)
	}
	c, _ := pgx.Connect(ctx, labDSN)
	defer c.Close(ctx)

	fmt.Println(`Payload 1 — boolean tautology  "' OR '1'='1"`)
	for _, v := range []struct {
		label string
		fn    func(context.Context, *pgx.Conn, string) ([]row, error)
	}{{"vulnerable", searchVulnerable}, {"parameterized", searchParameterized}} {
		rows, err := v.fn(ctx, c, "' OR '1'='1")
		if err != nil {
			fmt.Printf("   %-14s -> ERROR: %v\n", v.label, err)
			continue
		}
		names := make([]string, len(rows))
		for i, r := range rows {
			names[i] = r.name
		}
		fmt.Printf("   %-14s -> %d rows: %v\n", v.label, len(rows), names)
	}

	fmt.Println("\nPayload 2 — UNION cross-table (steal api_keys.key)")
	union := "' UNION SELECT user_id, key, key FROM api_keys--"
	for _, v := range []struct {
		label string
		fn    func(context.Context, *pgx.Conn, string) ([]row, error)
	}{{"vulnerable", searchVulnerable}, {"parameterized", searchParameterized}} {
		rows, err := v.fn(ctx, c, union)
		if err != nil {
			fmt.Printf("   %-14s -> ERROR: %v\n", v.label, err)
			continue
		}
		leaked := "no"
		for _, r := range rows {
			if r.email == secretKey {
				leaked = r.email
			}
		}
		fmt.Printf("   %-14s -> %d rows; secret leaked: %s\n", v.label, len(rows), leaked)
	}

	fmt.Println("\nPayload 4 — identifier injection on ORDER BY (cannot be bound)")
	sort := "(SELECT key FROM api_keys LIMIT 1)"
	rows, err := query(ctx, c, fmt.Sprintf("SELECT id, email, name FROM users ORDER BY %s", sort))
	if err != nil {
		fmt.Printf("   parameterized* -> %v\n", err)
	} else {
		fmt.Printf("   parameterized* -> %d rows (injection ran in ORDER BY!)\n", len(rows))
	}
	allowed := map[string]bool{"id": true, "email": true, "name": true}
	if !allowed[sort] {
		fmt.Printf("   allowlist      -> rejected identifier %q (not in allowlist)\n", sort)
	}
	fmt.Println("   *parameterizing WHERE does not close ORDER BY: the column " +
		"position is fixed at Parse time, before any Bind value exists.")

	// Part C — blind extraction
	fmt.Println("\nPayload 3 — boolean-blind extraction of the 32-char key")
	var recovered strings.Builder
	requests := 0
	t0 := time.Now()
	for pos := 1; pos <= len(secretKey); pos++ {
		for _, ch := range charset {
			requests++
			payload := fmt.Sprintf("nope' OR substr((SELECT key FROM api_keys WHERE user_id=1),%d,1)='%c", pos, ch)
			rows, _ := searchVulnerable(ctx, c, payload)
			if len(rows) > 0 {
				recovered.WriteRune(ch)
				break
			}
		}
	}
	ms := time.Since(t0).Milliseconds()
	n := len(secretKey)
	linear := n * (len(charset) + 1) / 2
	binsearch := n * 7
	got := recovered.String()
	fmt.Printf("   recovered: %s\n", got)
	fmt.Printf("   correct:   %v\n", got == secretKey)
	fmt.Printf("   requests to recover %d chars, one char/request (measured): %d\n", n, requests)
	fmt.Printf("   wall-clock: %d ms\n", ms)
	fmt.Printf("   theory: linear ~%d req; binary-search per char ~%d req (ratio ~%.1fx)\n",
		linear, binsearch, float64(linear)/float64(binsearch))

	fmt.Println("\nTakeaway: pgx binds via the extended protocol; the value rides " +
		"in Bind, never Parse. Same move as Topic 3 escaping and command-injection " +
		"defence: attacker bytes must never reach a parser as syntax.")
}
