// Layer 5 - Topic 4: metastable failure, in one JVM.
//
// THE FLAGSHIP. The claim is not "overload is bad" -- everyone knows that.
// The claim is that the thing which TRIGGERS an outage and the thing which
// SUSTAINS it are different mechanisms, so removing the trigger does not end
// the outage. This file removes the trigger, keeps offered load exactly where
// it was, waits, and shows you nothing improving.
//
// Java carries two hazards on this topic and this file is built around the
// second one, because the first is famous and the second is a default nobody
// reads.
//
//   1. THE GC DEATH SPIRAL. More load means more live objects means more GC
//      means less CPU for work means more requests in flight means more live
//      objects. Run this with -Xlog:gc and watch collection frequency track
//      the in-flight count; the run line below turns it on for exactly that.
//      Note what it is: a SECOND sustaining effect stacked on the first, and
//      one the other five runtimes mostly do not have.
//   2. `Executors.newFixedThreadPool(n)` uses an UNBOUNDED
//      LinkedBlockingQueue. It bounds your CONCURRENCY and leaves your QUEUE
//      infinite, which is the textbook latency bomb: the pool looks like an
//      admission controller, reports healthy thread counts, and quietly
//      accumulates a backlog no metric of the pool exposes. Use a bounded
//      ArrayBlockingQueue with an explicit RejectedExecutionHandler and you
//      have accidentally built topic 5.
//
//   3. `new Semaphore(n)` IS NOT FIFO, and on this experiment that turns out
//      to matter more than either of the above. The default constructor is
//      NON-FAIR: a thread arriving exactly as a permit is released may barge
//      ahead of threads already queued for it. Under sustained overload that
//      is accidental adaptive LIFO -- the newest request served first, which
//      is topic 5's recommended mitigation obtained by accident -- and the
//      newest request is precisely the one whose caller has not given up yet.
//      So some queries beat their deadline, so some cache fills land, so the
//      hit rate climbs off the floor and the system claws its way part of the
//      way back out of a state the other five runtimes stay stuck in.
//
//      Which is a real finding and a broken experiment at the same time, so
//      this file builds the pool with `new Semaphore(POOL_SIZE, true)` --
//      FIFO, the same discipline Python's asyncio.Semaphore, Go's buffered
//      channel and tokio's Semaphore all give you -- and scenario 0 collapses
//      here exactly as it does in the other five. A pool that quietly serves
//      LIFO is not the thing this topic is about.
//
//      The finding is still worth having, and it is a ONE ARGUMENT
//      experiment to get it back: in the Database class below, change
//      `new Semaphore(POOL_SIZE, /* fair = */ true)` to `new
//      Semaphore(POOL_SIZE)` and rerun. Nothing else in the file changes.
//      Read the `hit%` column in both runs -- it stays on the floor with
//      fairness on and climbs off it with fairness off, which is the whole
//      of topic 5's adaptive LIFO argument arriving as an accident of a
//      default. "Which end of the queue do you serve?" is a design decision,
//      not an implementation detail, and Java is the language that makes it
//      for you unless you say otherwise.
//
// The twist worth having in mind while reading: this file uses VIRTUAL
// THREADS (`Executors.newVirtualThreadPerTaskExecutor()`), which is where new
// Java code is going and which removes the fixed pool that was accidentally
// your admission controller. There is now no bound anywhere -- every arrival
// gets a thread, the way Go gives every arrival a goroutine and Python gives
// every arrival a task. Virtual threads make you WRITE the limit you used to
// get for free by accident. That is the same lesson Go teaches, arrived at
// from the opposite direction, and `vthreads` in the output below is the
// column where you watch it happen.
//
// WHAT THIS DEMONSTRATES
//
//   A cache in front of a database, at a 90% hit rate, comfortably stable.
//   The trigger is one instantaneous, fully reversible command: FLUSHALL.
//   The cache is BACK the moment it starts refilling -- except that it never
//   starts, because refilling requires a query to finish before its caller
//   gives up, and no query does any more.
//
//   HotOS '25 vocabulary, which this file is built to make concrete:
//     trigger                 the cache flush, over in one millisecond
//     amplification mechanism naive retries (topic 3) plus the miss rate
//                             going from 10% to 100%
//     sustaining effect       a cache that cannot refill, because fills only
//                             happen on completions that beat the deadline
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. `goodput` versus `thruput`. Throughput stays high while goodput goes
//      to zero: the JVM is busy, the pool is full, requests are flowing, and
//      almost none of them produce a response anybody receives.
//   2. `hit%` stuck at zero AFTER the trigger is long gone. That is the
//      sustaining effect, and it is why scenario 0 never recovers.
//   3. `vthreads` climbing. Cheap threads are still queue.
//   4. `hit%` in scenario 0, and then again after you delete the `, true`
//      from the semaphore. If it climbs back off zero, that is barging, not
//      luck -- see hazard 3 above, and run the one-character experiment.
//   5. Which escapes are SUFFICIENT rather than merely helpful. The verdict
//      lines at the end are computed from THIS run, not asserted here.
//
// RUN
//   javac Metastable.java -Xlint:all -d /tmp/javabuild && \
//     java -Xlog:gc -cp /tmp/javabuild Metastable
//
// Roughly four minutes: five scenarios, the four with an escape running
// longer because "did it recover" is a question about minutes, not seconds.

import java.util.ArrayList;
import java.util.HashSet;
import java.util.Iterator;
import java.util.List;
import java.util.Random;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicLong;

public final class Metastable {

    // ------------------------------------------------------------ config
    //
    // Identical to python/metastable.py's constants, deliberately: the point
    // of six languages here is that the same system-level dynamic appears in
    // all of them, so the constants are not allowed to drift.

    static final double OFFERED_RPS = 180.0;  // constant. It never changes.
    static final int KEYS = 400;              // the cache keyspace
    static final int EVICT_PER_SEC = 18;      // TTL churn -> 90% hit rate

    static final long DB_SERVICE_MS = 200;    // an uncached read
    static final long CACHE_SERVICE_MS = 1;   // a cached one
    static final int POOL_SIZE = 6;           // 6 / 0.200 = 30 misses/s

    static final long CLIENT_TIMEOUT_MS = 500;  // longer than normal service
    static final int ATTEMPTS = 3;              // time, shorter than degraded

    static final double TRIGGER_AT = 6.0;     // redis-cli FLUSHALL
    static final double ESCAPE_AT = 16.0;     // ten seconds of nothing improving
    static final double END_AT = 30.0;
    static final double ESCAPE_END_AT = 50.0;
    static final double REPORT_EVERY = 2.0;

    static final long SHED_LIMIT = 8;              // escape (c). Topic 5, early.
    static final double BUDGET_RATIO = 0.10;       // escape (b). Topic 3.
    static final double RAMP_BACK_SECONDS = 8.0;   // escape (a), SLOWLY.
    static final double DROP_SECONDS = 5.0;

    // ------------------------------------------------------------- metrics

    static final class Metrics {
        final AtomicLong goodput = new AtomicLong();
        final AtomicLong thruputAttempts = new AtomicLong();
        final AtomicLong retries = new AtomicLong();
        final AtomicLong failed = new AtomicLong();
        final AtomicLong shed = new AtomicLong();
        final AtomicLong vthreads = new AtomicLong();
    }

    // --------------------------------------------------------- the cache

    /**
     * Redis, modelled as the only thing about Redis that matters here: a set
     * of keys that are present, and the fact that emptying it is instant and
     * refilling it is not.
     */
    static final class Cache {
        private final Set<Integer> present = new HashSet<>();
        private long hits;
        private long misses;

        Cache() {
            for (int k = 0; k < KEYS; k++) {
                present.add(k);
            }
        }

        synchronized boolean get(int key) {
            if (present.contains(key)) {
                hits++;
                return true;
            }
            misses++;
            return false;
        }

        synchronized void put(int key) {
            present.add(key);
        }

        /**
         * One command. Instantaneous. Fully reversible. This is the entire
         * trigger, and ten seconds later it will be completely irrelevant to
         * why the system is down.
         */
        synchronized void flushall() {
            present.clear();
        }

        /**
         * Ordinary TTL churn, which is what holds the hit rate at 90% instead
         * of letting it climb to 100% and make the experiment lie.
         */
        synchronized void evict(int n) {
            Iterator<Integer> it = present.iterator();
            for (int i = 0; i < n && it.hasNext(); i++) {
                it.next();
                it.remove();
            }
        }

        synchronized long[] takeRates() {
            long[] out = {hits, misses};
            hits = 0;
            misses = 0;
            return out;
        }
    }

    // ------------------------------------------------------- the database

    /**
     * A real bounded pool. 6 connections at 200ms is 30 queries a second, and
     * nothing anybody does to the application changes that number.
     *
     * Note the constructor. `new Semaphore(POOL_SIZE)` -- what almost
     * everybody writes -- is the NON-FAIR one, and under overload its barging
     * behaves like LIFO, which is enough to pull scenario 0 partly back out
     * of the metastable state and make this file disagree with the other
     * five. So the `, true` is deliberate: it is FIFO, it matches every other
     * runtime in this topic, and deleting it is the single most interesting
     * edit you can make to this program. See hazard 3 in the header.
     *
     * The interrupt handling is the Java-specific part: when the client gives
     * up, it cancels the task, the carrier of that virtual thread throws
     * InterruptedException out of the sleep, and the `finally` hands the
     * permit back. Java can cancel a WAIT this way; it still cannot cancel a
     * query that a database is already executing, which is topic 2's finding
     * and the reason `statement_timeout` exists.
     */
    static final class Database {
        private final Semaphore sem = new Semaphore(POOL_SIZE, /* fair = */ true);
        private final AtomicLong inUse = new AtomicLong();

        void query() throws InterruptedException {
            sem.acquire();
            inUse.incrementAndGet();
            try {
                Thread.sleep(DB_SERVICE_MS);
            } finally {
                inUse.decrementAndGet();
                sem.release();
            }
        }

        long inUse() {
            return inUse.get();
        }
    }

    // ------------------------------------------------------- retry budget

    /** Topic 3's token bucket, used here only as escape (b). */
    static final class RetryBudget {
        private final AtomicLong milliTokens = new AtomicLong(3_000);

        void deposit() {
            milliTokens.updateAndGet(t -> Math.min(t + (long) (BUDGET_RATIO * 1000), 103_000));
        }

        boolean withdraw() {
            return milliTokens.getAndUpdate(t -> t >= 1000 ? t - 1000 : t) >= 1000;
        }
    }

    // ---------------------------------------------------------- the server

    static final class Server {
        final Cache cache;
        final Database db;
        final Metrics m;
        final AtomicLong inflight = new AtomicLong();
        volatile RetryBudget budget;     // escape (b)
        volatile long shedLimit;         // escape (c); 0 means none

        Server(Cache cache, Database db, Metrics m) {
            this.cache = cache;
            this.db = db;
            this.m = m;
        }

        /** One attempt. Returns true if the caller got an answer in time. */
        boolean handle(int key, long deadlineNanos) throws InterruptedException {
            // Escape (c), and topic 5 in one line: refuse work you have no
            // capacity for, immediately, instead of accepting it and being
            // late.
            long lim = shedLimit;
            if (lim > 0 && inflight.get() >= lim) {
                m.shed.incrementAndGet();
                return false;
            }
            inflight.incrementAndGet();
            try {
                if (cache.get(key)) {
                    Thread.sleep(CACHE_SERVICE_MS);
                    return System.nanoTime() <= deadlineNanos;
                }
                db.query();
                boolean inTime = System.nanoTime() <= deadlineNanos;
                if (inTime) {
                    // THE SUSTAINING EFFECT, in one `if`. The fill happens in
                    // the handler, after the query returns -- and under
                    // overload the handler has already been abandoned by
                    // then, so the fill never happens. The cache cannot
                    // refill precisely because the database is slow, and the
                    // database is slow precisely because the cache is empty.
                    cache.put(key);
                }
                return inTime;
            } finally {
                inflight.decrementAndGet();
            }
        }
    }

    // ---------------------------------------------------------- the client

    /**
     * Topic 3's naive retry client: no jitter, no budget unless escape (b)
     * turned one on, and a per-attempt timeout that is comfortable when the
     * system is well and hopeless when it is not.
     */
    static void clientRequest(ExecutorService exec, Server server, Metrics m, int key) {
        try {
            for (int attempt = 0; attempt < ATTEMPTS; attempt++) {
                RetryBudget budget = server.budget;
                if (attempt > 0) {
                    if (budget != null && !budget.withdraw()) {
                        break;
                    }
                    m.retries.incrementAndGet();
                }
                long deadline = System.nanoTime() + CLIENT_TIMEOUT_MS * 1_000_000L;
                boolean ok = false;
                Future<Boolean> f;
                try {
                    f = exec.submit(() -> server.handle(key, deadline));
                } catch (RuntimeException rejected) {
                    // The executor is gone: the app "restarted" underneath
                    // this request. The client, of course, has not.
                    return;
                }
                try {
                    ok = f.get(CLIENT_TIMEOUT_MS, TimeUnit.MILLISECONDS);
                } catch (TimeoutException e) {
                    // We stopped waiting. cancel(true) interrupts the virtual
                    // thread doing the work, which is the closest Java gets to
                    // Rust dropping a future -- and the retry we are about to
                    // send is additive regardless.
                    f.cancel(true);
                } catch (Exception e) {
                    f.cancel(true);
                }
                m.thruputAttempts.incrementAndGet();
                if (ok) {
                    // GOODPUT: a response delivered to a caller that was still
                    // waiting for it. Not "requests handled". This is the only
                    // number in this file worth alerting on.
                    m.goodput.incrementAndGet();
                    if (budget != null) {
                        budget.deposit();
                    }
                    return;
                }
            }
            m.failed.incrementAndGet();
        } finally {
            m.vthreads.decrementAndGet();
        }
    }

    // --------------------------------------------------------- the harness

    record Row(double t, double offered, double thruput, double goodput,
               double hit, long pg, long inflight, long vthreads, double retry) { }

    /**
     * Offered load. Constant everywhere except escape (a), which is the only
     * intervention in this file that touches the client side at all.
     */
    static double offeredRate(double t, String escape) {
        if (!escape.equals("a") || t < ESCAPE_AT) {
            return OFFERED_RPS;
        }
        double since = t - ESCAPE_AT;
        if (since < DROP_SECONDS) {
            return 0.0;                                            // take it away
        }
        double ramp = (since - DROP_SECONDS) / RAMP_BACK_SECONDS;  // ... let back
        return OFFERED_RPS * Math.min(1.0, ramp);                  // SLOWLY
    }

    static List<Row> runScenario(String escape, double[] endOut) throws Exception {
        double endAt = escape.isEmpty() ? END_AT : ESCAPE_END_AT;
        endOut[0] = endAt;
        Metrics m = new Metrics();
        Cache cache = new Cache();
        Database db = new Database();
        Server server = new Server(cache, db, m);
        Random rng = new Random(20250504);

        // Every arrival gets a thread. That is the modern Java shape and it
        // is also the missing bound: nothing here refuses to make one.
        ExecutorService clients = Executors.newVirtualThreadPerTaskExecutor();
        ExecutorService handlers = Executors.newVirtualThreadPerTaskExecutor();

        long begin = System.nanoTime();
        long lastReport = begin;
        long lastEvict = begin;
        long at = begin;
        long lastG = 0;
        long lastTh = 0;
        long lastR = 0;
        boolean triggered = false;
        boolean escaped = false;
        List<Row> rows = new ArrayList<>();

        while (true) {
            double tPlanned = (at - begin) / 1e9;
            if (tPlanned > endAt) {
                break;
            }
            double rate = offeredRate(tPlanned, escape);
            if (rate <= 0) {
                at += 50_000_000L;
            } else {
                at += (long) (-Math.log(1 - rng.nextDouble()) / rate * 1e9);
            }
            long sleepNanos = at - System.nanoTime();
            if (sleepNanos > 0) {
                Thread.sleep(sleepNanos / 1_000_000L, (int) (sleepNanos % 1_000_000L));
            }
            long now = System.nanoTime();
            double t = (now - begin) / 1e9;

            if (!triggered && t >= TRIGGER_AT) {
                cache.flushall();
                triggered = true;
            }
            if (!escaped && t >= ESCAPE_AT) {
                escaped = true;
                switch (escape) {
                    case "b" -> server.budget = new RetryBudget();
                    case "c" -> server.shedLimit = SHED_LIMIT;
                    case "d" -> {
                        // "Restart the app containers." Everything the process
                        // owns goes: the handler threads, the in-flight work,
                        // the pool. The cache is external and stays exactly as
                        // cold as it was, and the clients never stopped
                        // retrying.
                        handlers.shutdownNow();
                        handlers = Executors.newVirtualThreadPerTaskExecutor();
                        // Rebind rather than reset in place. A restart replaces
                        // the process: the new one starts with an empty pool
                        // and a zero gauge, while the dying requests unwind
                        // against the old objects. Zeroing counters underneath
                        // them would drive the gauges NEGATIVE, which is a bug
                        // in the instrument rather than a finding.
                        db = new Database();
                        server = new Server(cache, db, m);
                    }
                    default -> { }
                }
            }

            if (now - lastEvict >= 1_000_000_000L) {
                cache.evict(EVICT_PER_SEC);
                lastEvict = now;
            }

            if (rate > 0) {
                // No backpressure anywhere in these two lines. A virtual
                // thread is always available, whatever the state of the system
                // it is feeding.
                final Server s = server;
                final ExecutorService h = handlers;
                final int key = rng.nextInt(KEYS);
                m.vthreads.incrementAndGet();
                clients.submit(() -> clientRequest(h, s, m, key));
            }

            if ((now - lastReport) / 1e9 >= REPORT_EVERY) {
                double span = (now - lastReport) / 1e9;
                long g = m.goodput.get();
                long th = m.thruputAttempts.get();
                long r = m.retries.get();
                long[] rates = cache.takeRates();
                rows.add(new Row(t, rate,
                        (th - lastTh) / span,
                        (g - lastG) / span,
                        100.0 * rates[0] / Math.max(1, rates[0] + rates[1]),
                        db.inUse(),
                        server.inflight.get(),
                        m.vthreads.get(),
                        (r - lastR) / Math.max(1.0, (double) (th - lastTh))));
                lastG = g;
                lastTh = th;
                lastR = r;
                lastReport = now;
            }
        }

        clients.shutdownNow();
        handlers.shutdownNow();
        clients.awaitTermination(2, TimeUnit.SECONDS);
        handlers.awaitTermination(2, TimeUnit.SECONDS);
        return rows;
    }

    // -------------------------------------------------------- reporting

    static final String HEADER =
            "      t   offered   thruput   goodput   hit%   pg  inflight  vthreads  retry/req"
            + "   goodput as % of offered";

    static double[] render(String title, String note, List<Row> rows, double endAt) {
        System.out.printf("%n=== %s ===%n", title);
        System.out.printf("    %s%n", note);
        System.out.println(HEADER);
        System.out.println("-".repeat(HEADER.length()));
        for (Row r : rows) {
            double frac = r.goodput() / OFFERED_RPS;
            String bar = "#".repeat((int) Math.max(0, Math.round(24 * Math.min(1.0, frac))));
            String mark = "";
            if (Math.abs(r.t() - TRIGGER_AT) < REPORT_EVERY / 2) {
                mark = "  <-- FLUSHALL";
            } else if (Math.abs(r.t() - ESCAPE_AT) < REPORT_EVERY / 2) {
                mark = "  <-- escape applied";
            }
            System.out.printf("  %5.1f %9.1f %9.1f %9.1f %6.1f %4d %9d %9d %10.2f   |%s%s%n",
                    r.t(), r.offered(), r.thruput(), r.goodput(), r.hit(), r.pg(),
                    r.inflight(), r.vthreads(), r.retry(), bar, mark);
        }
        double gBefore = rows.stream().filter(r -> r.t() < TRIGGER_AT)
                .mapToDouble(Row::goodput).average().orElse(0);
        double gAfter = rows.stream().filter(r -> r.t() >= endAt - 6)
                .mapToDouble(Row::goodput).average().orElse(0);
        System.out.printf("    goodput before the trigger %6.1f rps (%.0f%% of offered)   "
                        + "final 6 seconds %6.1f rps (%.0f%% of offered)%n",
                gBefore, 100 * gBefore / OFFERED_RPS, gAfter, 100 * gAfter / OFFERED_RPS);
        return new double[]{gBefore, gAfter};
    }

    /**
     * COMPUTED from the run that just happened, never asserted here.
     * Sufficient means "goodput came back", not "the intervention did
     * something measurable" -- that distinction is the whole of step 5 in the
     * README.
     */
    static String verdict(double before, double after) {
        if (before <= 1.0) {
            return "baseline never established -- see README";
        }
        double pct = 100 * after / before;
        if (pct >= 70) {
            return String.format("SUFFICIENT   (recovered to %.0f%% of pre-trigger goodput)", pct);
        }
        if (pct >= 20) {
            return String.format("partial      (only %.0f%% of pre-trigger goodput)", pct);
        }
        return String.format("not sufficient (%.0f%% of pre-trigger goodput)", pct);
    }

    public static void main(String[] args) throws Exception {
        System.out.println("Metastable failure: a cache flush that stops mattering long "
                + "before the outage does.");
        System.out.printf("Offered load is constant at %.0f rps and is never raised. "
                        + "Cache hit rate %.0f%% when warm.%n",
                OFFERED_RPS, 100 - 100 * EVICT_PER_SEC / OFFERED_RPS);
        double capacity = POOL_SIZE / (DB_SERVICE_MS / 1000.0);
        System.out.printf("Database capacity is %d/%.3f = %.0f queries per second. Warm, the "
                        + "miss rate needs %d of them (%.0f%% utilised).%n",
                POOL_SIZE, DB_SERVICE_MS / 1000.0, capacity, EVICT_PER_SEC,
                100.0 * EVICT_PER_SEC / capacity);
        System.out.printf("Cold, it needs all %.0f -- %.0fx capacity, before a single retry. "
                        + "Client timeout %dms, %d attempts, no jitter, no budget, no shedding.%n",
                OFFERED_RPS, OFFERED_RPS / capacity, CLIENT_TIMEOUT_MS, ATTEMPTS);
        System.out.printf("FLUSHALL at t=%.0fs. Escapes, where a scenario has one, at t=%.0fs.%n",
                TRIGGER_AT, ESCAPE_AT);

        String[][] scenarios = {
            {"0 no escape: remove the trigger and wait",
             "The trigger was over in a millisecond. Watch the next 24 seconds.", ""},
            {"a drop offered load to zero, then ramp it back slowly",
             String.format("The one nobody wants to authorise. %.0fs of zero, then %.0fs of "
                     + "ramp. Watch the ramp, not the drop.", DROP_SECONDS, RAMP_BACK_SECONDS),
             "a"},
            {"b enable topic 3's 10% retry budget, load unchanged",
             "Removes the amplification. Does not remove the sustaining effect.", "b"},
            {"c enable topic 5's load shedder, load unchanged",
             String.format("Admit at most %d in flight; 503 the rest, immediately.", SHED_LIMIT),
             "c"},
            {"d restart the app, load unchanged",
             "Clears the handler threads, the in-flight work and the pool. Not the cache.", "d"},
        };

        List<Object[]> results = new ArrayList<>();
        for (String[] sc : scenarios) {
            double[] endOut = new double[1];
            List<Row> rows = runScenario(sc[2], endOut);
            double[] ba = render(sc[0], sc[1], rows, endOut[0]);
            results.add(new Object[]{sc[0], ba[0], ba[1]});
        }

        System.out.println();
        System.out.println("=".repeat(78));
        System.out.printf("%-52s%15s%11s%n", "scenario", "goodput before", "after");
        System.out.println("-".repeat(78));
        for (Object[] r : results) {
            System.out.printf("%-52s%14.1f%11.1f%n", r[0], r[1], r[2]);
        }

        System.out.println();
        System.out.println("Scenario 0 is the whole topic. The trigger -- one FLUSHALL -- was over");
        System.out.println("instantly and reversibly, offered load never changed by a single request,");
        System.out.printf("and goodput half a minute later is %.1f rps -- which is what THIS run%n",
                (Double) results.get(0)[2]);
        System.out.println("measured, not a sentence written before it. If it is not near zero, read");
        System.out.println("the README's 'what would mean the experiment is broken' before reading");
        System.out.println("anything else. Nothing is broken. Nothing needs rolling back. The system");
        System.out.println("has settled into a second stable state, where the cache cannot refill");
        System.out.println("because the database is saturated and the database is saturated because");
        System.out.println("the cache is empty.");
        System.out.println();
        System.out.println("Escapes, judged against THIS run rather than against a story:");
        for (int i = 1; i < results.size(); i++) {
            Object[] r = results.get(i);
            System.out.printf("  %s %s%n", ((String) r[0]).substring(0, 2),
                    verdict((Double) r[1], (Double) r[2]));
        }
        System.out.printf("  (scenario 0 finished at %.1f rps of goodput, for comparison)%n",
                (Double) results.get(0)[2]);
        System.out.println();
        System.out.println("What each escape actually touches, which is why they do not rank the way");
        System.out.println("intuition ranks them:");
        System.out.println("  (a) drop and ramp    removes load, not the loop. The drop always works;");
        System.out.println("      the RAMP is the experiment. Full load returning to a cache that is");
        System.out.println("      still empty walks straight back into the same state, so \"let it back");
        System.out.println("      slowly\" is a QUANTITATIVE claim -- the ramp has to be slower than the");
        System.out.printf("      cache can refill, which here is %.0f keys per second against %d keys.%n",
                capacity, KEYS);
        System.out.printf("      Raise RAMP_BACK_SECONDS from %.0f and find the threshold yourself.%n",
                RAMP_BACK_SECONDS);
        System.out.println("  (b) retry budget     removes topic 3's amplification and leaves the");
        System.out.println("      sustaining effect untouched. \"We turned the retries off\" is a sentence");
        System.out.println("      people say in incidents that are still ongoing twenty minutes later.");
        System.out.println("  (c) load shedding    is the only one that breaks the FEEDBACK LOOP: it is");
        System.out.println("      the only intervention that lets the ADMITTED requests finish inside");
        System.out.println("      their deadline, which is the exact condition the cache needs to");
        System.out.println("      refill. Watch its hit% climb while retry/req falls -- that is the loop");
        System.out.println("      running backwards.");
        System.out.println("  (d) restart the app  clears everything the process owns and nothing the");
        System.out.println("      clients own. The amplifier is in the clients. They did not restart.");
        System.out.println();
        System.out.println("In HotOS '25 vocabulary, worth writing down for your own system before");
        System.out.println("you need it:");
        System.out.println("  trigger                 a cache flush, over in one millisecond");
        System.out.println("  amplification mechanism naive retries, plus the miss rate going from 10%");
        System.out.println("                          to 100% on a database that was 60% utilised");
        System.out.println("  sustaining effect       fills only happen on completions that beat the");
        System.out.println("                          caller's deadline, and under overload none do");
    }

    private Metastable() { }
}
