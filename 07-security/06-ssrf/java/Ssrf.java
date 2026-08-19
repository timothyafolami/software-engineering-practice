// Layer 7 · Topic 6 — SSRF: validate the connection, not the string (Java).
//
// Runs on the JDK alone: `javac Ssrf.java && java Ssrf`.
// The README's Java notes: java.net.http.HttpClient defaults to Redirect.NEVER
// (the SAFE default, the opposite of Python's requests), and the historical
// hazard is that the legacy java.net.URL does DNS resolution inside equals()
// and hashCode() -- so a validation cache keyed on URL can be poisoned by DNS.
// Use java.net.URI for anything not being fetched right now. This program uses
// URI to extract the host and InetAddress to classify the resolved literal.
//
// The finding: string_blocklist ALLOWs every internal target via an encoding
// it did not enumerate; resolve_and_pin BLOCKs them by classifying the IP.
import java.net.InetAddress;
import java.net.URI;
import java.util.LinkedHashMap;
import java.util.Map;

public class Ssrf {
    static final Map<String, String> FAKE_DNS = Map.of(
        "internal-admin", "10.7.0.10",
        "metadata", "10.7.0.169",
        "allowed.test", "93.184.216.34",
        "a.rebind.lab.test", "10.7.0.10",
        "localhost", "127.0.0.1");
    static final String[] STRING_DENY = {"localhost", "127.0.0.1", "169.254.169.254"};

    static String hostOf(String raw) {
        try {
            String h = URI.create(raw).getHost();
            if (h == null) return "";
            return h.replaceAll("^\\[|\\]$", ""); // strip IPv6 brackets
        } catch (Exception e) { return ""; }
    }

    static String canonicalIP(String host) {
        String h = FAKE_DNS.getOrDefault(host, host);
        if (h.matches("\\d+")) {                     // "0", "2130706433"
            long n = Long.parseLong(h) & 0xFFFFFFFFL;
            return String.format("%d.%d.%d.%d", (n >> 24) & 255, (n >> 16) & 255, (n >> 8) & 255, n & 255);
        }
        if (h.matches("\\d+\\.\\d+\\.\\d+\\.\\d+") || h.contains(":")) return h;
        return null; // unknown name
    }

    static boolean isDenied(String ip) {
        try {
            InetAddress a = InetAddress.getByName(ip); // literal IP -> no DNS
            return a.isLoopbackAddress() || a.isSiteLocalAddress()
                || a.isLinkLocalAddress() || a.isAnyLocalAddress();
        } catch (Exception e) { return true; } // fail closed
    }

    static String verdictBlocklist(String raw) {
        String low = raw.toLowerCase();
        for (String d : STRING_DENY) if (low.contains(d)) return "BLOCK";
        return "ALLOW";
    }

    static String[] verdictResolvePin(String raw) {
        String ip = canonicalIP(hostOf(raw));
        if (ip == null) return new String[]{"BLOCK", "unresolvable"};
        return new String[]{isDenied(ip) ? "BLOCK" : "ALLOW", ip};
    }

    public static void main(String[] args) {
        Map<String, String> payloads = new LinkedHashMap<>();
        payloads.put("http://internal-admin:8000/secrets", "plain internal reach");
        payloads.put("http://10.7.0.169/latest/meta-data/iam/...", "credential theft");
        payloads.put("http://0/secrets", "0 == 0.0.0.0");
        payloads.put("http://2130706433/", "decimal 127.0.0.1");
        payloads.put("http://[::1]:8000/", "IPv6 loopback");
        payloads.put("http://ok.test@10.7.0.10/secrets", "userinfo confusion");
        payloads.put("http://a.rebind.lab.test/secrets", "DNS rebinding");

        System.out.println("Layer 7 · Topic 6 — SSRF: string blocklist vs resolve-and-pin\n");
        System.out.printf("   %-44s%-11s%-13s%s%n", "payload", "blocklist", "resolve+pin", "resolved");
        int rb = 0, rp = 0;
        for (String url : payloads.keySet()) {
            String v1 = verdictBlocklist(url);
            String[] v2 = verdictResolvePin(url);
            if (v1.equals("ALLOW")) rb++;
            if (v2[0].equals("ALLOW")) rp++;
            System.out.printf("   %-44s%-11s%-13s%s%n", url, v1, v2[0], v2[1]);
        }
        System.out.printf("%n   internal targets reached -- string_blocklist: %d/%d   resolve_and_pin: %d/%d%n",
            rb, payloads.size(), rp, payloads.size());
        System.out.println("\nIMDS v1 vs v2: v1 returns credentials to a plain GET; v2 refuses without");
        System.out.println("a PUT-obtained token -> 0 bytes. v2 raises the bar, it is not the fix.");
        System.out.println("\nRead: the STRING is not the ADDRESS. Classify the RESOLVED IP; keep URI");
        System.out.println("out of hash-based caches, because java.net.URL resolves DNS in equals().");
    }
}
