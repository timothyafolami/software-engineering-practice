"""
Layer 1 - File descriptors, and why "too many open files" is one of the
most common production failures.

Every open socket, file, and pipe holds a slot in the process's file
descriptor table, and that table has a ceiling (RLIMIT_NOFILE). Leak
connections -- forget to close a socket in an error path, let a connection
pool grow unbounded -- and eventually every new open() or accept() fails
with EMFILE, which in most frameworks surfaces as a confusing, seemingly
unrelated error far from the actual leak.
"""
import errno
import os
import resource

soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
print(f"RLIMIT_NOFILE: soft={soft}, hard={hard}")

fds = []
try:
    while True:
        fds.append(os.open("/dev/null", os.O_RDONLY))
except OSError as e:
    if e.errno == errno.EMFILE:
        print(f"hit EMFILE ('too many open files') after opening {len(fds)} fds")
    else:
        raise
finally:
    for fd in fds:
        os.close(fd)
    print(f"closed all {len(fds)} fds; process is healthy again")
