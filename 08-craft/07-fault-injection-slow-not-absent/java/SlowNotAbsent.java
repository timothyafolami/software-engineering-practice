// Layer 8 Topic 7 - Java: pool wait as a first-class number, and the Loom
// migration that made things worse.
//
// WHAT THIS DEMONSTRATES: the same fault as the lab's toxiproxy ladder -- the
// dependency answers correctly, 20x slower -- run against four configurations
// that differ only in WHERE THE BOUND IS. The dependency is a real TCP server on
// loopback with its own connection ceiling, which is this file's stand-in for
// Postgres `max_connections`. Load is offered at a fixed arrival rate; every
// latency is measured from the request's SCHEDULED time, so queueing at the
// generator is counted rather than erased.
//
//   A  platform threads (16), fast dependency          -- baseline
//   B  platform threads (16), slow dependency          -- the THREAD POOL binds
//   C  virtual threads, slow dependency, same big pool -- the DATABASE binds
//   D  virtual threads, pool sized under the database's ceiling, bounded waits
//
// The finding worth sitting with is B -> C. Nothing about the dependency changed
// between them. Removing the thread limit did not remove the queue, it MOVED it:
// off the executor, onto the connection pool, and from there onto the database's
// own connection ceiling -- which answers with an error rather than with a wait.
// "We switched to virtual threads and it got worse" is a real 2025-era migration
// story and this is the mechanism, in one process.
//
// WHAT TO LOOK FOR:
//   1. B's pool-wait p99 is small while B's p99 is enormous. The queue is real
//      and the pool metric cannot see it, because the executor queue is upstream
//      of the pool. HikariCP gives Java this number for free; that is only useful
//      if the bound is actually the pool.
//   2. C's pool wait is ALSO near zero, for the opposite reason: a pool larger
//      than the dependency's connection ceiling never queues. It converts the
//      wait into an error raised by the database instead. C's latency is far
//      better than B's and C has an error class B did not have at all.
//   3. D is worse than C on almost every column -- lower throughput, higher p99,
//      more failed requests -- and is still the change to make. C's failures are
//      raised BY THE DEPENDENCY, at a ceiling shared with every other client of
//      it. D's are raised by this service, in its own process, at a threshold it
//      chose. Same incident; only one of the two keeps it local.
//
//   cd java && javac SlowNotAbsent.java -d /tmp/t7java && java -cp /tmp/t7java SlowNotAbsent

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.Semaphore;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

public class SlowNotAbsent {

    // --- knobs, in one place so the arithmetic below is checkable ------------

    static final int ARRIVAL_RPS = 400;      // offered load, open model
    static final int WINDOW_MS = 2000;       // how long it is offered for
    static final int FAST_MS = 5;            // baseline dependency service time
    static final int SLOW_MS = 100;          // the injected fault
    static final int PLATFORM_THREADS = 16;  // the "servlet container" bound
    static final int BIG_POOL = 64;          // a pool sized by optimism
    static final int SAFE_POOL = 24;         // a pool sized under the ceiling
    static final int DB_MAX_CONNECTIONS = 32; // the dependency's own ceiling

    // ========================================================================
    // The dependency: a real TCP server, slow on purpose, with a connection
    // ceiling. `max_connections` is the bound nobody models until it bites.
    // ========================================================================

    static final class SlowDependency implements AutoCloseable {
        private final ServerSocket listener;
        private final AtomicInteger open = new AtomicInteger();
        private volatile int latencyMs = FAST_MS;
        private volatile boolean running = true;
        private final Thread acceptor;
        private final List<Thread> conns = new ArrayList<>();

        SlowDependency() throws IOException {
            listener = new ServerSocket(0, 256, InetAddress.getLoopbackAddress());
            acceptor = Thread.ofPlatform().name("dep-accept").start(this::acceptLoop);
        }

        int port() { return listener.getLocalPort(); }
        void latency(int ms) { latencyMs = ms; }
        int openConnections() { return open.get(); }

        private void acceptLoop() {
            while (running) {
                try {
                    Socket s = listener.accept();
                    if (open.incrementAndGet() > DB_MAX_CONNECTIONS) {
                        // Exactly what Postgres does at max_connections: it accepts
                        // the socket and then refuses, so the client learns about
                        // the limit AFTER paying for a connect.
                        open.decrementAndGet();
                        s.close();
                        continue;
                    }
                    synchronized (conns) {
                        conns.add(Thread.ofPlatform().start(() -> serve(s)));
                    }
                } catch (IOException e) {
                    return;
                }
            }
        }

        private void serve(Socket s) {
            try (s;
                 BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), StandardCharsets.UTF_8))) {
                OutputStream out = s.getOutputStream();
                out.write("READY\n".getBytes(StandardCharsets.UTF_8));
                out.flush();
                String line;
                while (running && (line = in.readLine()) != null) {
                    if (line.isEmpty()) continue;
                    Thread.sleep(latencyMs);      // the fault: slow, never absent
                    out.write("ok\n".getBytes(StandardCharsets.UTF_8));
                    out.flush();
                }
            } catch (IOException | InterruptedException ignored) {
                // client went away, or we are shutting down
            } finally {
                open.decrementAndGet();
            }
        }

        @Override public void close() throws IOException {
            running = false;
            listener.close();
        }
    }

    // ========================================================================
    // The pool. HikariCP's contribution is not that it has one -- everyone has
    // one -- it is that it exposes the WAIT as a metric. So does this.
    // ========================================================================

    static final class Pool implements AutoCloseable {
        private final int port;
        private final Semaphore permits;
        private final ArrayDeque<Socket> idle = new ArrayDeque<>();

        Pool(int port, int size) {
            this.port = port;
            this.permits = new Semaphore(size);
        }

        /** timeoutMs < 0 means wait forever -- the shipped default nearly everywhere. */
        Socket acquire(long timeoutMs) throws IOException, InterruptedException {
            if (timeoutMs < 0) {
                permits.acquire();
            } else if (!permits.tryAcquire(timeoutMs, TimeUnit.MILLISECONDS)) {
                return null;                       // fast, honest failure
            }
            synchronized (idle) {
                Socket s = idle.poll();
                if (s != null) return s;
            }
            try {
                return connect();
            } catch (IOException e) {
                permits.release();
                throw e;
            }
        }

        private Socket connect() throws IOException {
            Socket s = new Socket(InetAddress.getLoopbackAddress(), port);
            s.setTcpNoDelay(true);
            BufferedReader in = new BufferedReader(
                    new InputStreamReader(s.getInputStream(), StandardCharsets.UTF_8));
            String hello = in.readLine();
            if (!"READY".equals(hello)) {
                s.close();
                // This is the database refusing, not the pool. No pool setting on
                // the client side can make this go away; only a smaller pool can.
                throw new IOException("too many connections");
            }
            READERS.set(s, in);
            return s;
        }

        void release(Socket s) {
            synchronized (idle) { idle.add(s); }
            permits.release();
        }

        void discard(Socket s) {
            try { s.close(); } catch (IOException ignored) { }
            READERS.remove(s);
            permits.release();
        }

        @Override public void close() {
            synchronized (idle) {
                for (Socket s : idle) { try { s.close(); } catch (IOException ignored) { } }
                idle.clear();
            }
        }
    }

    /** Readers must outlive a single borrow, or a buffered byte is lost between them. */
    static final class ReaderTable {
        private final java.util.Map<Socket, BufferedReader> m = new java.util.concurrent.ConcurrentHashMap<>();
        void set(Socket s, BufferedReader r) { m.put(s, r); }
        BufferedReader get(Socket s) { return m.get(s); }
        void remove(Socket s) { m.remove(s); }
    }
    static final ReaderTable READERS = new ReaderTable();

    // ========================================================================
    // Measurement
    // ========================================================================

    enum Outcome { OK, POOL_TIMEOUT, CONNECT_REFUSED, READ_TIMEOUT }

    static final class Sample {
        double totalMs, poolWaitMs, serviceMs;
        Outcome outcome = Outcome.OK;
    }

    record Phase(String label, boolean virtualThreads, int injectedMs, int poolSize,
                 long poolTimeoutMs, int readTimeoutMs) { }

    record Result(Phase phase, int offered, int ok, int poolTimeouts, int refused, int readTimeouts,
                  double wallMs, double p50, double p99, double max,
                  double waitP99, double serviceMean, int peakDbConns, long maxLagMs) { }

    static double pct(List<Double> xs, double p) {
        if (xs.isEmpty()) return 0;
        List<Double> v = new ArrayList<>(xs);
        v.sort(null);
        int i = (int) Math.round(p / 100.0 * (v.size() - 1));
        return v.get(i);
    }

    static Result runPhase(SlowDependency dep, Phase ph) throws Exception {
        dep.latency(ph.injectedMs());
        int total = ARRIVAL_RPS * WINDOW_MS / 1000;
        Sample[] samples = new Sample[total];
        AtomicInteger peak = new AtomicInteger();
        java.util.concurrent.atomic.AtomicLong maxLag = new java.util.concurrent.atomic.AtomicLong();

        try (Pool pool = new Pool(dep.port(), ph.poolSize());
             ExecutorService exec = ph.virtualThreads()
                     ? Executors.newVirtualThreadPerTaskExecutor()
                     : new ThreadPoolExecutor(PLATFORM_THREADS, PLATFORM_THREADS,
                             0L, TimeUnit.MILLISECONDS, new LinkedBlockingQueue<>())) {

            long t0 = System.nanoTime();
            long gapNs = 1_000_000_000L / ARRIVAL_RPS;

            for (int i = 0; i < total; i++) {
                // OPEN MODEL. Request i is due at t0 + i*gap and is submitted then,
                // whether or not request i-1 has finished. A closed loop (fixed VUs
                // waiting for responses) would throttle itself as the system slows
                // and would show no collapse at all.
                final long dueNs = t0 + gapNs * i;
                long sleep = dueNs - System.nanoTime();
                // parkNanos, not Thread.sleep: at 400 rps the gap is 2.5 ms and
                // sleep's millisecond granularity would make the GENERATOR the
                // slowest thing in the experiment. `maxLagMs` below is this
                // program's version of k6's `dropped_iterations` -- if it is not
                // small, every number in the row is coordinated omission.
                while (sleep > 0) {
                    java.util.concurrent.locks.LockSupport.parkNanos(sleep);
                    sleep = dueNs - System.nanoTime();
                }
                maxLag.accumulateAndGet((System.nanoTime() - dueNs) / 1_000_000L, Math::max);

                final Sample s = new Sample();
                samples[i] = s;
                exec.execute(() -> {
                    long acqStart = System.nanoTime();
                    Socket sock = null;
                    try {
                        sock = pool.acquire(ph.poolTimeoutMs());
                        s.poolWaitMs = (System.nanoTime() - acqStart) / 1e6;
                        if (sock == null) {
                            s.outcome = Outcome.POOL_TIMEOUT;
                            return;
                        }
                        peak.accumulateAndGet(dep.openConnections(), Math::max);
                        if (ph.readTimeoutMs() > 0) {
                            // Java's SO_RCVTIMEO. Still not a deadline: it bounds one
                            // read, not the request, and nothing propagates it.
                            sock.setSoTimeout(ph.readTimeoutMs());
                        }
                        long svc = System.nanoTime();
                        sock.getOutputStream().write("GET\n".getBytes(StandardCharsets.UTF_8));
                        sock.getOutputStream().flush();
                        String reply = READERS.get(sock).readLine();
                        s.serviceMs = (System.nanoTime() - svc) / 1e6;
                        if ("ok".equals(reply)) {
                            pool.release(sock);
                        } else {
                            s.outcome = Outcome.READ_TIMEOUT;
                            pool.discard(sock);
                        }
                        sock = null;
                    } catch (SocketTimeoutException e) {
                        s.outcome = Outcome.READ_TIMEOUT;
                    } catch (IOException e) {
                        s.poolWaitMs = (System.nanoTime() - acqStart) / 1e6;
                        s.outcome = Outcome.CONNECT_REFUSED;
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        s.outcome = Outcome.READ_TIMEOUT;
                    } finally {
                        if (sock != null) pool.discard(sock);
                        s.totalMs = (System.nanoTime() - dueNs) / 1e6;
                    }
                });
            }
            exec.shutdown();
            if (!exec.awaitTermination(120, TimeUnit.SECONDS)) {
                throw new IllegalStateException("phase did not drain");
            }
            double wall = (System.nanoTime() - t0) / 1e6;

            List<Double> lat = new ArrayList<>(), wait = new ArrayList<>(), svc = new ArrayList<>();
            int ok = 0, pt = 0, refused = 0, rt = 0;
            for (Sample s : samples) {
                lat.add(s.totalMs);
                wait.add(s.poolWaitMs);
                switch (s.outcome) {
                    case OK -> { ok++; svc.add(s.serviceMs); }
                    case POOL_TIMEOUT -> pt++;
                    case CONNECT_REFUSED -> refused++;
                    case READ_TIMEOUT -> rt++;
                }
            }
            double mean = svc.stream().mapToDouble(Double::doubleValue).average().orElse(0);
            return new Result(ph, total, ok, pt, refused, rt, wall,
                    pct(lat, 50), pct(lat, 99), pct(lat, 100),
                    pct(wait, 99), mean, peak.get(), maxLag.get());
        }
    }

    static void printRow(Result r) {
        double rps = r.ok() / (r.wallMs() / 1000.0);
        System.out.printf("  %-28s %6d %6d %7.0f %8.0f %8.0f %9.0f %9.1f %6d%n",
                r.phase().label(), r.offered(), r.ok(), rps, r.p50(), r.p99(),
                r.waitP99(), r.serviceMean(), r.peakDbConns());
        System.out.printf("  %-28s injected %d ms | pool %d | pool timeouts %d | refused %d | "
                        + "read timeouts %d | errors %.1f%% | generator max lag %d ms%n", "",
                r.phase().injectedMs(), r.phase().poolSize(), r.poolTimeouts(), r.refused(),
                r.readTimeouts(), 100.0 * (r.offered() - r.ok()) / r.offered(), r.maxLagMs());
    }

    public static void main(String[] args) throws Exception {
        try (SlowDependency dep = new SlowDependency()) {
            System.out.println("Layer 8 topic 7 - Java: the bound moves, it does not disappear.");
            System.out.printf("%n  dependency: 127.0.0.1:%d, max_connections = %d, service time is a knob%n",
                    dep.port(), DB_MAX_CONNECTIONS);
            System.out.printf("  offered load = %d rps for %d ms (open model) -> %d requests per phase%n",
                    ARRIVAL_RPS, WINDOW_MS, ARRIVAL_RPS * WINDOW_MS / 1000);
            System.out.printf("  platform executor = %d threads; virtual executor = one thread per request%n%n",
                    PLATFORM_THREADS);

            System.out.printf("  %-28s %6s %6s %7s %8s %8s %9s %9s %6s%n", "phase", "offer", "ok",
                    "rps", "p50 ms", "p99 ms", "wait p99", "svc mean", "dbmax");
            System.out.println("  " + "-".repeat(104));

            Result a = runPhase(dep, new Phase("A platform(16), fast", false, FAST_MS, BIG_POOL, -1, 0));
            printRow(a);
            Result b = runPhase(dep, new Phase("B platform(16), slow", false, SLOW_MS, BIG_POOL, -1, 0));
            printRow(b);
            Result c = runPhase(dep, new Phase("C virtual, slow, pool 64", true, SLOW_MS, BIG_POOL, -1, 0));
            printRow(c);
            Result d = runPhase(dep, new Phase("D virtual, pool 24, bounded", true, SLOW_MS, SAFE_POOL, 250, 500));
            printRow(d);

            System.out.println("\nLITTLE'S LAW, worked from the measurements above");
            System.out.printf("  phase B: the bound is the EXECUTOR, not the pool.%n");
            System.out.printf("           %d threads / %.4f s service = %.0f rps of capacity, "
                            + "against %d rps offered.%n",
                    PLATFORM_THREADS, b.serviceMean() / 1000.0,
                    b.serviceMean() > 0 ? PLATFORM_THREADS / (b.serviceMean() / 1000.0) : 0, ARRIVAL_RPS);
            System.out.printf("           pool wait p99 = %.0f ms, i.e. the pool metric is QUIET while%n",
                    b.waitP99());
            System.out.printf("           p99 is %.0f ms. The queue is upstream of the thing you instrumented.%n",
                    b.p99());
            System.out.printf("  phase C: the executor bound is gone, so the pool and then the database%n");
            System.out.printf("           become the bound. Peak connections into the dependency:%n");
            System.out.printf("           B = %d, C = %d, against a ceiling of %d.%n",
                    b.peakDbConns(), c.peakDbConns(), DB_MAX_CONNECTIONS);
            System.out.printf("           Connection refusals: B = %d, C = %d.%n", b.refused(), c.refused());
            System.out.printf("           Offered concurrency at %d rps and %.0f ms service is about%n",
                    ARRIVAL_RPS, c.serviceMean());
            System.out.printf("           %.0f simultaneous calls, which is the number to compare with %d.%n",
                    ARRIVAL_RPS * c.serviceMean() / 1000.0, DB_MAX_CONNECTIONS);

            System.out.println("\nWHAT ACTUALLY HAPPENED");
            System.out.printf("  A -> B  p99 %.0f ms -> %.0f ms for a dependency that got %d ms slower.%n",
                    a.p99(), b.p99(), SLOW_MS - FAST_MS);
            System.out.printf("          errors %.1f%% -> %.1f%%: nothing broke, everything is late.%n",
                    100.0 * (a.offered() - a.ok()) / a.offered(),
                    100.0 * (b.offered() - b.ok()) / b.offered());
            System.out.printf("  B -> C  the Loom migration. Same fault, same pool setting, thread limit%n");
            System.out.printf("          removed: rps %.0f -> %.0f, p99 %.0f ms -> %.0f ms, errors %.1f%% -> %.1f%%,%n",
                    b.ok() / (b.wallMs() / 1000.0), c.ok() / (c.wallMs() / 1000.0),
                    b.p99(), c.p99(),
                    100.0 * (b.offered() - b.ok()) / b.offered(),
                    100.0 * (c.offered() - c.ok()) / c.offered());
            System.out.printf("          peak dependency connections %d -> %d.%n", b.peakDbConns(), c.peakDbConns());
            System.out.printf("          Read those four numbers together before deciding whether C is%n");
            System.out.printf("          better than B: latency improved and a NEW error class appeared.%n");
            System.out.printf("          The scarce OS thread had been holding the request rate off the%n");
            System.out.printf("          database. It was never a design; it was a side effect, and it was%n");
            System.out.printf("          load-bearing.%n");
            System.out.printf("  C -> D  pool sized UNDER the dependency's ceiling, plus a 250 ms acquire%n");
            System.out.printf("          timeout: rps %.0f -> %.0f, p99 %.0f ms -> %.0f ms, errors %.1f%% -> %.1f%%,%n",
                    c.ok() / (c.wallMs() / 1000.0), d.ok() / (d.wallMs() / 1000.0),
                    c.p99(), d.p99(),
                    100.0 * (c.offered() - c.ok()) / c.offered(),
                    100.0 * (d.offered() - d.ok()) / d.offered());
            System.out.printf("          refusals %d -> %d, fast pool rejections %d -> %d.%n",
                    c.refused(), d.refused(), c.poolTimeouts(), d.poolTimeouts());
            System.out.printf("          Read that honestly: D is WORSE than C on throughput, on p99 and on%n");
            System.out.printf("          total failed requests. The column that matters is the last one.%n");
            System.out.printf("          C's %d failures were raised by the DEPENDENCY, at a ceiling shared%n", c.refused());
            System.out.printf("          with every other client of it -- this service's incident became%n");
            System.out.printf("          everyone's. D's %d failures were raised here, in this process, at a%n", d.poolTimeouts());
            System.out.printf("          threshold this service chose, in %d ms, where they can be shed,%n", 250);
            System.out.printf("          retried against a budget, or shown to a user. Same incident,%n");
            System.out.printf("          contained instead of exported.%n");

            System.out.println("\nTHE JAVA POINT");
            System.out.println("  Pool wait is a first-class number here and in HikariCP, and it is the");
            System.out.println("  single most useful metric in this topic -- but B and C are two different");
            System.out.println("  reminders that a quiet pool-wait metric does not mean there is no queue.");
            System.out.println("  In B the queue is UPSTREAM of the pool, in the executor. In C there is no");
            System.out.println("  queue because the pool is bigger than the ceiling behind it, so the wait");
            System.out.println("  became an error somewhere you do not own. Only D makes the pool the");
            System.out.println("  binding constraint, which is the precondition for the metric to mean");
            System.out.println("  anything at all.");
            System.out.println("  Virtual threads did not create the problem and did not solve it. They");
            System.out.println("  removed an accidental bound, and everything downstream of that bound");
            System.out.println("  then had to be sized on purpose, which is work nobody scheduled.");
        }
    }
}
