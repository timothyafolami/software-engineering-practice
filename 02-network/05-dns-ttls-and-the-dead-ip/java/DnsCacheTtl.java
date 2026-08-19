// Layer 2 · Topic 5 - Java: the one runtime in this topic that caches by
// default, and the folklore that grew around it.
//
// Python, Node, Go and Rust cache nothing in-process. The JVM does, in
// InetAddress, controlled by two security properties:
//
//     networkaddress.cache.ttl           successful lookups
//     networkaddress.cache.negative.ttl  failures
//
// They live in $JAVA_HOME/conf/security/java.security, not in your code and
// not in a system property you can set on the command line by that name. Older
// JDK behaviour cached successful lookups FOREVER when a security manager was
// installed, which is why "the JVM caches DNS forever" is folklore in Java
// shops and not in Python ones -- and why AWS's SDK documentation has spent a
// decade telling people to set that property before a failover eats them.
//
// This program reads the values off the JDK you are actually running, then
// measures the cache doing its job, then measures it AFTER an address change
// so you can see the stale window with your own timings.
//
// It deliberately quotes no default. Read yours below; the number in any blog
// post, including the ones this file could have copied, is a number for
// somebody else's JDK.
//
// What to look for in the output:
//   - the two property values, and whether they are set at all. "not set"
//     means the JDK's built-in default applies, which is version-dependent.
//   - repeated lookups after the first: near-zero, because they never leave
//     the process. That is the cache.
//   - the stale window: how long InetAddress keeps handing you the OLD
//     address after the mapping underneath it has changed.
//
// Compile & run:
//   javac DnsCacheTtl.java -d /tmp/javabuild && java -cp /tmp/javabuild DnsCacheTtl
//
// Then run it again with the cache disabled and compare:
//   java -Dsun.net.inetaddr.ttl=0 -cp /tmp/javabuild DnsCacheTtl

import java.net.InetAddress;
import java.net.UnknownHostException;
import java.security.Security;
import java.util.ArrayList;
import java.util.List;

public class DnsCacheTtl {

    static final String NAME = "example.com";
    static final int LOOKUPS = 6;

    static String prop(String key) {
        String v = Security.getProperty(key);
        return v == null || v.isEmpty() ? "not set (the JDK's built-in default applies)" : v;
    }

    record Timed(String address, double ms, boolean failed) { }

    static Timed lookup(String name) {
        long t0 = System.nanoTime();
        try {
            InetAddress a = InetAddress.getByName(name);
            return new Timed(a.getHostAddress(), (System.nanoTime() - t0) / 1e6, false);
        } catch (UnknownHostException e) {
            return new Timed("UnknownHostException", (System.nanoTime() - t0) / 1e6, true);
        }
    }

    public static void main(String[] args) throws Exception {
        System.out.println("=".repeat(78));
        System.out.println("Java: the runtime that caches, and the two properties that decide for how long");
        System.out.println("=".repeat(78));
        System.out.printf("  JDK %s (%s)%n", System.getProperty("java.version"), System.getProperty("java.vendor"));
        System.out.printf("  java.home %s%n%n", System.getProperty("java.home"));

        System.out.println("  The properties, read off THIS JDK:");
        System.out.printf("    networkaddress.cache.ttl           %s%n", prop("networkaddress.cache.ttl"));
        System.out.printf("    networkaddress.cache.negative.ttl  %s%n", prop("networkaddress.cache.negative.ttl"));
        System.out.println("    (they are in $JAVA_HOME/conf/security/java.security -- go and look,");
        System.out.println("     because the shipped file has the defaults written in comments next");
        System.out.println("     to them and those comments are the only authoritative source)");
        System.out.println();
        System.out.printf("  Legacy system properties, if set on this JVM:%n");
        System.out.printf("    sun.net.inetaddr.ttl               %s%n",
                System.getProperty("sun.net.inetaddr.ttl", "not set"));
        System.out.printf("    sun.net.inetaddr.negative.ttl      %s%n",
                System.getProperty("sun.net.inetaddr.negative.ttl", "not set"));
        System.out.println();

        // ---- successful lookups -------------------------------------------
        System.out.printf("  A. %s, %d times in a row%n", NAME, LOOKUPS);
        List<Timed> ok = new ArrayList<>();
        for (int i = 0; i < LOOKUPS; i++) {
            Timed t = lookup(NAME);
            ok.add(t);
            System.out.printf("      lookup %d  %8.3f ms  %s%n", i, t.ms(), t.address());
        }
        double first = ok.get(0).ms();
        double rest = ok.stream().skip(1).mapToDouble(Timed::ms).average().orElse(0);
        System.out.printf("      first %.3f ms, mean of the rest %.3f ms%n", first, rest);
        if (rest < first / 5 && first > 1.0) {
            System.out.println("      -> the later lookups never left this process. That is the JVM cache,");
            System.out.println("         and it is honouring networkaddress.cache.ttl, NOT the record's TTL.");
        } else {
            System.out.println("      -> no clear caching signal in this run. Either the OS resolver");
            System.out.println("         answered the first one from ITS cache too, or the JVM cache is");
            System.out.println("         disabled here. Check the ttl value printed above before");
            System.out.println("         concluding anything -- this is a recordable result, not a failure.");
        }
        System.out.println();

        // ---- negative lookups ---------------------------------------------
        String missing = "does-not-exist-" + System.nanoTime() + ".example.com";
        System.out.println("  B. A name that does not exist, twice");
        Timed n1 = lookup(missing);
        Timed n2 = lookup(missing);
        System.out.printf("      first   %8.3f ms  %s%n", n1.ms(), n1.address());
        System.out.printf("      second  %8.3f ms  %s%n", n2.ms(), n2.address());
        System.out.println("      Negative answers have their own TTL, and it is the one that bites");
        System.out.println("      during a deploy: a client that looked up a service name one second");
        System.out.println("      too early caches the failure and keeps failing after the service");
        System.out.println("      exists. If negative.ttl is 10 on your JDK, that is a ten-second");
        System.out.println("      outage manufactured entirely inside your own process.");
        System.out.println();

        // ---- the stale window ---------------------------------------------
        System.out.println("  C. What the cache costs you when the address changes");
        System.out.println("      This half cannot be measured honestly on a laptop: it needs a name");
        System.out.println("      whose address you can actually move. Run it in the lab, where you");
        System.out.println("      can, and time it:");
        System.out.println();
        System.out.println("        docker compose up -d upstream_b");
        System.out.println("        docker network disconnect lab_default upstream");
        System.out.println("        # then watch how long this JVM keeps returning the old address");
        System.out.println();
        System.out.println("      The arithmetic to predict it first: your stale window is at worst");
        System.out.println("      networkaddress.cache.ttl PLUS the record TTL still held by every");
        System.out.println("      resolver between you and the authority -- and then, on top of both,");
        System.out.println("      for as long as you hold an open connection to the old address,");
        System.out.println("      FOREVER, because an established socket asks DNS nothing.");
        System.out.println();

        System.out.println("  The conclusion this topic keeps arriving at, from a sixth direction:");
        System.out.println("    Java is the only runtime here with a real in-process DNS cache, and it");
        System.out.println("    is STILL not the thing that kept your service talking to a dead");
        System.out.println("    address. Set networkaddress.cache.ttl to something small and sane, by");
        System.out.println("    all means -- and then go and set a maximum connection lifetime,");
        System.out.println("    because that is the setting that actually bounds the outage.");
    }
}
