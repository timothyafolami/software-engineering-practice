"""
Layer 7 · Topic 7, Part A — Secrets: scanning, and why history is the problem
(Python, self-contained).

One command, no arguments: `python3 secret_scan.py`. gitleaks is not installed
on this machine, so this is a minimal stand-in scanner (the same idea: match
KNOWN CREDENTIAL PATTERNS, not the word "secret"). It:

  1. Plants a realistically-formatted FAKE credential -- an AKIA-shaped string,
     not SECRET=hunter2. A fake that matches no pattern would teach you that
     scanners are useless; a fake that matches teaches you what they catch.
  2. Scans the working tree and reports findings + scan time.
  3. Initializes a throwaway git repo, commits the secret, `git rm`s it, commits
     the removal, and re-scans BOTH the working tree (clean) and the full
     history -- showing the finding survives in history after removal. This is
     the whole lesson: `git rm` + `.gitignore` do not un-leak a committed key;
     rotation does.

What to look for: the working tree is clean after removal, but the history scan
still finds the credential in an earlier commit -- which is why the only number
that matters during an incident is seconds-to-rotate, not seconds-to-delete.
"""
import os
import re
import subprocess
import tempfile
import time

# Patterns a real scanner ships with. Each is a credential SHAPE.
PATTERNS = {
    "aws-access-key-id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "aws-secret-access-key": re.compile(r"\baws_secret_access_key\s*=\s*['\"]?[A-Za-z0-9/+=]{40}['\"]?"),
    "private-key-header": re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
    "generic-api-token": re.compile(r"\b(?:api[_-]?key|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{24,}['\"]", re.I),
}

# A planted, realistically-formatted FAKE credential.
PLANTED = '''# config.py -- planted fake credential (matches a scanner pattern on purpose)
AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
'''


def scan_text(text):
    hits = []
    for name, rx in PATTERNS.items():
        for m in rx.finditer(text):
            hits.append((name, m.group(0)[:24] + ("…" if len(m.group(0)) > 24 else "")))
    return hits


def scan_tree(root):
    findings = []
    for dirpath, dirs, files in os.walk(root):
        if ".git" in dirs:
            dirs.remove(".git")  # working-tree scan skips .git
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                with open(p, "r", errors="ignore") as fh:
                    for name, snippet in scan_text(fh.read()):
                        findings.append((os.path.relpath(p, root), name, snippet))
            except OSError:
                pass
    return findings


def git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def scan_history(repo):
    """Scan every blob ever committed (what `--no-git` history mode does)."""
    out = subprocess.run(["git", "log", "-p", "--all"], cwd=repo,
                         capture_output=True, text=True).stdout
    return scan_text(out)


def main():
    print("Layer 7 · Topic 7 — secret scanning and the history problem\n")
    with tempfile.TemporaryDirectory() as repo:
        secrets_path = os.path.join(repo, "config.py")
        with open(secrets_path, "w") as fh:
            fh.write(PLANTED)

        t0 = time.perf_counter()
        findings = scan_tree(repo)
        secs = time.perf_counter() - t0
        print(f"1. Working-tree scan: {len(findings)} finding(s) in {secs*1000:.1f} ms")
        for rel, name, snip in findings:
            print(f"   {rel}: {name} = {snip}")

        # Commit, remove, commit the removal.
        git(["init"], repo)
        git(["config", "user.email", "lab@lab.test"], repo)
        git(["config", "user.name", "lab"], repo)
        git(["add", "."], repo)
        git(["commit", "-m", "add config"], repo)
        git(["rm", "config.py"], repo)
        git(["commit", "-m", "remove key"], repo)

        print("\n2. After `git rm config.py` + commit:")
        wt = scan_tree(repo)
        print(f"   working tree: {len(wt)} finding(s)  <- looks clean")
        hist = scan_history(repo)
        print(f"   full history: {len(hist)} finding(s)  <- STILL THERE in commit 1")
        for name, snip in hist:
            print(f"      history hit: {name} = {snip}")

    print("\nRead: removing the file scrubs the working tree and nothing else. "
          "The credential lives in every clone's history until you rewrite it "
          "(filter-repo/BFG) AND rotate. `.gitignore` prevents an accident and "
          "prevents nothing once the file is tracked. Push protection (checked "
          "on the server, not your laptop) is what stops the next one. The only "
          "number that matters in an incident is seconds-to-rotate.")


if __name__ == "__main__":
    main()
