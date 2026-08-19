// Layer 1 - File descriptors, C++ version.
// getrlimit(2) called directly -- no parsing /proc needed, the same clean
// path Python's resource.getrlimit and Go's syscall.Getrlimit take. open()
// returns -1 and sets errno to EMFILE when the table is full; checking
// errno directly (rather than a wrapped exception type) is about as close
// to the raw kernel contract as any language in this lab gets.
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <sys/resource.h>
#include <unistd.h>
#include <vector>

int main() {
    rlimit rl{};
    getrlimit(RLIMIT_NOFILE, &rl);
    std::printf("RLIMIT_NOFILE: soft=%lu, hard=%lu\n", (unsigned long)rl.rlim_cur, (unsigned long)rl.rlim_max);

    std::vector<int> fds;
    while (true) {
        int fd = open("/dev/null", O_RDONLY);
        if (fd < 0) {
            std::printf("hit errno=%d (%s) after opening %zu fds\n", errno, strerror(errno), fds.size());
            break;
        }
        fds.push_back(fd);
    }
    for (int fd : fds) close(fd);
    std::printf("closed all %zu fds; process is healthy again\n", fds.size());
    return 0;
}
