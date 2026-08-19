// Layer 7 · Topic 7 — supply chain (Java/Gradle): the build script IS a program.
//
// Blocked on this machine: Gradle is not installed. With Gradle present:
//     gradle help          # this file's top-level code runs at CONFIGURATION time
// and the println below fires before any task executes -- proving the build
// script is arbitrary Kotlin/Groovy, and any plugin it applies runs too. Maven
// is milder but similar: it executes PLUGINS during the build lifecycle,
// resolved from the same repositories as your dependencies.
//
// Java's mitigating feature is the strongest artifact-verification story of the
// six -- signed artifacts and checksums have been Maven Central policy for a
// long time -- and its weakness is that verifying them is opt-in.

println("[build.gradle.kts] configuration-time code executed -- this is arbitrary code")

plugins {
    java
}

// A dependency block declares coordinates; the PLUGINS and any `buildscript`
// blocks are what execute. Verifying signatures is opt-in:
//   dependencyVerification, or Maven's <checksumPolicy>fail</checksumPolicy>
repositories { mavenCentral() }
