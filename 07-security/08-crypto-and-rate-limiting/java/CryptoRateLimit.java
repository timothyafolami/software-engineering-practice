// Layer 7 · Topic 8 — Crypto hygiene and rate limiting (Java).
//
// JDK only: `javac CryptoRateLimit.java && java CryptoRateLimit`. The JVM twist
// (README) is unique: HotSpot profiles and recompiles hot methods, so a
// comparison's timing behaviour can CHANGE after a few thousand invocations. A
// timing experiment that skips warm-up measures the interpreter, not the
// deployed system -- so Part B warms both compares before timing. Java's
// constant-time primitive is MessageDigest.isEqual (constant-time for equal
// lengths); the slow KDF available in the JDK is PBKDF2 (argon2 needs a lib).
//
// Three parts, measured at runtime.
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.security.spec.KeySpec;
import java.util.HashMap;
import java.util.Map;
import javax.crypto.SecretKeyFactory;
import javax.crypto.spec.PBEKeySpec;

public class CryptoRateLimit {
    static long sink = 0;

    static void partA() throws Exception {
        System.out.println("A. Hash cost (verifications/sec, measured)");
        byte[] pw = "correct horse battery staple".getBytes();

        int reps = 500_000;
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        long t0 = System.nanoTime();
        for (int i = 0; i < reps; i++) { md.reset(); md.update(pw); md.digest(); }
        double shaVps = reps / ((System.nanoTime() - t0) / 1e9);
        System.out.printf("   sha256               %14.0f verify/sec%n", shaVps);

        // PBKDF2 at 600k iterations (OWASP's PBKDF2-HMAC-SHA256 baseline).
        SecureRandom sr = new SecureRandom();
        byte[] salt = new byte[16]; sr.nextBytes(salt);
        SecretKeyFactory f = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256");
        reps = 20;
        t0 = System.nanoTime();
        for (int i = 0; i < reps; i++) {
            KeySpec ks = new PBEKeySpec("pw".toCharArray(), salt, 600_000, 256);
            f.generateSecret(ks).getEncoded();
        }
        double pbVps = reps / ((System.nanoTime() - t0) / 1e9);
        System.out.printf("   pbkdf2(600k)         %14.1f verify/sec%n", pbVps);

        int N = 10_000, K = 1_000_000;
        System.out.printf("   crack-time model: attacker rig N=%dx, list K=%d candidates%n", N, K);
        System.out.printf("      sha256: %.6f s to first crack%n", K / (shaVps * N));
        System.out.printf("      pbkdf2: %.1f s to first crack  -- ~%.0fx slower per verify%n",
                K / (pbVps * N), shaVps / pbVps);
        System.out.println("   (argon2id is the OWASP first choice; it needs a library here.)\n");
    }

    static int naiveEq(byte[] a, byte[] b) {
        if (a.length != b.length) return 0;
        for (int i = 0; i < a.length; i++) if (a[i] != b[i]) return 0; // short-circuit
        return 1;
    }

    static void partB() {
        System.out.println("B. Timing signal: naive short-circuit vs constant-time");
        SecureRandom sr = new SecureRandom();
        byte[] secret = new byte[32]; sr.nextBytes(secret);

        java.util.function.IntFunction<byte[]> candidate = (matching) -> {
            byte[] c = new byte[32]; sr.nextBytes(c);
            System.arraycopy(secret, 0, c, 0, matching);
            if (matching < 32) c[matching] = (byte) (secret[matching] ^ 0xFF);
            return c;
        };

        // WARM-UP: force HotSpot to compile both paths before we measure.
        byte[] warm = candidate.apply(16);
        for (int i = 0; i < 200_000; i++) { sink += naiveEq(secret, warm); sink += MessageDigest.isEqual(secret, warm) ? 1 : 0; }

        System.out.println("   matching leading bytes ->        avg ns/op");
        String[] labels = {"naive_eq", "isEqual (constant)"};
        for (int variant = 0; variant < 2; variant++) {
            StringBuilder out = new StringBuilder(String.format("   %-20s", labels[variant]));
            for (int k : new int[]{0, 8, 16, 31}) {
                byte[] cand = candidate.apply(k);
                int reps = 3_000_000;
                long t0 = System.nanoTime();
                for (int i = 0; i < reps; i++)
                    sink += (variant == 0) ? naiveEq(secret, cand) : (MessageDigest.isEqual(secret, cand) ? 1 : 0);
                out.append(String.format(" k=%d:%.2f", k, (System.nanoTime() - t0) / (double) reps));
            }
            System.out.println(out);
        }
        System.out.println("   (naive trends up with k; isEqual flat -- AFTER warm-up. Skip warm-up");
        System.out.println("    and you measure the interpreter, not the deployed JIT-compiled code.)\n");
    }

    static void partC() {
        System.out.println("C. Rate limiting: attempts-to-first-success and effective limit");
        final int LIST = 1000, CORRECT_AT = 500, CONFIGURED = 10;
        String[][] rows = {
            {"off", "1", "1", "no limit"},
            {"redis_token_bucket", "1", "1", "shared bucket, configured=10"},
            {"inproc", "1", "1", "in-proc, 1 worker"},
            {"inproc", "4", "1", "in-proc, 4 workers -> effective 4x"},
            {"ip_keyed", "1", "50", "IP-keyed, attacker uses 50 IPs"},
        };
        for (String[] r : rows) {
            String mode = r[0]; int workers = Integer.parseInt(r[1]), ips = Integer.parseInt(r[2]);
            Map<String, Integer> buckets = new HashMap<>();
            int allowed = 0; boolean reached = false;
            for (int i = 1; i <= LIST; i++) {
                int ip = i % ips;
                boolean permitted;
                if (mode.equals("off")) permitted = true;
                else {
                    String key = mode.equals("redis_token_bucket") ? "account"
                        : mode.equals("inproc") ? ("w" + (i % workers)) : ("ip" + ip);
                    int b = buckets.getOrDefault(key, CONFIGURED);
                    permitted = b > 0;
                    if (permitted) buckets.put(key, b - 1);
                }
                if (permitted) { allowed++; if (i == CORRECT_AT) reached = true; }
            }
            System.out.printf("   %-18s %-34s allowed=%-4d %s%n", mode, r[3], allowed,
                    reached ? "reached password" : "password NOT reached");
        }
        System.out.printf("%n   effective/configured: inproc workers=4 allows ~%d vs configured %d -> 4x.%n",
                4 * CONFIGURED, CONFIGURED);
        System.out.println("   IP-keyed with 50 IPs lets the password through -> keying on IP is a fake fix.\n");
    }

    public static void main(String[] args) throws Exception {
        System.out.println("Layer 7 · Topic 8 — hash cost, timing signal, rate limiting\n");
        partA(); partB(); partC();
        if (sink == -1) System.out.println(sink); // keep sink live
        System.out.println("Takeaway: password hash must be SLOW, a secret compare CONSTANT-TIME, and " +
                "a rate limit keyed on the account with SHARED state.");
    }
}
