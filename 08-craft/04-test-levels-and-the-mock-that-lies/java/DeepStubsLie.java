// Layer 8 Topic 4 - Java: RETURNS_DEEP_STUBS, and the object graph the mock
// invented for you.
//
// WHAT THIS DEMONSTRATES: Mockito's deep stubs make `a.getB().getC().getD()`
// return a live mock at every hop. That is the exact failure mode of Python's
// AsyncMock, with better tooling around it -- and one extra property that
// AsyncMock does not have and that is easy to miss: the chain is CACHED, so
// `s.getRepository() == s.getRepository()` is true on the double. Real sessions
// are under no such obligation, and a `verify` written against the cached hop is
// asserting about an object production would never have used.
//
// Mockito is not installed here and is not needed. `deep(...)` below is
// java.lang.reflect.Proxy plus twenty lines, which is what RETURNS_DEEP_STUBS
// is: unstubbed interface-returning methods hand back another proxy, collections
// come back empty, primitives come back zero. (Mockito's other party trick --
// mocking statics and finals via mockito-inline's bytecode agent -- is real and
// is NOT reproduced here; it needs an agent, and this file makes no claim about
// it beyond naming it.)
//
// WHAT TO LOOK FOR: SUITE A is four green assertions. Then SUITE B runs the same
// four questions against the real objects: the repository is null, the graph was
// never wired, and the transaction was never committed. Assertion 2 in SUITE A
// is the sharp one -- `assertNotNull(session.getRepository())` is the sort of
// line that gets written to "make the test more thorough", and it can only ever
// pass.
//
//   cd java && javac DeepStubsLie.java -d /tmp/t4java && java -cp /tmp/t4java DeepStubsLie

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public class DeepStubsLie {

    // --- the object graph the code under test walks -------------------------

    interface Session {
        Repository getRepository();
        Transaction getTransaction();
    }

    interface Repository {
        OrderTable getOrders();
    }

    interface OrderTable {
        List<Integer> recent(int customerId, int limit);
        void markArchived(int id);
    }

    interface Transaction {
        void commit();
        boolean isCommitted();
    }

    // --- the code under test ------------------------------------------------

    /** Four hops. Every one of them is a place the real graph can be unwired. */
    static List<Integer> recentIds(Session s, int customerId, int limit) {
        return s.getRepository().getOrders().recent(customerId, limit);
    }

    /** THE BUG: marks the row, opens nothing, commits nothing. */
    static void archive(Session s, int id) {
        s.getRepository().getOrders().markArchived(id);
        // s.getTransaction().commit();   <-- the missing line
    }

    // --- RETURNS_DEEP_STUBS, in twenty lines --------------------------------

    static final class DeepStub implements InvocationHandler {
        private final String path;
        private final Map<String, Object> children = new HashMap<>();
        private final Map<String, Object> stubbed = new HashMap<>();
        private final List<String> calls = new ArrayList<>();

        DeepStub(String path) { this.path = path; }

        @Override
        public Object invoke(Object proxy, Method m, Object[] args) {
            String name = m.getName();
            switch (name) {
                case "toString": return "DeepStub(" + path + ")";
                case "hashCode": return System.identityHashCode(proxy);
                case "equals":   return proxy == args[0];
                default: break;
            }
            calls.add(name + Arrays.toString(args == null ? new Object[0] : args));

            if (stubbed.containsKey(name)) return stubbed.get(name);

            Class<?> rt = m.getReturnType();
            if (Collection.class.isAssignableFrom(rt)) return new ArrayList<>();
            if (rt.isInterface()) {
                // The whole mechanism. An unstubbed hop is not an error and is
                // not null -- it is ANOTHER live double, cached so that the
                // second call returns the same one. That caching is what makes
                // `verify(s.getRepository().getOrders())` appear to work.
                return children.computeIfAbsent(name, k -> proxyFor(rt, path + "." + k));
            }
            if (rt == int.class || rt == long.class) return 0;
            if (rt == boolean.class) return false;
            return null;
        }

        boolean sawCall(String name, Object... args) {
            return calls.contains(name + Arrays.toString(args));
        }

        List<String> calls() { return calls; }
    }

    static Object proxyFor(Class<?> iface, String path) {
        return Proxy.newProxyInstance(iface.getClassLoader(), new Class<?>[]{iface}, new DeepStub(path));
    }

    @SuppressWarnings("unchecked")
    static <T> T deep(Class<T> iface) {
        return (T) proxyFor(iface, iface.getSimpleName());
    }

    static DeepStub handlerOf(Object proxy) {
        return (DeepStub) Proxy.getInvocationHandler(proxy);
    }

    /** `when(mock.method(...)).thenReturn(value)`, argument-insensitive. */
    static void stub(Object proxy, String method, Object value) {
        handlerOf(proxy).stubbed.put(method, value);
    }

    // --- the real objects ---------------------------------------------------

    /** A session that was constructed without being bound to a repository. */
    record RealSession(Repository repo, Transaction tx) implements Session {
        public Repository getRepository() { return repo; }
        public Transaction getTransaction() { return tx; }
    }

    static final class RealTransaction implements Transaction {
        private int commits = 0;
        public void commit() { commits++; }
        public boolean isCommitted() { return commits > 0; }
        int commits() { return commits; }
    }

    // --- a two-line harness -------------------------------------------------

    static final class Suite {
        int passed, failed;
        void check(String what, boolean ok, String detail) {
            if (ok) passed++; else failed++;
            System.out.printf("  [%s] %-32s %s%n", ok ? "PASS" : "FAIL", what, detail);
        }
        void report() { System.out.printf("  ----> %d/%d pass%n", passed, passed + failed); }
    }

    static void printCalls(String owner, DeepStub h) {
        Map<String, Integer> counts = new java.util.LinkedHashMap<>();
        for (String c : h.calls()) counts.merge(c, 1, Integer::sum);
        counts.forEach((sig, n) -> System.out.printf("  %s.%-24s x%d%n", owner, sig, n));
    }

    static String describe(Runnable r) {
        try {
            r.run();
            return "returned normally";
        } catch (Throwable t) {
            return t.getClass().getSimpleName() + (t.getMessage() == null ? "" : ": " + t.getMessage());
        }
    }

    // --- the two suites -----------------------------------------------------

    public static void main(String[] args) {
        System.out.println("Layer 8 topic 4 - Java: the object graph RETURNS_DEEP_STUBS invented.");

        System.out.println("\nSUITE A - Mockito-style deep stubs");
        Session mocked = deep(Session.class);
        // One line of setup reaches three hops down. Note what it did on the way:
        // it CREATED a Repository double and an OrderTable double, and nothing
        // anywhere asserted that the real Session can produce either.
        stub(mocked.getRepository().getOrders(), "recent", List.of(5, 4, 3, 2));

        // Held in locals from here on, because reading the log THROUGH the chain
        // adds to the log -- a small reflexivity that is worth noticing once.
        Repository mockedRepo = mocked.getRepository();
        OrderTable mockedOrders = mockedRepo.getOrders();

        Suite a = new Suite();
        List<Integer> got = recentIds(mocked, 1, 4);
        a.check("recent orders, newest first", got.equals(List.of(5, 4, 3, 2)), "got " + got);
        a.check("the session has a repository", mockedRepo != null,
                "getRepository() -> " + mockedRepo);
        archive(mocked, 7);
        a.check("archive marks the row",
                handlerOf(mockedOrders).sawCall("markArchived", 7),
                "verify(orders).markArchived(7)");
        a.check("a transaction is available", mocked.getTransaction() != null,
                "getTransaction() -> " + mocked.getTransaction());
        a.report();

        System.out.println("\nSUITE B - the same four questions, asked of the real objects");
        RealTransaction tx = new RealTransaction();
        Session real = new RealSession(null, tx);   // never bound: the shipped bug

        Suite b = new Suite();
        b.check("recent orders, newest first", false, describe(() -> recentIds(real, 1, 4)));
        b.check("the session has a repository", real.getRepository() != null,
                "getRepository() -> " + real.getRepository());
        b.check("archive marks the row", false, describe(() -> archive(real, 7)));
        b.check("a transaction is committed", tx.isCommitted(),
                "commit() called " + tx.commits() + " times");
        b.report();

        System.out.println("\nTHE CACHING NOBODY ASKED FOR");
        System.out.printf("  deep stub:  session.getRepository() == session.getRepository()  -> %b%n",
                mocked.getRepository() == mocked.getRepository());
        Session freshEachTime = new Session() {
            public Repository getRepository() {                 // a perfectly legal
                return new Repository() {                       // real implementation
                    public OrderTable getOrders() { return null; }
                };
            }
            public Transaction getTransaction() { return tx; }
        };
        System.out.printf("  real:       session.getRepository() == session.getRepository()  -> %b%n",
                freshEachTime.getRepository() == freshEachTime.getRepository());
        System.out.println("  `verify(session.getRepository().getOrders()).markArchived(7)` only");
        System.out.println("  works because the double cached the hop. The identity it relies on");
        System.out.println("  is an invariant of the mock, not of the interface.");

        System.out.println("\nEVERY CALL THE DEEP STUB ACCEPTED, AND HOW OFTEN");
        printCalls("Session", handlerOf(mocked));
        printCalls("Repository", handlerOf(mockedRepo));
        printCalls("OrderTable", handlerOf(mockedOrders));
        System.out.println("  Not one of them was declared in advance, and not one could fail.");

        System.out.printf("%nSUMMARY  suite A %d/%d   suite B %d/%d%n",
                a.passed, a.passed + a.failed, b.passed, b.passed + b.failed);
        System.out.println("Four green assertions, and the shipped session cannot serve one request.");
        System.out.println("Java holds both ends of this topic: the most powerful mocking library");
        System.out.println("in any of these six languages, and the community that responded by");
        System.out.println("building testcontainers.");
    }
}
