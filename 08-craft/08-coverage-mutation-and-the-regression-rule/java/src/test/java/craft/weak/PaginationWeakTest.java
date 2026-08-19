// The JUnit 5 face of the weak suite. PIT runs THIS class; everything it
// asserts lives in WeakChecks, so the offline demo and the measured suite are
// the same three checks and cannot drift apart.
//
// WHAT THIS DEMONSTRATES: three passing tests, full line coverage of
// `Pagination.page`, and a surviving mutant. Three separate @Test methods
// rather than one, because PIT reports which test killed which mutant and that
// mapping is the useful artifact when you are trying to improve a suite rather
// than score one -- the same reason this topic's README sends you to
// cosmic-ray's kill matrix on the Python side.
//
// WHAT TO LOOK FOR: nothing here looks at a returned row or a returned cursor.
// When PIT hands you a survivor, the fix is one added assertion in one of these
// three methods, not a fourth method.
//
// KEEP THIS SUITE WEAK until you have recorded the first number.

package craft.weak;

import static org.junit.jupiter.api.Assertions.assertNull;

import craft.core.Pagination;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

class PaginationWeakTest {

    @Test
    @DisplayName("a page is no longer than the limit")
    void limitRespected() {
        assertNull(WeakChecks.checkLimitRespected(Pagination::page));
    }

    @Test
    @DisplayName("a zero limit is rejected")
    void zeroLimitRejected() {
        assertNull(WeakChecks.checkZeroLimitRejected(Pagination::page));
    }

    @Test
    @DisplayName("the walk terminates -- and that is ALL it checks")
    void walkTerminates() {
        assertNull(WeakChecks.checkWalkTerminates(Pagination::walkPages));
    }
}
