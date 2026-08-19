// Layer 7 · Topic 1 — IDOR as a missing authorization check (Java).
//
// One command, no arguments, JDK only:
//   javac IdorEnumeration.java && java IdorEnumeration
//
// Java is the one runtime in this topic with a DECLARATIVE authorization
// model: Spring Security's @PreAuthorize("@invoiceGuard.owns(#id, auth)")
// runs before the method body and is greppable at the signature. That is a
// genuine improvement AND the sharpest illustration of its limit: the check
// is opt-in per method, so an UNANNOTATED new endpoint is wide open, and
// Spring Data's findById(id) underneath is still a primary-key lookup. A
// declarative check you can forget to declare has the same failure mode as
// an `if` you can forget to write. We model exactly that: `vulnerable` is the
// endpoint whose @PreAuthorize was never added.
//
// What to look for: vulnerable leaks ~667/1000; both fixes leak 0 and return
// 404 (not 403) on a wrong-owner id.
import java.util.HashMap;
import java.util.Map;
import java.util.function.BiFunction;

public class IdorEnumeration {
    static final int N_INVOICES = 1500;
    static final int CALLER = 1; // alice
    static final int N_REQUESTS = 1000;

    record Invoice(int id, int ownerId, int amount) {}

    static final class Store {
        final Map<Integer, Invoice> rows = new HashMap<>();
        int requestUser = 0; // the RLS "session variable"
        Store() {
            for (int i = 1; i <= N_INVOICES; i++)
                rows.put(i, new Invoice(i, ((i - 1) % 3) + 1, i * 100));
        }
        Invoice get(int id) { return rows.get(id); }            // findById(id)
        Invoice getScoped(int id, int owner) {                  // WHERE id AND owner_id
            Invoice r = rows.get(id);
            return (r != null && r.ownerId() == owner) ? r : null;
        }
        Invoice getRls(int id) {                                // policy below the handler
            Invoice r = rows.get(id);
            if (r == null || requestUser == 0 || r.ownerId() != requestUser) return null;
            return r;
        }
    }

    // A handler returns {status, ownerIdOrMinus1}.
    interface Handler { int[] handle(Store s, int caller, int id); }

    static int[] vulnerable(Store s, int caller, int id) {
        Invoice r = s.get(id);                                  // @PreAuthorize never added
        return r != null ? new int[]{200, r.ownerId()} : new int[]{404, -1};
    }
    static int[] fixedQuery(Store s, int caller, int id) {
        Invoice r = s.getScoped(id, caller);
        return r != null ? new int[]{200, r.ownerId()} : new int[]{404, -1};
    }
    static int[] fixedRls(Store s, int caller, int id) {
        s.requestUser = caller;                                 // SET LOCAL app.current_user
        Invoice r = s.getRls(id);
        return r != null ? new int[]{200, r.ownerId()} : new int[]{404, -1};
    }

    static int[] enumIds(int n) {
        int[] ids = new int[n];
        int x = 1; // LCG in 32-bit space; deterministic, measured numbers
        for (int i = 0; i < n; i++) {
            x = 1103515245 * x + 12345;
            ids[i] = 1 + Math.floorMod(x, N_INVOICES);
        }
        return ids;
    }

    static void run(Handler h, String label) {
        Store s = new Store();
        int leaked = 0, own = 0, notFound = 0;
        for (int id : enumIds(N_REQUESTS)) {
            int[] resp = h.handle(s, CALLER, id);
            if (resp[0] == 200) { if (resp[1] != CALLER) leaked++; else own++; }
            else notFound++;
        }
        int wrongStatus = h.handle(new Store(), CALLER, 2)[0]; // id 2 is bob's
        System.out.printf("  %-14s leaked=%4d/%d   own=%4d  not_found=%4d   wrong-owner id -> HTTP %d%n",
                label, leaked, N_REQUESTS, own, notFound, wrongStatus);
    }

    public static void main(String[] args) {
        System.out.println("Layer 7 · Topic 1 — IDOR enumeration (logged in as alice, id=1)");
        System.out.printf("seed: %d invoices, 3 tenants interleaved, alice owns %d%n", N_INVOICES, N_INVOICES / 3);
        System.out.printf("attack: %d requests, ids uniform over 1..%d%n%n", N_REQUESTS, N_INVOICES);
        System.out.println("  handler        wrong-owner rows leaked per 1,000 requests");
        run(IdorEnumeration::vulnerable, "vulnerable");
        run(IdorEnumeration::fixedQuery, "fixed_query");
        run(IdorEnumeration::fixedRls, "fixed_rls");
        System.out.println("\nRead: @PreAuthorize makes the check visible at the signature, but " +
                "it is opt-in -- the unannotated endpoint is the leak. The data-layer control " +
                "(RLS) is the one a missing annotation cannot switch off.");
    }
}
