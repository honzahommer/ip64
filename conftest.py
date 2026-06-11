import os

# Pin the zone and force geo off by default (no BIN); geo tests inject a fake
# GEO_DB. Must be set before ip64 is imported (it reads env at import time).
os.environ.setdefault("IP64_FQDN", "ip64.io.")
os.environ.setdefault("IP64_IPDB", "/nonexistent.BIN")
os.environ.setdefault("IP64_LOG_LEVEL", "CRITICAL")
