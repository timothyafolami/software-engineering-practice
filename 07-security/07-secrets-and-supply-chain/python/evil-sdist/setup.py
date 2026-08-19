# setup.py is ARBITRARY CODE that runs when pip builds this package. Installing
# an SDIST builds it on the CONSUMER's machine -> this runs as them. Installing
# a prebuilt WHEEL does not run setup.py at all (the wheel was already built).
# `pip install --only-binary :all:` refuses to build sdists -- a real, under-
# used control. The marker below stands in for "read the environment and POST".
import os, time
from setuptools import setup

with open("/tmp/pwned-py.txt", "a") as f:
    f.write(f"setup.py executed during build at {time.time()} as {os.environ.get('USER','?')}\n")

setup(name="evilpkg", version="1.0.0", packages=["evilpkg"])
