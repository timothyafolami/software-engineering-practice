// Layer 2 · Topic 7 - C++'s contribution to the SYN table.
//
// With libcurl, the easy handle IS the pool: reuse one CURL* and curl reuses
// the connection; allocate a new handle per request and you get a fresh TCP
// handshake every time. That is Topic 1's httpx.Client()-in-the-handler bug,
// in C, with the mechanism completely visible -- one line decides it.
//
// By default this does the REUSED-handle run only, so its row in the table is
// comparable with the other five clients. Set LAB_CONTRAST=1 and it runs the
// per-request-handle version first, which is worth doing once: the connection
// count for this row jumps from 1 to LAB_REQUESTS+1 and the timing shows you
// what those handshakes cost.
//
//   c++ -O2 -std=c++17 -o /tmp/syn_client_cpp syn_client.cpp -lcurl
//   LAB_URL=http://127.0.0.1:8000/work /tmp/syn_client_cpp
//   LAB_URL=... LAB_CONTRAST=1 /tmp/syn_client_cpp
#include <curl/curl.h>

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>

static size_t discard(void*, size_t size, size_t nmemb, void*) { return size * nmemb; }

static long run(const std::string& url, int n, bool reuse_handle) {
    auto t0 = std::chrono::steady_clock::now();
    CURL* shared = reuse_handle ? curl_easy_init() : nullptr;

    for (int i = 0; i < n; ++i) {
        CURL* h = reuse_handle ? shared : curl_easy_init();
        curl_easy_setopt(h, CURLOPT_URL, url.c_str());
        curl_easy_setopt(h, CURLOPT_WRITEFUNCTION, discard);
        curl_easy_setopt(h, CURLOPT_TIMEOUT, 10L);
        CURLcode rc = curl_easy_perform(h);
        if (rc != CURLE_OK) {
            std::fprintf(stderr, "request %d failed: %s\n", i, curl_easy_strerror(rc));
            std::exit(1);
        }
        if (!reuse_handle) curl_easy_cleanup(h);
    }
    if (shared) curl_easy_cleanup(shared);

    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now() - t0).count();
}

int main() {
    const char* env_url = std::getenv("LAB_URL");
    std::string url = env_url ? env_url : "http://127.0.0.1:8000/work";
    int n = std::getenv("LAB_REQUESTS") ? std::atoi(std::getenv("LAB_REQUESTS")) : 30;

    const bool contrast = std::getenv("LAB_CONTRAST") != nullptr;

    curl_global_init(CURL_GLOBAL_DEFAULT);
    long fresh = contrast ? run(url, n, false) : -1;
    long reused = run(url, n, true);
    curl_global_cleanup();

    if (contrast) {
        std::printf("libcurl: %d requests, new handle each %ld ms, one reused handle %ld ms\n",
                    n, fresh, reused);
    } else {
        std::printf("libcurl: one reused easy handle, %d requests in %ld ms\n", n, reused);
    }
    return 0;
}
