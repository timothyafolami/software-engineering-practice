// Layer 7 · Topic 6 — SSRF: validate the connection, not the string (C++ / libcurl-adjacent).
//
// One command (plain g++, stdlib only; see How to run). This is the language
// talking to the network with nothing between it and connect(2) (README): with
// libcurl, CURLOPT_FOLLOWLOCATION is OFF by default (so C++ is accidentally
// safer on the redirect axis than Python's requests), CURLOPT_PROTOCOLS_STR
// restricts schemes, CURLOPT_RESOLVE pre-seeds DNS with an address you chose,
// and CURLOPT_OPENSOCKETFUNCTION hands you the socket before connect(2) so you
// can inspect the sockaddr itself. Every other language's abstraction wraps
// these. This program models the validator that OPENSOCKETFUNCTION would call.
//
// The finding: string_blocklist ALLOWs every internal target via an encoding it
// did not enumerate; resolve_and_pin BLOCKs them by classifying the IP.
#include <cstdint>
#include <cstdio>
#include <map>
#include <string>
#include <vector>

static const std::map<std::string, std::string> FAKE_DNS = {
    {"internal-admin", "10.7.0.10"}, {"metadata", "10.7.0.169"},
    {"allowed.test", "93.184.216.34"}, {"a.rebind.lab.test", "10.7.0.10"},
    {"localhost", "127.0.0.1"}};
static const std::vector<std::string> STRING_DENY = {"localhost", "127.0.0.1", "169.254.169.254"};

static std::string to_lower(std::string s) {
    for (char& c : s) c = (char)tolower((unsigned char)c);
    return s;
}

// Extract the host the socket would use: after scheme, after userinfo@, no port.
static std::string host_of(const std::string& url) {
    auto p = url.find("://");
    std::string rest = (p == std::string::npos) ? url : url.substr(p + 3);
    std::string authority = rest.substr(0, rest.find('/'));
    auto at = authority.rfind('@');
    if (at != std::string::npos) authority = authority.substr(at + 1);
    if (!authority.empty() && authority[0] == '[') {           // [IPv6]
        auto close = authority.find(']');
        return authority.substr(1, close - 1);
    }
    return authority.substr(0, authority.find(':'));           // strip :port
}

static bool all_digits(const std::string& s) {
    if (s.empty()) return false;
    for (char c : s) if (!isdigit((unsigned char)c)) return false;
    return true;
}

// Returns canonical IP string, or "" if the name is unknown.
static std::string canonical_ip(std::string host) {
    auto it = FAKE_DNS.find(host);
    if (it != FAKE_DNS.end()) host = it->second;
    if (all_digits(host)) {                                    // "0", "2130706433"
        uint32_t n = (uint32_t)strtoul(host.c_str(), nullptr, 10);
        char buf[32];
        snprintf(buf, sizeof buf, "%u.%u.%u.%u", (n >> 24) & 255, (n >> 16) & 255, (n >> 8) & 255, n & 255);
        return buf;
    }
    if (host.find(':') != std::string::npos) return host;       // IPv6 literal
    // dotted IPv4?
    int dots = 0; for (char c : host) if (c == '.') dots++;
    if (dots == 3) return host;
    return "";                                                  // unknown name
}

static bool is_denied(const std::string& ip) {
    if (ip.find(':') != std::string::npos) {                    // IPv6
        std::string l = to_lower(ip);
        return l == "::1" || l == "::" || l.rfind("fe80", 0) == 0 ||
               l.rfind("fc", 0) == 0 || l.rfind("fd", 0) == 0;
    }
    int a = 0, b = 0, c = 0, d = 0;
    if (sscanf(ip.c_str(), "%d.%d.%d.%d", &a, &b, &c, &d) != 4) return true; // fail closed
    return a == 127 || a == 10 || a == 0 ||
           (a == 172 && b >= 16 && b <= 31) ||
           (a == 192 && b == 168) ||
           (a == 169 && b == 254);
}

static std::string verdict_blocklist(const std::string& url) {
    std::string low = to_lower(url);
    for (const auto& d : STRING_DENY)
        if (low.find(d) != std::string::npos) return "BLOCK";
    return "ALLOW";
}

int main() {
    std::vector<std::string> payloads = {
        "http://internal-admin:8000/secrets",
        "http://10.7.0.169/latest/meta-data/iam/...",
        "http://0/secrets",
        "http://2130706433/",
        "http://[::1]:8000/",
        "http://ok.test@10.7.0.10/secrets",
        "http://a.rebind.lab.test/secrets"};

    printf("Layer 7 · Topic 6 — SSRF: string blocklist vs resolve-and-pin\n\n");
    printf("   %-44s%-11s%-13s%s\n", "payload", "blocklist", "resolve+pin", "resolved");
    int rb = 0, rp = 0;
    for (const auto& url : payloads) {
        std::string v1 = verdict_blocklist(url);
        std::string ip = canonical_ip(host_of(url));
        std::string v2 = ip.empty() ? "BLOCK" : (is_denied(ip) ? "BLOCK" : "ALLOW");
        std::string shown = ip.empty() ? "unresolvable" : ip;
        if (v1 == "ALLOW") rb++;
        if (v2 == "ALLOW") rp++;
        printf("   %-44s%-11s%-13s%s\n", url.c_str(), v1.c_str(), v2.c_str(), shown.c_str());
    }
    printf("\n   internal targets reached -- string_blocklist: %d/%zu   resolve_and_pin: %d/%zu\n",
           rb, payloads.size(), rp, payloads.size());
    printf("\nIMDS v1 vs v2: v1 returns credentials to a plain GET; v2 refuses without\n");
    printf("a PUT-obtained token -> 0 bytes. v2 raises the bar, it is not the fix.\n");
    printf("\nRead: the STRING is not the ADDRESS. In libcurl, inspect the sockaddr in\n");
    printf("CURLOPT_OPENSOCKETFUNCTION -- the one place the resolved address is yours\n");
    printf("to refuse before connect(2).\n");
    return 0;
}
