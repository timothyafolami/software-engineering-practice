// Layer 7 · Topic 8 — Crypto hygiene and rate limiting (C++ / OpenSSL).
//
// One command (see How to run; links OpenSSL). C++ is where "constant time is
// a property of the EMITTED INSTRUCTIONS" stops being an abstraction (README):
// no standard constant-time compare exists, memcmp short-circuits and is
// vectorised, and a hand-rolled XOR-accumulate is correct until the optimiser
// proves it can exit early. The real answer is a crypto library's own compare
// (OpenSSL's CRYPTO_memcmp, libsodium's sodium_memcmp) -- used here for the
// constant-time row. Compile the same source at -O0 and -O2 and read the
// disassembly to see the property change.
//
// Three parts, measured at runtime: (A) sha256 vs PBKDF2 verify/sec; (B) the
// timing signal, hand-rolled naive short-circuit vs CRYPTO_memcmp; (C) rate
// limiting.
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <vector>
#include <openssl/evp.h>
#include <openssl/rand.h>
#include <openssl/crypto.h>

using Clock = std::chrono::steady_clock;
static volatile uint64_t sink = 0;

static void part_a() {
    printf("A. Hash cost (verifications/sec, measured)\n");
    const char* pw = "correct horse battery staple";
    size_t pwlen = strlen(pw);
    unsigned char out[32];

    int reps = 500000;
    auto t0 = Clock::now();
    for (int i = 0; i < reps; i++) {
        unsigned int n = 32;
        EVP_Digest(pw, pwlen, out, &n, EVP_sha256(), nullptr);
    }
    double dt = std::chrono::duration<double>(Clock::now() - t0).count();
    double sha_vps = reps / dt;
    printf("   sha256               %14.0f verify/sec\n", sha_vps);

    unsigned char salt[16];
    RAND_bytes(salt, sizeof salt);
    reps = 20;
    t0 = Clock::now();
    for (int i = 0; i < reps; i++)
        PKCS5_PBKDF2_HMAC(pw, pwlen, salt, sizeof salt, 600000, EVP_sha256(), 32, out);
    dt = std::chrono::duration<double>(Clock::now() - t0).count();
    double pb_vps = reps / dt;
    printf("   pbkdf2(600k)         %14.1f verify/sec\n", pb_vps);

    int N = 10000, K = 1000000;
    printf("   crack-time model: attacker rig N=%dx, list K=%d candidates\n", N, K);
    printf("      sha256: %.6f s to first crack\n", K / (sha_vps * N));
    printf("      pbkdf2: %.1f s to first crack  -- ~%.0fx slower per verify\n",
           K / (pb_vps * N), sha_vps / pb_vps);
    printf("   (argon2id is the OWASP first choice; it needs libsodium/argon2 here.)\n\n");
}

// Hand-rolled naive: short-circuits on first mismatch -> secret-dependent time.
static int naive_eq(const unsigned char* a, const unsigned char* b, size_t n) {
    for (size_t i = 0; i < n; i++)
        if (a[i] != b[i]) return 0;
    return 1;
}

static void part_b() {
    printf("B. Timing signal: naive short-circuit vs constant-time\n");
    unsigned char secret[32];
    RAND_bytes(secret, sizeof secret);

    auto candidate = [&](int matching, unsigned char* c) {
        RAND_bytes(c, 32);
        memcpy(c, secret, matching);
        if (matching < 32) c[matching] = secret[matching] ^ 0xFF;
    };
    auto avg_ns = [&](int which, unsigned char* cand, long reps) -> double {
        auto t0 = Clock::now();
        for (long i = 0; i < reps; i++) {
            int r = (which == 0) ? naive_eq(secret, cand, 32)
                                 : (CRYPTO_memcmp(secret, cand, 32) == 0 ? 1 : 0);
            sink += (uint64_t)r;
        }
        return std::chrono::duration<double, std::nano>(Clock::now() - t0).count() / reps;
    };

    printf("   matching leading bytes ->        avg ns/op\n");
    const char* labels[2] = {"naive_eq", "CRYPTO_memcmp"};
    for (int which = 0; which < 2; which++) {
        printf("   %-18s", labels[which]);
        for (int k : {0, 8, 16, 31}) {
            unsigned char cand[32];
            candidate(k, cand);
            printf(" k=%d:%.2f", k, avg_ns(which, cand, 3000000));
        }
        printf("\n");
    }
    printf("   (naive trends up with k; CRYPTO_memcmp flat. memcmp would also short-\n");
    printf("    circuit AND vectorise -- constant time is a property of the emitted code.)\n\n");
}

static void part_c() {
    printf("C. Rate limiting: attempts-to-first-success and effective limit\n");
    const int LIST = 1000, CORRECT_AT = 500, CONFIGURED = 10;
    struct Row { const char* mode; int workers, ips; const char* note; };
    Row rows[] = {
        {"off", 1, 1, "no limit"},
        {"redis_token_bucket", 1, 1, "shared bucket, configured=10"},
        {"inproc", 1, 1, "in-proc, 1 worker"},
        {"inproc", 4, 1, "in-proc, 4 workers -> effective 4x"},
        {"ip_keyed", 1, 50, "IP-keyed, attacker uses 50 IPs"},
    };
    for (auto& r : rows) {
        std::map<std::string, int> buckets;
        int allowed = 0; bool reached = false;
        for (int i = 1; i <= LIST; i++) {
            int ip = i % r.ips;
            bool permitted;
            std::string mode = r.mode;
            if (mode == "off") permitted = true;
            else {
                std::string key = mode == "redis_token_bucket" ? "account"
                    : mode == "inproc" ? ("w" + std::to_string(i % r.workers))
                    : ("ip" + std::to_string(ip));
                if (buckets.find(key) == buckets.end()) buckets[key] = CONFIGURED;
                permitted = buckets[key] > 0;
                if (permitted) buckets[key]--;
            }
            if (permitted) { allowed++; if (i == CORRECT_AT) reached = true; }
        }
        printf("   %-18s %-34s allowed=%-4d %s\n", r.mode, r.note, allowed,
               reached ? "reached password" : "password NOT reached");
    }
    printf("\n   effective/configured: inproc workers=4 allows ~%d vs configured %d -> 4x.\n",
           4 * CONFIGURED, CONFIGURED);
    printf("   IP-keyed with 50 IPs lets the password through -> keying on IP is a fake fix.\n\n");
}

int main() {
    printf("Layer 7 · Topic 8 — hash cost, timing signal, rate limiting\n\n");
    part_a();
    part_b();
    part_c();
    if (sink == UINT64_MAX) printf("%llu\n", (unsigned long long)sink); // keep sink live
    printf("Takeaway: password hash must be SLOW, a secret compare CONSTANT-TIME\n"
           "(CRYPTO_memcmp, not memcmp), and a rate limit keyed on the account with SHARED state.\n");
    return 0;
}
