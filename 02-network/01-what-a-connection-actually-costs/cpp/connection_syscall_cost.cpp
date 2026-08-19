// Layer 2 · Topic 1 - What a connection costs, one syscall at a time.
//
// C++ is here because it is the only language in this lab with nothing
// between your code and the kernel. Every other entry in this topic
// measures "a request"; this one measures socket(), connect(), write(),
// read() and close() separately, so you can see exactly which of them a
// warm pool skips and what the remainder actually costs.
//
// That decomposition is the whole argument for pooling stated precisely:
// WARM does not skip "some overhead", it skips socket() + connect() +
// close() and nothing else. On loopback that is a few tens of microseconds
// of kernel work. Across a real link connect() additionally waits a full
// round trip, and a TLS handshake on top waits another -- which is why the
// same code that looks fine in a local benchmark falls over in production.
//
// Portability: this is POSIX sockets, and it builds and runs on Darwin as
// written. Nothing here needs epoll, /proc, or cgroups. One Darwin detail
// worth knowing because it breaks otherwise-portable code: htons/htonl/ntohs
// are function-like MACROS in <sys/_endian.h> on macOS, not functions, so the
// habit of writing `::htons(...)` to name the global namespace does not
// compile here. They are written unqualified below for that reason.
//
// What to look for in the output: the per-syscall table. connect() and
// close() are the columns that disappear when you pool.
//
// Build & run:
//   clang++ -O2 -std=c++20 -pthread -o /tmp/conn_cost connection_syscall_cost.cpp && /tmp/conn_cost

#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

using Clock = std::chrono::steady_clock;

namespace {

constexpr int kRequests = 200;
constexpr const char* kBody = "{\"ok\":true}";

std::atomic<int> g_accepted{0};

double ms_since(Clock::time_point start) {
    return std::chrono::duration<double, std::milli>(Clock::now() - start).count();
}

// A deliberately minimal HTTP/1.1 server. It honours `Connection: close`
// so the COLD client gets the behaviour a non-pooling client really gets.
void serve_connection(int fd) {
    std::string buffer;
    char chunk[4096];
    for (;;) {
        ssize_t n = ::read(fd, chunk, sizeof(chunk));
        if (n <= 0) break;
        buffer.append(chunk, static_cast<size_t>(n));

        // Handle every complete request sitting in the buffer.
        for (;;) {
            size_t end = buffer.find("\r\n\r\n");
            if (end == std::string::npos) break;
            std::string request = buffer.substr(0, end + 4);
            buffer.erase(0, end + 4);

            std::string lowered = request;
            std::transform(lowered.begin(), lowered.end(), lowered.begin(), ::tolower);
            bool close_requested = lowered.find("connection: close") != std::string::npos;

            char response[256];
            int len = std::snprintf(response, sizeof(response),
                                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                    "Content-Length: %zu\r\n%s\r\n%s",
                                    std::strlen(kBody),
                                    close_requested ? "Connection: close\r\n" : "",
                                    kBody);
            if (::write(fd, response, static_cast<size_t>(len)) < 0) {
                ::close(fd);
                return;
            }
            if (close_requested) {
                ::close(fd);
                return;
            }
        }
    }
    ::close(fd);
}

int start_server() {
    int listen_fd = ::socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    ::setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = 0;  // let the kernel choose
    ::bind(listen_fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
    // Backlog 512, not the textbook 5. A backlog of 5 with 200 cold connects
    // makes the kernel drop SYNs and you end up measuring your own accept
    // queue instead of the handshake.
    ::listen(listen_fd, 512);

    socklen_t len = sizeof(addr);
    ::getsockname(listen_fd, reinterpret_cast<sockaddr*>(&addr), &len);
    int port = ntohs(addr.sin_port);

    std::thread([listen_fd] {
        for (;;) {
            int fd = ::accept(listen_fd, nullptr, nullptr);
            if (fd < 0) return;
            g_accepted.fetch_add(1, std::memory_order_relaxed);
            std::thread(serve_connection, fd).detach();
        }
    }).detach();

    return port;
}

struct Phases {
    std::vector<double> socket_us, connect_us, write_us, read_us, close_us, total_us;
};

void read_one_response(int fd) {
    // Every response here is small and arrives in one segment on loopback,
    // so one read() is enough. A real client must loop until Content-Length
    // bytes have arrived -- see Topic 3 on why "read timeout" means "time to
    // the NEXT byte", not "time to the whole body".
    char buf[4096];
    ::read(fd, buf, sizeof(buf));
}

int connect_to(int port) {
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons(static_cast<uint16_t>(port));
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
        ::close(fd);
        return -1;
    }
    return fd;
}

Phases drive_cold(int port) {
    Phases p;
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons(static_cast<uint16_t>(port));

    const char* request =
        "GET /thing HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n";

    for (int i = 0; i < kRequests; i++) {
        auto t_total = Clock::now();

        auto t = Clock::now();
        int fd = ::socket(AF_INET, SOCK_STREAM, 0);
        p.socket_us.push_back(ms_since(t) * 1000);

        t = Clock::now();
        ::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
        p.connect_us.push_back(ms_since(t) * 1000);

        int one = 1;
        ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

        t = Clock::now();
        ::write(fd, request, std::strlen(request));
        p.write_us.push_back(ms_since(t) * 1000);

        t = Clock::now();
        read_one_response(fd);
        p.read_us.push_back(ms_since(t) * 1000);

        t = Clock::now();
        ::close(fd);
        p.close_us.push_back(ms_since(t) * 1000);

        p.total_us.push_back(ms_since(t_total) * 1000);
    }
    return p;
}

Phases drive_warm(int port) {
    Phases p;
    const char* request = "GET /thing HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n";

    auto t = Clock::now();
    int fd = connect_to(port);
    double setup_us = ms_since(t) * 1000;
    int one = 1;
    ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

    for (int i = 0; i < kRequests; i++) {
        auto t_total = Clock::now();
        p.socket_us.push_back(0);   // not called: the socket already exists
        p.connect_us.push_back(0);  // not called: this is the saving

        auto t2 = Clock::now();
        ::write(fd, request, std::strlen(request));
        p.write_us.push_back(ms_since(t2) * 1000);

        t2 = Clock::now();
        read_one_response(fd);
        p.read_us.push_back(ms_since(t2) * 1000);

        p.close_us.push_back(0);  // not called: the connection is kept
        p.total_us.push_back(ms_since(t_total) * 1000);
    }
    ::close(fd);
    std::printf("    (one-time setup for the whole warm run: %.1f us)\n", setup_us);
    return p;
}

double mean(const std::vector<double>& v) {
    double sum = 0;
    for (double x : v) sum += x;
    return v.empty() ? 0 : sum / static_cast<double>(v.size());
}

double percentile(std::vector<double> v, double f) {
    if (v.empty()) return 0;
    std::sort(v.begin(), v.end());
    size_t i = std::min(v.size() - 1, static_cast<size_t>(static_cast<double>(v.size()) * f));
    return v[i];
}

void report(const char* label, const Phases& p, int connections) {
    std::printf("  %s\n", label);
    std::printf("    requests issued        %zu\n", p.total_us.size());
    std::printf("    TCP connections opened %d\n", connections);
    std::printf("    %-10s %10s %10s\n", "syscall", "mean us", "p99 us");
    std::printf("    %-10s %10.1f %10.1f\n", "socket()", mean(p.socket_us), percentile(p.socket_us, 0.99));
    std::printf("    %-10s %10.1f %10.1f\n", "connect()", mean(p.connect_us), percentile(p.connect_us, 0.99));
    std::printf("    %-10s %10.1f %10.1f\n", "write()", mean(p.write_us), percentile(p.write_us, 0.99));
    std::printf("    %-10s %10.1f %10.1f\n", "read()", mean(p.read_us), percentile(p.read_us, 0.99));
    std::printf("    %-10s %10.1f %10.1f\n", "close()", mean(p.close_us), percentile(p.close_us, 0.99));
    std::printf("    %-10s %10.1f %10.1f\n", "TOTAL", mean(p.total_us), percentile(p.total_us, 0.99));
}

}  // namespace

int main() {
    int port = start_server();
    std::this_thread::sleep_for(std::chrono::milliseconds(50));

    std::printf("==============================================================================\n");
    std::printf("C++: the cost of a connection, decomposed into syscalls\n");
    std::printf("==============================================================================\n");
    std::printf("  server 127.0.0.1:%d   %d requests\n\n", port, kRequests);

    int before = g_accepted.load();
    Phases cold = drive_cold(port);
    report("COLD - socket/connect/write/read/close, every request", cold, g_accepted.load() - before);

    std::printf("\n");
    before = g_accepted.load();
    Phases warm = drive_warm(port);
    report("WARM - one connection, 200 requests down it", warm, g_accepted.load() - before);

    std::printf("\n  Read the connect() row twice.\n");
    std::printf("    On loopback connect() completes inside the kernel with no packet\n");
    std::printf("    ever leaving the machine, so what you measured is pure CPU. Over a\n");
    std::printf("    real link connect() blocks for one full round trip -- 0.4 ms inside\n");
    std::printf("    a datacenter, 30 ms across a region, 100 ms+ on mobile -- and a TLS\n");
    std::printf("    handshake adds another. Substitute your real RTT into that row and\n");
    std::printf("    you have the production cost of not pooling, per request.\n");
    std::printf("\n    The close() row matters too, on the client side specifically: the\n");
    std::printf("    end that closes first holds the socket in TIME_WAIT for ~60 s. A\n");
    std::printf("    client doing this at a few thousand requests per second runs out of\n");
    std::printf("    ephemeral ports, which arrives as EADDRNOTAVAIL and looks nothing\n");
    std::printf("    like a pooling bug. Count them with Topic 7's socket-state report.\n");
    return 0;
}
