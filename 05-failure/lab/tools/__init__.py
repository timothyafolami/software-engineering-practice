"""Layer 5 lab tooling: k6 CSV plotters and the zombie report.

Importable as a package (`python -m tools.zombie_report` inside a container,
where compose mounts this directory at /srv/tools) and runnable as plain
scripts from the host (`python3 tools/plot_knee.py out/ramp.csv`).
"""
