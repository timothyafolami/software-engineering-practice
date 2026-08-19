// Layer 10 - Topic 2: does hanging up actually free the KV blocks? (C++)
//
// What this demonstrates
//     The same experiment as the other five, in the language that has no
//     cancellation mechanism at all -- which is exactly why it is here. A
//     thread blocked in read() on the upstream socket learns nothing about
//     the downstream one. Nobody is going to cancel anything for you, so
//     the fix has to be spelled out in full:
//
//       /naive       connect upstream, read it to EOF, write the result to
//                    the client. The thread is parked in read() the whole
//                    time. When the client leaves, this code has no way to
//                    find out and no way to react if it did.
//       /cancelling  a watcher thread poll()s the CLIENT socket for EOF,
//                    and on EOF calls shutdown(upstream_fd, SHUT_RDWR).
//                    That forces the blocked read() in the worker thread to
//                    return, and closes the upstream connection so the
//                    engine frees the sequence.
//
//     Every other runtime in this topic is doing some version of that
//     watcher for you: Go's net/http cancels r.Context(), Node fires
//     req.on('close'), tokio drops the losing future, Starlette throws into
//     the generator, the JVM interrupts the virtual thread. Here you can
//     see the machinery, because you have to write it.
//
// What to look for
//     - The fix is a thread and a shutdown() call, and nothing about the
//       "fast, compiled language" helped. This is the same argument as
//       Layer 1's C++ pairing: the failure mode is about scheduling and
//       ownership, not about speed.
//     - shutdown() rather than close(): close() on an fd another thread is
//       blocked in read() on is a use-after-free waiting to happen if that
//       fd number gets reused. shutdown() leaves the descriptor valid and
//       makes the pending read return 0.
//
// macOS note: this uses poll(2), which exists on Darwin. epoll(7) does not
// -- see Layer 1's portability notes. Nothing here needs an event loop
// anyway; the point is the absence of one.
//
// Build and run (no arguments, binds 127.0.0.1 only):
//     c++ -O2 -std=c++20 -pthread -o /tmp/cancel_cpp cpp/cancel_propagation.cpp \
//       && /tmp/cancel_cpp

#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

constexpr int kTokens = 40;
constexpr auto kTokenInterval = std::chrono::milliseconds(100);  // 4.0s of decode
constexpr auto kClientHangsUpAfter = std::chrono::milliseconds(500);

using Clock = std::chrono::steady_clock;

struct Observation {
    bool aborted;
    int tokens;
    double seconds;
};

std::mutex g_ledger_mu;
std::vector<Observation> g_ledger;

void record(Observation o) {
    std::lock_guard<std::mutex> lock(g_ledger_mu);
    g_ledger.push_back(o);
}

int listen_on_ephemeral_port(uint16_t* port_out) {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    ::setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = 0;
    ::bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
    ::listen(fd, 16);
    socklen_t len = sizeof(addr);
    ::getsockname(fd, reinterpret_cast<sockaddr*>(&addr), &len);
    *port_out = ntohs(addr.sin_port);
    return fd;
}

int connect_to(uint16_t port) {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons(port);
    if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        ::close(fd);
        return -1;
    }
    return fd;
}

// Has the peer closed its end? POLLIN with a zero-length peek is EOF.
// MSG_PEEK so this never consumes a byte the caller still wants.
bool peer_closed(int fd) {
    pollfd p{fd, POLLIN | POLLHUP, 0};
    if (::poll(&p, 1, 0) <= 0) return false;
    if (p.revents & (POLLHUP | POLLERR | POLLNVAL)) return true;
    char c;
    ssize_t n = ::recv(fd, &c, 1, MSG_PEEK | MSG_DONTWAIT);
    return n == 0;
}

std::string read_request_head(int fd) {
    char buf[2048];
    ssize_t n = ::recv(fd, buf, sizeof(buf) - 1, 0);
    if (n <= 0) return {};
    buf[n] = '\0';
    return std::string(buf, static_cast<size_t>(n));
}

// ---------------------------------------------------------------------------
// The stub model server.
// ---------------------------------------------------------------------------
void upstream_connection(int fd) {
    read_request_head(fd);
    const char* head =
        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n";
    ::send(fd, head, std::strlen(head), 0);

    const auto start = Clock::now();
    int sent = 0;
    bool aborted = false;
    for (int i = 0; i < kTokens; ++i) {
        if (peer_closed(fd)) {
            aborted = true;
            break;
        }
        std::string chunk = "data: token " + std::to_string(i) + "\n\n";
        // MSG_NOSIGNAL does not exist on Darwin, so SIGPIPE is ignored in
        // main() and this send() returns -1/EPIPE instead of killing us.
        if (::send(fd, chunk.data(), chunk.size(), 0) < 0) {
            aborted = true;
            break;
        }
        sent = i + 1;
        std::this_thread::sleep_for(kTokenInterval);
    }
    record({aborted, sent,
            std::chrono::duration<double>(Clock::now() - start).count()});
    ::close(fd);
}

// ---------------------------------------------------------------------------
// The gateway.
// ---------------------------------------------------------------------------
void gateway_connection(int client_fd, uint16_t upstream_port) {
    const std::string head = read_request_head(client_fd);
    const bool cancelling = head.find("/cancelling") != std::string::npos;

    int up_fd = connect_to(upstream_port);
    if (up_fd < 0) {
        ::close(client_fd);
        return;
    }
    const char* req =
        "POST /completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n";
    ::send(up_fd, req, std::strlen(req), 0);

    std::atomic<bool> done{false};
    std::thread watcher;
    if (cancelling) {
        // The fix, in full. No runtime is going to do this for you.
        watcher = std::thread([client_fd, up_fd, &done] {
            while (!done.load(std::memory_order_relaxed)) {
                if (peer_closed(client_fd)) {
                    // shutdown(), not close(): the worker thread is blocked
                    // in recv() on this descriptor. shutdown() makes that
                    // recv return 0 while the descriptor stays valid, so
                    // there is no window for the fd number to be reused
                    // under the blocked thread.
                    ::shutdown(up_fd, SHUT_RDWR);
                    return;
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
            }
        });
    }

    // Read the upstream to EOF, buffering. This is the blocking call the
    // whole topic is about: without the watcher above, this thread cannot
    // learn anything about the client while it sits here.
    std::string body;
    char buf[4096];
    for (;;) {
        ssize_t n = ::recv(up_fd, buf, sizeof(buf), 0);
        if (n <= 0) break;
        body.append(buf, static_cast<size_t>(n));
    }
    done.store(true, std::memory_order_relaxed);
    if (watcher.joinable()) watcher.join();

    ::send(client_fd, body.data(), body.size(), 0);
    ::close(up_fd);
    ::close(client_fd);
}

void accept_loop(int listen_fd, uint16_t upstream_port, bool is_gateway) {
    for (;;) {
        int fd = ::accept(listen_fd, nullptr, nullptr);
        if (fd < 0) return;
        if (is_gateway) {
            std::thread(gateway_connection, fd, upstream_port).detach();
        } else {
            std::thread(upstream_connection, fd).detach();
        }
    }
}

// A raw socket, on purpose: a client library's timeout returns control to
// your code without necessarily closing the TCP connection, so the server
// would see nothing and the experiment would measure something else.
void hang_up_on(uint16_t gateway_port, const char* path) {
    int fd = connect_to(gateway_port);
    if (fd < 0) return;
    std::string req = std::string("POST ") + path +
                      " HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n";
    ::send(fd, req.data(), req.size(), 0);
    const auto deadline = Clock::now() + kClientHangsUpAfter;
    char buf[4096];
    while (Clock::now() < deadline) {
        pollfd p{fd, POLLIN, 0};
        if (::poll(&p, 1, 20) > 0 && ::recv(fd, buf, sizeof(buf), 0) <= 0) break;
    }
    ::close(fd);  // the hang-up
}

}  // namespace

int main() {
    // A send() to a socket the peer closed raises SIGPIPE by default, which
    // kills the process. Ignore it and take the -1/EPIPE return instead.
    ::signal(SIGPIPE, SIG_IGN);

    uint16_t upstream_port = 0;
    uint16_t gateway_port = 0;
    int upstream_ln = listen_on_ephemeral_port(&upstream_port);
    int gateway_ln = listen_on_ephemeral_port(&gateway_port);
    std::thread(accept_loop, upstream_ln, upstream_port, false).detach();
    std::thread(accept_loop, gateway_ln, upstream_port, true).detach();

    std::printf("C++ / POSIX sockets - cancellation on client disconnect\n");
    std::printf("  upstream streams %d tokens x %lldms = %.1fs of decode\n", kTokens,
                static_cast<long long>(kTokenInterval.count()),
                kTokens * kTokenInterval.count() / 1000.0);
    std::printf("  client hangs up after %.1fs\n\n",
                kClientHangsUpAfter.count() / 1000.0);
    std::printf("  %-14s %-16s %14s %13s %8s\n", "handler", "upstream saw",
                "tokens decoded", "upstream ran", "wasted");
    std::printf("  ----------------------------------------------------------------------\n");

    for (const char* path : {"/naive", "/cancelling"}) {
        {
            std::lock_guard<std::mutex> lock(g_ledger_mu);
            g_ledger.clear();
        }
        hang_up_on(gateway_port, path);

        const auto deadline =
            Clock::now() + kTokenInterval * kTokens + std::chrono::seconds(1);
        Observation obs{false, 0, 0.0};
        for (;;) {
            {
                std::lock_guard<std::mutex> lock(g_ledger_mu);
                if (!g_ledger.empty()) {
                    obs = g_ledger.front();
                    break;
                }
            }
            if (Clock::now() > deadline) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }

        const double wasted =
            std::max(0.0, obs.seconds - kClientHangsUpAfter.count() / 1000.0);
        std::printf("  %-14s %-16s %14d %12.2fs %7.2fs\n", path,
                    obs.aborted ? "cancelled" : "nothing", obs.tokens, obs.seconds,
                    wasted);
    }

    std::printf("\n");
    std::printf("  'wasted' is decode time spent on a response nobody read. On a\n");
    std::printf("  loaded server those KV blocks stayed allocated the whole time,\n");
    std::printf("  so the scheduler could not admit somebody who was still waiting.\n\n");
    std::printf("  The fix cost one thread and one shutdown() call. Every other\n");
    std::printf("  runtime in this topic ships that watcher; here you write it.\n");
    return 0;
}
