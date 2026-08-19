# Layer 7 · Topic 7 — supply chain (C++/vcpkg): a portfile is a CMake SCRIPT.
#
# Blocked on this machine: vcpkg is not installed. When vcpkg installs this
# port it EXECUTES this file on your machine to fetch and build -- there is no
# sandbox. The ecosystem norm of building from source means arbitrary code
# execution at install is not an edge case, it is the design. The line below
# stands in for "run this stranger's build logic".
message(STATUS "[portfile.cmake] executing during vcpkg install -- arbitrary CMake, as you")

# A real portfile downloads, patches and builds. Every abstraction the other
# five languages put between you and 'run this stranger's build script' is a
# convenience over exactly this.
# vcpkg_from_github(OUT_SOURCE_PATH SOURCE_PATH REPO some/lib REF v1 SHA512 ...)
