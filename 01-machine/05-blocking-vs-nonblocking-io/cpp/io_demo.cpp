// Layer 1 - Blocking vs non-blocking IO, C++ version -- and the only
// version in this lab that calls epoll directly instead of trusting a
// runtime to be doing it somewhere underneath. Every other language's
// "concurrent" client in this topic (asyncio, Promise.all, goroutines,
// tokio) is built on exactly the mechanism spelled out explicitly below:
// make every socket non-blocking, register them all with one epoll
// instance, and call epoll_wait in a loop, handling whichever sockets are
// ready on each pass.
#include <arpa/inet.h>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>
#include <vector>

constexpr int RESPONSE_DELAY_MS = 100;
constexpr int N = 20;

int start_server() {
    int server_fd = socket(AF_INET, SOCK_STREAM, 0);
    int opt = 1;
    setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = inet_addr("127.0.0.1");
    addr.sin_port = 0; // ask the OS for a free port
    bind(server_fd, (sockaddr*)&addr, sizeof(addr));
    listen(server_fd, 128);

    socklen_t len = sizeof(addr);
    getsockname(server_fd, (sockaddr*)&addr, &len);
    int port = ntohs(addr.sin_port);

    std::thread([server_fd] {
        while (true) {
            int client = accept(server_fd, nullptr, nullptr);
            if (client < 0) return;
            std::thread([client] {
                char buf[1024];
                read(client, buf, sizeof(buf));
                std::this_thread::sleep_for(std::chrono::milliseconds(RESPONSE_DELAY_MS));
                write(client, "ok", 2);
                close(client);
            }).detach();
        }
    }).detach();

    return port;
}

void blocking_request(int port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
    connect(fd, (sockaddr*)&addr, sizeof(addr));
    write(fd, "ping", 4);
    char buf[1024];
    read(fd, buf, sizeof(buf));
    close(fd);
}

double bench_serial(int port) {
    auto start = std::chrono::high_resolution_clock::now();
    for (int i = 0; i < N; i++) blocking_request(port);
    return std::chrono::duration<double>(std::chrono::high_resolution_clock::now() - start).count();
}

// The real thing: N non-blocking sockets, one epoll instance, a loop that
// asks the kernel "which of these are ready?" instead of parking a thread
// per connection.
double bench_epoll(int port) {
    auto start = std::chrono::high_resolution_clock::now();

    int epfd = epoll_create1(0);
    std::vector<int> fds(N);
    int remaining = N;

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);

    for (int i = 0; i < N; i++) {
        int fd = socket(AF_INET, SOCK_STREAM, 0);
        fcntl(fd, F_SETFL, O_NONBLOCK); // the key line: this socket will never block the calling thread
        fds[i] = fd;
        connect(fd, (sockaddr*)&addr, sizeof(addr)); // returns immediately with EINPROGRESS
        epoll_event ev{};
        ev.events = EPOLLOUT; // first tell us when the connection completes
        ev.data.fd = fd;
        epoll_ctl(epfd, EPOLL_CTL_ADD, fd, &ev);
    }

    std::vector<bool> sent(N, false);
    epoll_event events[N];
    while (remaining > 0) {
        int n = epoll_wait(epfd, events, N, -1); // block ONLY here, waiting on ALL sockets at once
        for (int i = 0; i < n; i++) {
            int fd = events[i].data.fd;
            if (events[i].events & EPOLLOUT) {
                // connection completed (or ready to write) -- send the request,
                // then switch to watching for the response.
                write(fd, "ping", 4);
                epoll_event ev{};
                ev.events = EPOLLIN;
                ev.data.fd = fd;
                epoll_ctl(epfd, EPOLL_CTL_MOD, fd, &ev);
            } else if (events[i].events & EPOLLIN) {
                char buf[1024];
                read(fd, buf, sizeof(buf));
                epoll_ctl(epfd, EPOLL_CTL_DEL, fd, nullptr);
                close(fd);
                remaining--;
            }
        }
    }
    close(epfd);
    return std::chrono::duration<double>(std::chrono::high_resolution_clock::now() - start).count();
}

int main() {
    int port = start_server();
    double t_serial = bench_serial(port);
    double t_epoll = bench_epoll(port);
    std::printf("N=%d requests, %dms server delay each\n", N, RESPONSE_DELAY_MS);
    std::printf("serial (blocking sockets):     %.3fs  (~%.0fms/req)\n", t_serial, t_serial / N * 1000.0);
    std::printf("concurrent (raw epoll):        %.3fs\n", t_epoll);
    return 0;
}
