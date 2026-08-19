// Layer 7 · Topic 4 — What a JWT is, and the revocation problem (Java).
//
// Runs on the JDK alone: `javac JwtDemo.java && java JwtDemo`. The README's
// library is Nimbus JOSE+JWT, which is not in this machine's offline cache;
// this file uses java.security (RSA) and javax.crypto.Mac (HMAC) directly. The
// Nimbus lesson holds and is in fact enforced structurally by this code: a
// JWSVerifier is algorithm-specific BY CONSTRUCTION -- an RSASSAVerifier
// cannot be tricked into HMAC. Below, `pinnedVerify` is that RSA-only verifier;
// `naiveVerify` is the mistake of choosing the algorithm from the token header.
//
// Three parts: (A) a JWT is signed, not encrypted; (B) forge HS256 with the
// RSA public key as the HMAC secret -- naive verifier accepts, pinned rejects;
// (C) revocation latency per strategy.
import java.nio.charset.StandardCharsets;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.PublicKey;
import java.security.Signature;
import java.util.Base64;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

public class JwtDemo {
    static final Base64.Encoder B64 = Base64.getUrlEncoder().withoutPadding();
    static final Base64.Decoder B64D = Base64.getUrlDecoder();

    static String enc(String s) { return B64.encodeToString(s.getBytes(StandardCharsets.UTF_8)); }

    static KeyPair rsa() throws Exception {
        KeyPairGenerator g = KeyPairGenerator.getInstance("RSA");
        g.initialize(2048);
        return g.generateKeyPair();
    }

    static String signRS256(String claimsJson, KeyPair kp) throws Exception {
        String input = enc("{\"alg\":\"RS256\",\"typ\":\"JWT\"}") + "." + enc(claimsJson);
        Signature sig = Signature.getInstance("SHA256withRSA");
        sig.initSign(kp.getPrivate());
        sig.update(input.getBytes(StandardCharsets.UTF_8));
        return input + "." + B64.encodeToString(sig.sign());
    }

    // Attacker path: HS256 keyed on the RSA public-key bytes.
    static String signHS256(String claimsJson, byte[] hmacKey) throws Exception {
        String input = enc("{\"alg\":\"HS256\",\"typ\":\"JWT\"}") + "." + enc(claimsJson);
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(hmacKey, "HmacSHA256"));
        return input + "." + B64.encodeToString(mac.doFinal(input.getBytes(StandardCharsets.UTF_8)));
    }

    static String algOf(String token) {
        String h = new String(B64D.decode(token.split("\\.")[0]), StandardCharsets.UTF_8);
        return h.contains("\"HS256\"") ? "HS256" : h.contains("\"RS256\"") ? "RS256" : "?";
    }

    // Naive: pick the verification algorithm from the token header (the bug).
    static String naiveVerify(String token, PublicKey pub, byte[] pubBytes) throws Exception {
        String[] p = token.split("\\.");
        String input = p[0] + "." + p[1];
        boolean ok;
        if (algOf(token).equals("RS256")) {
            Signature s = Signature.getInstance("SHA256withRSA");
            s.initVerify(pub);
            s.update(input.getBytes(StandardCharsets.UTF_8));
            ok = s.verify(B64D.decode(p[2]));
        } else { // HS256 -- uses the public key bytes as an HMAC secret
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(pubBytes, "HmacSHA256"));
            String expected = B64.encodeToString(mac.doFinal(input.getBytes(StandardCharsets.UTF_8)));
            ok = expected.equals(p[2]);
        }
        return ok ? "ACCEPTED role=" + role(p[1]) : "REJECTED";
    }

    // Pinned: RSA-only verifier, algorithm fixed by construction.
    static String pinnedVerify(String token, PublicKey pub) throws Exception {
        if (!algOf(token).equals("RS256")) return "REJECTED (alg != RS256)";
        String[] p = token.split("\\.");
        Signature s = Signature.getInstance("SHA256withRSA");
        s.initVerify(pub);
        s.update((p[0] + "." + p[1]).getBytes(StandardCharsets.UTF_8));
        return s.verify(B64D.decode(p[2])) ? "ACCEPTED role=" + role(p[1]) : "REJECTED";
    }

    static String role(String payloadSeg) {
        String j = new String(B64D.decode(payloadSeg), StandardCharsets.UTF_8);
        int i = j.indexOf("\"role\":\"");
        return i < 0 ? "?" : j.substring(i + 8, j.indexOf('"', i + 8));
    }

    static void partA() throws Exception {
        System.out.println("A. A JWT is signed, NOT encrypted");
        KeyPair kp = rsa();
        String token = signRS256("{\"sub\":\"alice\",\"role\":\"admin\",\"note\":\"not secret\"}", kp);
        System.out.println("   claims read with no key: " +
                new String(B64D.decode(token.split("\\.")[1]), StandardCharsets.UTF_8));
        System.out.println("   -> anyone holding the token reads every claim.\n");
    }

    static void partB() throws Exception {
        System.out.println("B. alg-confusion: forge HS256 with the RSA public key as the secret");
        KeyPair kp = rsa();
        byte[] pubBytes = kp.getPublic().getEncoded();
        String good = signRS256("{\"sub\":\"alice\",\"role\":\"user\"}", kp);
        String forged = signHS256("{\"sub\":\"alice\",\"role\":\"admin\"}", pubBytes);
        System.out.printf("   legit RS256, pinned [RS256]:                  %s%n", pinnedVerify(good, kp.getPublic()));
        System.out.printf("   forged HS256, naive verifier (pubkey as key): %s  <- the attack works%n",
                naiveVerify(forged, kp.getPublic(), pubBytes));
        System.out.printf("   forged HS256, pinned [RS256]:                 %s  <- safe%n", pinnedVerify(forged, kp.getPublic()));
        System.out.println("   An RSASSAVerifier (the pinned path) is algorithm-specific by\n" +
                "   construction and cannot be tricked into HMAC. Choosing the algorithm\n" +
                "   from the header is the entire bug.\n");
    }

    static void partC() {
        System.out.println("C. Revocation latency by strategy (poll every 50ms after logout)");
        final int ttl = 2000, logoutAt = 500, poll = 50;
        for (String strat : new String[]{"plain", "denylist", "opaque_introspect"}) {
            java.util.Set<String> denylist = new java.util.HashSet<>();
            boolean[] opaqueDead = {false};
            if (strat.equals("denylist")) denylist.add("tok-123");
            else if (strat.equals("opaque_introspect")) opaqueDead[0] = true;
            int latency = ttl - logoutAt;
            for (int now = logoutAt; now <= ttl; now += poll) {
                boolean dead = now >= ttl
                        || (strat.equals("denylist") && denylist.contains("tok-123"))
                        || (strat.equals("opaque_introspect") && opaqueDead[0]);
                if (dead) { latency = now - logoutAt; break; }
            }
            String note = strat.equals("plain") ? "= full remaining TTL" : "~ one poll interval";
            System.out.printf("   %-20s revocation latency: %4d ms   %s%n", strat, latency, note);
        }
        System.out.println("   (plain 'logout' invalidates a session /me never checks)\n");
    }

    public static void main(String[] args) throws Exception {
        System.out.println("Layer 7 · Topic 4 — JWT: not-encrypted, alg-confusion, revocation\n");
        partA(); partB(); partC();
        System.out.println("Takeaway: a stateless JWT trades revocability for statelessness. " +
                "Instant logout needs per-request server state -- a session by another name.");
    }
}
