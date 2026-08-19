// Layer 2 · Topic 3 - C++: there is no timeout abstraction, so you can see
// what one actually is.
//
// Either a timeout is an argument to a syscall -- the last parameter of
// poll(), the struct timeval in select(), SO_RCVTIMEO set with setsockopt --
// or it is a timer you run yourself and a socket you close from another
// thread. There is no third option, in any language. Every runtime in this
// topic is one of those two wearing a nicer surface.
//
// Two consequences fall straight out, and they explain the other five
// entries:
//
//   1. A "timeout" is really SOMETHING ELSE WAKING YOU UP. Nothing was
//      cancelled. Nothing was told. Your thread simply stopped waiting.
//   2. After it fires you still hold a connection in an unknown state, and
//      you must decide what to do with it. Every client library makes that
//      decision for you, silently, and it is always "throw it away" -- phase
//      C shows what happens when you make the other choice.
//
// Phases:
//   A. A deadline budget spent down three sequential hops, with poll().
//   B. What firing the timeout does to the request in flight at the server.
//   C. Reusing the socket afterwards: the late response is still in the
//      receive buffer, and you read it as the answer to your NEXT request.
//   D. The same wait expressed as SO_RCVTIMEO instead of poll(), because the
//      two are not interchangeable and the difference matters.
//
// What to look for in the output:
//   - phase A: hop 3 is not started, because its answer would arrive after
//     the caller has stopped waiting
//   - phase B: the server's FINISHED count rises for the abandoned request
//   - phase C: "asked for /marker, got path=/slow". One reused socket, every
//     response off by one from there on
//
// Portability: POSIX sockets, poll(2), setsockopt(2). No epoll, no /proc, no
// cgroups -- builds and runs unchanged on Darwin arm64 and on Linux.
//
// Build & run:
//   c++ -O2 -std=c++17 -pthread -o /tmp/polldeadline poll_deadline.cpp && /tmp/polldeadline

#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

using clock_type = std::chrono::steady_clock;
using msec = std::chrono::milliseconds;

static constexpr int SLOW_MS        = 400;   // how long the server holds /slow
static constexpr int OUTER_BUDGET   = 900;   // what we promised our caller
static constexpr int RESERVE_MS     = 100;   // held back for our own response
static constexpr int PER_HOP_CAP_MS = 500;   // the flat "library default"

static std::atomic<int> g_accepted{0};
static std::atomic<int> g_started{0};
static std::atomic<int> g_finished{0};
static std::atomic<bool> g_stop{false};

// ---------------------------------------------------------------- server --
// Minimal keep-alive HTTP/1.1: read until the blank line, echo the path back.
static void serve_conn(int fd) {
    std::string buf;
    char chunk[1024];
    for (;;) {
        auto end = buf.find("\r\n\r\n");
        if (end == std::string::npos) {
            ssize_t n = ::read(fd, chunk, sizeof(chunk));
            if (n <= 0) break;
            buf.append(chunk, static_cast<size_t>(n));
            continue;
        }

        std::string head = buf.substr(0, end);
        buf.erase(0, end + 4);

        std::string path = "/";
        auto sp1 = head.find(' ');
        if (sp1 != std::string::npos) {
            auto sp2 = head.find(' ', sp1 + 1);
            if (sp2 != std::string::npos) path = head.substr(sp1 + 1, sp2 - sp1 - 1);
        }

        if (path.rfind("/slow", 0) == 0) {
            g_started.fetch_add(1);
            // No client-disconnect check, exactly like the handlers you ship.
            std::this_thread::sleep_for(msec(SLOW_MS));
            g_finished.fetch_add(1);
        }

        std::string body = "path=" + path;
        std::string resp = "HTTP/1.1 200 OK\r\nContent-Length: " +
                           std::to_string(body.size()) +
                           "\r\nConnection: keep-alive\r\n\r\n" + body;
        if (::write(fd, resp.data(), resp.size()) < 0) break;
    }
    ::close(fd);
}

static int start_server(int& out_port) {
    int lfd = ::socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    ::setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = 0;   // let the kernel choose
    ::bind(lfd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
    ::listen(lfd, 128);

    socklen_t len = sizeof(addr);
    ::getsockname(lfd, reinterpret_cast<sockaddr*>(&addr), &len);
    out_port = ntohs(addr.sin_port);

    std::thread([lfd] {
        while (!g_stop.load()) {
            int fd = ::accept(lfd, nullptr, nullptr);
            if (fd < 0) return;
            g_accepted.fetch_add(1);
            std::thread(serve_conn, fd).detach();
        }
    }).detach();

    return lfd;
}

// ---------------------------------------------------------------- client --
static int dial(int port) {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons(static_cast<uint16_t>(port));
    if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
        ::close(fd);
        return -1;
    }
    return fd;
}

static void send_request(int fd, const std::string& path) {
    std::string req = "GET " + path + " HTTP/1.1\r\nHost: lab\r\n\r\n";
    ::write(fd, req.data(), req.size());
}

// THE TIMEOUT, in its honest form: the last argument of poll(). Returns the
// response, or an empty string if we stopped waiting. Note the word "stopped".
// Nothing was cancelled and nobody was told.
static bool read_with_deadline(int fd, int timeout_ms, std::string& out) {
    pollfd pfd{fd, POLLIN, 0};
    int rc = ::poll(&pfd, 1, timeout_ms);
    if (rc == 0) return false;            // the deadline, and only the deadline
    if (rc < 0) return false;
    char buf[4096];
    ssize_t n = ::read(fd, buf, sizeof(buf));
    if (n <= 0) return false;
    out.assign(buf, static_cast<size_t>(n));
    return true;
}

static std::string body_of(const std::string& resp) {
    auto p = resp.find("\r\n\r\n");
    return p == std::string::npos ? std::string() : resp.substr(p + 4);
}

// ----------------------------------------------------------------- phases --
static void phase_a(int port) {
    std::printf("A. A budget, spent down three sequential hops\n");
    std::printf("    promised to our caller     %5d ms\n", OUTER_BUDGET);
    std::printf("    reserved for our own work  %5d ms\n", RESERVE_MS);
    std::printf("    each hop's flat default    %5d ms  <- what a flat config would use\n\n", PER_HOP_CAP_MS);

    auto expires_at = clock_type::now() + msec(OUTER_BUDGET);
    auto t0 = clock_type::now();
    auto remaining = [&] {
        return std::chrono::duration_cast<msec>(expires_at - clock_type::now()).count();
    };

    for (int hop = 1; hop <= 3; ++hop) {
        long long slice = remaining() - RESERVE_MS;
        if (slice > PER_HOP_CAP_MS) slice = PER_HOP_CAP_MS;
        if (slice <= 0) {
            std::printf("    hop %d  slice %6lld ms  -> NOT STARTED: its answer would arrive after\n", hop, slice);
            std::printf("                              our caller has stopped waiting. Failing now is\n");
            std::printf("                              correct, and it is the line people skip.\n");
            break;
        }
        int fd = dial(port);
        send_request(fd, "/slow");
        std::string resp;
        bool ok = read_with_deadline(fd, static_cast<int>(slice), resp);
        ::close(fd);
        auto elapsed = std::chrono::duration_cast<msec>(clock_type::now() - t0).count();
        std::printf("    hop %d  slice %6lld ms  -> %s  (%lld ms elapsed, %lld ms left)\n",
                    hop, slice, ok ? "ok" : "poll() returned 0 -- deadline", elapsed, remaining());
    }
    std::printf("\n    A flat %d ms per hop would have spent %d ms on three hops, against a\n",
                PER_HOP_CAP_MS, PER_HOP_CAP_MS * 3);
    std::printf("    %d ms promise. The timeout that is not derived from the promise is\n", OUTER_BUDGET);
    std::printf("    not protecting the promise.\n");
}

static void phase_b(int port) {
    std::printf("\nB. What a fired deadline does to the request already in flight\n");
    std::this_thread::sleep_for(msec(SLOW_MS + 100));   // let phase A's abandoned hop land
    int before = g_finished.load();

    int fd = dial(port);
    send_request(fd, "/slow");
    auto t0 = clock_type::now();
    std::string resp;
    bool ok = read_with_deadline(fd, 100, resp);
    auto elapsed = std::chrono::duration_cast<msec>(clock_type::now() - t0).count();
    ::close(fd);

    std::printf("    client stopped waiting after  %lld ms (got a response: %s)\n", elapsed, ok ? "yes" : "no");
    std::this_thread::sleep_for(msec(SLOW_MS + 200));
    std::printf("    server FINISHED this request anyway: %d -> %d\n", before, g_finished.load());
    std::printf("    poll() returning 0 did not reach across the network. It woke up one\n");
    std::printf("    thread. The request is still being served, and it will still be\n");
    std::printf("    served if you retry -- which is how one slow dependency and one\n");
    std::printf("    retry policy multiply into an outage.\n");
}

static void phase_c(int port) {
    std::printf("\nC. The connection you still hold, in a state you do not know\n");

    int fd = dial(port);
    send_request(fd, "/slow");
    std::string resp;
    bool ok = read_with_deadline(fd, 100, resp);
    std::printf("    request sent, waited 100 ms for a %d ms response: %s\n",
                SLOW_MS, ok ? "answered" : "gave up");

    // The library would destroy this socket here. We are going to do the other
    // thing on purpose, because "return it to the pool" is a decision somebody
    // makes on your behalf in every other language in this topic.
    std::this_thread::sleep_for(msec(SLOW_MS + 100));  // the late answer arrives
    send_request(fd, "/marker");
    std::string second;
    read_with_deadline(fd, 1000, second);
    std::string body = body_of(second);

    std::printf("\n    reusing that same socket, we ask for   /marker\n");
    std::printf("    the response body says                 %s\n", body.c_str());
    if (body != "path=/marker") {
        std::printf("    A RESPONSE FOR A DIFFERENT REQUEST. The answer we stopped waiting\n");
        std::printf("    for was delivered anyway and sat in the receive buffer; the next\n");
        std::printf("    read took it. From here every response on this connection is off\n");
        std::printf("    by one, forever, and no error was raised at any point.\n");
        std::printf("    THIS is what your client library is protecting you from when it\n");
        std::printf("    closes a connection after a timeout, and it is why an unbounded\n");
        std::printf("    'reuse' optimisation on a timing-out path is a data-integrity bug\n");
        std::printf("    rather than a performance one.\n");
    } else {
        std::printf("    The socket came back clean -- the late response was not buffered\n");
        std::printf("    where we expected. Re-run; the race is real, it just did not land.\n");
    }
    ::close(fd);
}

static void phase_d(int port) {
    std::printf("\nD. The same wait, expressed as SO_RCVTIMEO instead of poll()\n");

    int fd = dial(port);
    send_request(fd, "/slow");
    timeval tv{};
    tv.tv_sec = 0;
    tv.tv_usec = 100 * 1000;
    ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    char buf[4096];
    auto t0 = clock_type::now();
    ssize_t n = ::read(fd, buf, sizeof(buf));
    auto elapsed = std::chrono::duration_cast<msec>(clock_type::now() - t0).count();
    int err = errno;
    ::close(fd);

    std::printf("    read() returned %zd after %lld ms, errno=%d (%s)\n", n, elapsed, err, std::strerror(err));
    std::printf("    Same wall-clock behaviour, different shape. SO_RCVTIMEO applies to\n");
    std::printf("    EVERY read on that socket, so it is a per-read timeout, not a\n");
    std::printf("    deadline: a server trickling one byte per 50 ms never trips it and\n");
    std::printf("    can hold you forever. poll() with a deadline you recompute is the\n");
    std::printf("    only one of the two that bounds the whole operation. That is exactly\n");
    std::printf("    the read-versus-total distinction this topic opened with, and here\n");
    std::printf("    it is as two different syscalls rather than two words in a doc page.\n");
}

int main() {
    int port = 0;
    int lfd = start_server(port);

    std::printf("==============================================================================\n");
    std::printf("C++: a timeout is an argument to a syscall, and nothing else\n");
    std::printf("==============================================================================\n");
    std::printf("  server on 127.0.0.1:%d, holds /slow for %d ms\n\n", port, SLOW_MS);

    phase_a(port);
    phase_b(port);
    phase_c(port);
    phase_d(port);

    std::printf("\n  For this topic's table:\n");
    std::printf("    what a fired timeout does to the in-flight request:\n");
    std::printf("      nothing. poll() returns 0 and your thread stops waiting. The request\n");
    std::printf("      is still in flight and will still be served.\n");
    std::printf("    connection reused after?\n");
    std::printf("      only if you enjoy reading the previous response as the answer to your\n");
    std::printf("      next request. Close it.\n");
    std::printf("\n  connections accepted during this run: %d\n", g_accepted.load());

    g_stop.store(true);
    ::close(lfd);
    return 0;
}
