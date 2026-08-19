# Layer 7 · Topic 7 — supply chain (C++/Conan): a recipe is a PYTHON FILE.
#
# Blocked on this machine: Conan is not installed. `conan install .` imports and
# runs this Python on your machine to fetch and build the dependency. Same
# lesson as the vcpkg portfile: the underlying operation was always "execute a
# stranger's build program", and the package manager is just the convenience
# layer over it.
print("[conanfile.py] imported and executed during `conan install` -- arbitrary Python, as you")

from conan import ConanFile  # noqa: E402


class Demo(ConanFile):
    settings = "os", "compiler", "build_type", "arch"

    def build(self):
        # Arbitrary code, run on your machine at install/build time.
        self.output.info("conan build() step -- runs as you, no sandbox")
