# Part B (Python) — sdist executes setup.py, wheel does not

```
rm -f /tmp/pwned-py.txt
# Building/installing the SDIST runs setup.py on your machine:
pip install --no-build-isolation ./evil-sdist && cat /tmp/pwned-py.txt

# A prebuilt WHEEL is unpacked, not executed -- installing it runs NO setup.py:
rm -f /tmp/pwned-py.txt
pip wheel --no-build-isolation -w /tmp/wh ./evil-sdist    # setup.py runs HERE, on the builder
rm -f /tmp/pwned-py.txt
pip install --force-reinstall /tmp/wh/evilpkg-*.whl       # install: no setup.py, no marker
ls /tmp/pwned-py.txt 2>/dev/null || echo "no marker: wheel install executed nothing"
```

An sdist runs `setup.py` at install time, arbitrary code, as you. A wheel does
not — "wheels only" (`pip install --only-binary :all:`) is a real control most
Python developers have never turned on. `uv` with a committed lockfile plus
hash verification is the current posture; tamper with one hash in `uv.lock` and
`uv sync` refuses (see `uvlock_tamper.md`).
