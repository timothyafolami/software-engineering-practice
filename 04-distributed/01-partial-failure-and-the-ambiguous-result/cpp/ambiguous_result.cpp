// Layer 4 · Topic 1 — the third outcome, in C++.
//
// WHAT THIS DEMONSTRATES
//   The same five faults and the same ledger as the other five programs. C++ is
//   here because it is the only language in this lab with nothing between the
//   program and the kernel, and this topic is entirely about a boundary the
//   kernel draws:
//
//     connect()  failed  -> not one byte of your request exists anywhere.
//     send()     failed with 0 bytes written -> same.
//     send()     failed after n>0 bytes      -> the peer may have all of it.
//     recv()     failed  -> the request was delivered. You will never know more.
//
//   Every HTTP client in the other five languages is a wrapper that decides how
//   much of that to show you. Here there is no wrapper, so the safe/unsafe
//   decision is not an exception taxonomy to learn -- it is which line returned
//   -1, and what errno says.
//
//   And, unlike Rust: nothing stops you writing the wrong thing. Phase 1's
//   `if (outcome != SUCCESS) continue;` compiles without a warning at -Wall
//   -Wextra. That is the comparison worth making.
//
// WHAT TO LOOK FOR
//   Phase 1's duplicate charges against phase 2's, and the unresolved ambiguity
//   that survives both.
//
// Build & run:
//   g++ -O2 -std=c++17 -pthread -Wall -Wextra -o /tmp/l4t1_cpp cpp/ambiguous_result.cpp && /tmp/l4t1_cpp

// NOTE: htonl/htons/ntohs are function-like MACROS on Darwin (sys/_endian.h),
// so they must not be written as ::htonl(...). On glibc they are functions and
// the :: form compiles, which is exactly how a Linux-only file fails here.
#include <arpa/inet.h>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <errno.h>
#include <fcntl.h>
#include <map>
#include <mutex>
#include <netinet/in.h>
#include <poll.h>
#include <string>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>
#include <vector>

static const int CLIENT_TIMEOUT_MS = 300;
static const int SLOW_RESPONSE_MS = 1000;
static const int REQUESTS_PER_MODE = 4;
static const int MAX_ATTEMPTS = 3;

static const char* MODES[] = {"ok", "slow", "hang", "reset",
                              "crash_after_commit", "refused"};
static const int MODE_COUNT = 6;

// --- server-side truth ------------------------------------------------------

static std::mutex g_ledger_mutex;
static std::vector<std::string> g_ledger;

static void commit(const std::string& charge_id) {
    std::lock_guard<std::mutex> lock(g_ledger_mutex);
    g_ledger.push_back(charge_id);
}

static size_t ledger_size() {
    std::lock_guard<std::mutex> lock(g_ledger_mutex);
    return g_ledger.size();
}

static std::mutex g_held_mutex;
static std::vector<int> g_held_fds;  // `hang` connections, closed at shutdown

static void send_reply(int fd, const std::string& charge_id) {
    std::string body = "{\"charge_id\":\"" + charge_id + "\"}";
    char head[256];
    int n = std::snprintf(head, sizeof(head),
                          "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                          "Content-Length: %zu\r\nConnection: close\r\n\r\n",
                          body.size());
    (void)::send(fd, head, (size_t)n, 0);
    (void)::send(fd, body.data(), body.size(), 0);
}

static void serve_connection(int fd) {
    char buf[4096];
    ssize_t n = ::recv(fd, buf, sizeof(buf) - 1, 0);
    if (n <= 0) {
        ::close(fd);
        return;
    }
    buf[n] = '\0';

    // "GET /charge/<mode>/<charge_id> HTTP/1.1"
    std::string request(buf);
    size_t sp1 = request.find(' ');
    size_t sp2 = request.find(' ', sp1 + 1);
    if (sp1 == std::string::npos || sp2 == std::string::npos) {
        ::close(fd);
        return;
    }
    std::string path = request.substr(sp1 + 1, sp2 - sp1 - 1);
    size_t s2 = path.find('/', 1);
    size_t s3 = path.find('/', s2 + 1);
    if (s2 == std::string::npos || s3 == std::string::npos) {
        ::close(fd);
        return;
    }
    std::string mode = path.substr(s2 + 1, s3 - s2 - 1);
    std::string charge_id = path.substr(s3 + 1);

    if (mode == "ok") {
        commit(charge_id);
        send_reply(fd, charge_id);
        ::close(fd);
    } else if (mode == "slow") {
        commit(charge_id);
        std::this_thread::sleep_for(std::chrono::milliseconds(SLOW_RESPONSE_MS));
        send_reply(fd, charge_id);
        ::close(fd);
    } else if (mode == "hang") {
        // Accepted, committed, never answered. The fd is parked rather than
        // closed, because closing it is what the client is waiting for.
        commit(charge_id);
        std::lock_guard<std::mutex> lock(g_held_mutex);
        g_held_fds.push_back(fd);
    } else if (mode == "reset") {
        commit(charge_id);
        // SO_LINGER with a zero timeout turns close() into an RST rather than a
        // FIN. This is what a peer that aborts, or a middlebox that gives up,
        // looks like from the caller's side.
        struct linger lin;
        lin.l_onoff = 1;
        lin.l_linger = 0;
        ::setsockopt(fd, SOL_SOCKET, SO_LINGER, &lin, sizeof(lin));
        ::close(fd);
    } else if (mode == "crash_after_commit") {
        // The case no timeout tuning can fix: durable work, dead reporter.
        commit(charge_id);
        ::close(fd);
    } else {
        ::close(fd);
    }
}

static int start_ledger_server() {
    int listen_fd = ::socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    ::setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = 0;  // let the kernel choose
    ::bind(listen_fd, (struct sockaddr*)&addr, sizeof(addr));
    ::listen(listen_fd, 128);

    std::thread([listen_fd] {
        for (;;) {
            int fd = ::accept(listen_fd, nullptr, nullptr);
            if (fd < 0) return;
            std::thread(serve_connection, fd).detach();
        }
    }).detach();
    return listen_fd;
}

static int port_of(int fd) {
    struct sockaddr_in addr;
    socklen_t len = sizeof(addr);
    ::getsockname(fd, (struct sockaddr*)&addr, &len);
    return ntohs(addr.sin_port);
}

// --- the client, one phase at a time ----------------------------------------

enum Kind { SUCCESS, SAFE, AMBIGUOUS };

struct Outcome {
    Kind kind;
    std::string label;
};

static Outcome ok_outcome(int code) {
    return {SUCCESS, "SUCCESS(" + std::to_string(code) + ")"};
}
static Outcome safe(const char* phase, int err) {
    return {SAFE, std::string("SAFE(") + std::strerror(err) + " [" + phase + "])"};
}
static Outcome unknown(const char* phase, const std::string& detail) {
    return {AMBIGUOUS, std::string("AMBIGUOUS(") + detail + " [" + phase + "])"};
}

// Non-blocking connect + poll, because connect(2) does not honour SO_SNDTIMEO.
// Returns 0 on success, otherwise sets errno.
static int connect_with_timeout(int fd, const struct sockaddr_in& addr, int timeout_ms) {
    int flags = ::fcntl(fd, F_GETFL, 0);
    ::fcntl(fd, F_SETFL, flags | O_NONBLOCK);

    int rc = ::connect(fd, (const struct sockaddr*)&addr, sizeof(addr));
    if (rc == 0) {
        ::fcntl(fd, F_SETFL, flags);
        return 0;
    }
    if (errno != EINPROGRESS) return -1;

    struct pollfd pfd;
    pfd.fd = fd;
    pfd.events = POLLOUT;
    rc = ::poll(&pfd, 1, timeout_ms);
    if (rc == 0) {
        errno = ETIMEDOUT;
        return -1;
    }
    if (rc < 0) return -1;

    int sock_err = 0;
    socklen_t len = sizeof(sock_err);
    ::getsockopt(fd, SOL_SOCKET, SO_ERROR, &sock_err, &len);
    ::fcntl(fd, F_SETFL, flags);
    if (sock_err != 0) {
        errno = sock_err;
        return -1;
    }
    return 0;
}

static Outcome attempt(int port, const std::string& path) {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return unknown("socket", std::strerror(errno));

    struct sockaddr_in addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons((uint16_t)port);

    // PHASE 1 - connect. If this fails, nothing was sent. Provably safe.
    if (connect_with_timeout(fd, addr, CLIENT_TIMEOUT_MS) != 0) {
        int e = errno;
        ::close(fd);
        return safe("connect", e);
    }

    struct timeval tv;
    tv.tv_sec = CLIENT_TIMEOUT_MS / 1000;
    tv.tv_usec = (CLIENT_TIMEOUT_MS % 1000) * 1000;
    ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    // PHASE 2 - send. Zero bytes written is provably safe; a partial write is
    // not, because the peer may already hold a complete request.
    std::string request = "GET " + path +
                          " HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n";
    size_t written = 0;
    while (written < request.size()) {
        ssize_t n = ::send(fd, request.data() + written, request.size() - written, 0);
        if (n > 0) {
            written += (size_t)n;
            continue;
        }
        int e = errno;
        if (n < 0 && e == EINTR) continue;
        ::close(fd);
        if (written == 0) return safe("send", e);
        return unknown("partial-send", std::strerror(e));
    }

    // PHASE 3 - receive. Anything that goes wrong from here is unknowable: the
    // request was delivered, and the outcome lives on the far side of whatever
    // just broke.
    std::string response;
    char buf[4096];
    for (;;) {
        ssize_t n = ::recv(fd, buf, sizeof(buf), 0);
        if (n > 0) {
            response.append(buf, (size_t)n);
            continue;
        }
        if (n == 0) break;  // clean EOF
        if (errno == EINTR) continue;
        int e = errno;
        ::close(fd);
        if (e == EAGAIN || e == EWOULDBLOCK) return unknown("recv", "timeout");
        return unknown("recv", std::strerror(e));
    }
    ::close(fd);

    if (response.empty()) return unknown("recv", "closed with no response");
    int code = 0;
    if (std::sscanf(response.c_str(), "HTTP/1.%*d %d", &code) != 1)
        return unknown("recv", "unparseable response");
    return ok_outcome(code);
}

// --- phases -----------------------------------------------------------------

struct PhaseResult {
    int duplicates;
    int unresolved;
};

static PhaseResult run_phase(const char* tag, const char* name, const char* note,
                             int port, int closed_port, bool retry_ambiguous) {
    size_t before = ledger_size();
    int unresolved = 0;

    std::printf("\n  %s\n  %s\n", name, note);
    std::printf("  %-20s %-44s %9s %12s\n", "fault", "client verdict", "attempts",
                "ledger rows");

    for (int m = 0; m < MODE_COUNT; m++) {
        const std::string mode = MODES[m];
        size_t mode_before = ledger_size();
        int attempts = 0;
        std::map<std::string, int> counts;
        int target = (mode == "refused") ? closed_port : port;

        for (int i = 0; i < REQUESTS_PER_MODE; i++) {
            std::string charge_id = std::string(tag) + "-" + mode + "-" + std::to_string(i);
            std::string path = "/charge/" + mode + "/" + charge_id;
            Outcome outcome{AMBIGUOUS, "AMBIGUOUS(not attempted [none])"};
            for (int a = 0; a < MAX_ATTEMPTS; a++) {
                attempts++;
                outcome = attempt(target, path);
                if (outcome.kind == SUCCESS) break;
                if (outcome.kind == SAFE) continue;   // provably safe: try again
                if (retry_ambiguous) continue;        // the bug, made explicit
                break;                                // correct: stop, escalate
            }
            if (outcome.kind == AMBIGUOUS) unresolved++;
            counts[outcome.label]++;
        }

        std::string summary;
        for (const auto& kv : counts) {
            if (!summary.empty()) summary += ", ";
            summary += std::to_string(kv.second) + "x " + kv.first;
        }
        std::printf("  %-20s %-44s %9d %12zu\n", mode.c_str(), summary.c_str(),
                    attempts, ledger_size() - mode_before);
    }

    std::map<std::string, int> seen;
    {
        std::lock_guard<std::mutex> lock(g_ledger_mutex);
        for (size_t i = before; i < g_ledger.size(); i++) seen[g_ledger[i]]++;
    }
    int duplicates = 0;
    int rows = 0;
    for (const auto& kv : seen) {
        rows += kv.second;
        if (kv.second > 1) duplicates += kv.second - 1;
    }
    std::printf("  ledger rows written this phase : %d\n", rows);
    std::printf("  DUPLICATE CHARGES              : %d   <- created by this client's retries\n",
                duplicates);
    std::printf("  unresolved ambiguous outcomes  : %d   <- caller cannot tell whether these happened\n",
                unresolved);
    return {duplicates, unresolved};
}

static int find_closed_port() {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = 0;
    ::bind(fd, (struct sockaddr*)&addr, sizeof(addr));
    int port = port_of(fd);
    ::close(fd);
    return port;
}

int main() {
    // Writing to a socket the peer has RST'd raises SIGPIPE, whose default
    // action is to kill the process. Every network program in C has to do this
    // and it is a genuine production outage waiting for the day a dependency
    // starts resetting connections.
    std::signal(SIGPIPE, SIG_IGN);

    int listen_fd = start_ledger_server();
    int port = port_of(listen_fd);
    int closed_port = find_closed_port();

    std::string bar(78, '=');
    std::printf("%s\nLayer 4 · Topic 1 — partial failure and the ambiguous result (C++)\n%s\n",
                bar.c_str(), bar.c_str());
    std::printf("  ledger        : 127.0.0.1:%d  (in-process, holds server-side truth)\n", port);
    std::printf("  closed port   : 127.0.0.1:%d  (for the connect-refused case)\n", closed_port);
    std::printf("  client timeout: %dms   slow response: %dms   max attempts: %d\n",
                CLIENT_TIMEOUT_MS, SLOW_RESPONSE_MS, MAX_ATTEMPTS);
    std::printf("  phase detection: which syscall returned -1\n");

    PhaseResult naive = run_phase("p1", "phase 1 — retry on any error",
                                  "`if (rc != 0) continue;` -- compiles clean at -Wall -Wextra",
                                  port, closed_port, true);
    PhaseResult fixed = run_phase("p2", "phase 2 — retry only connect/zero-byte-send failures",
                                  "the only two cases where the kernel proves nothing was sent",
                                  port, closed_port, false);

    std::string dash(78, '-');
    std::printf("\n%s\n", dash.c_str());
    std::printf("  duplicate charges    phase 1: %-6d phase 2: %d\n", naive.duplicates,
                fixed.duplicates);
    std::printf("  unresolved ambiguity phase 1: %-6d phase 2: %d\n", naive.unresolved,
                fixed.unresolved);
    std::printf(
        "\n  Nothing in this file prevented phase 1. Compare the Rust program, where\n"
        "  the same mistake is a compile error. Same hazard, same fix, and the only\n"
        "  difference is whether the language will let you ship it.\n");

    {
        std::lock_guard<std::mutex> lock(g_held_mutex);
        for (int fd : g_held_fds) ::close(fd);
    }
    ::close(listen_fd);
    return 0;
}
