#!/usr/bin/env python3

#
# pip install fastapi uvicorn IP2Location
#
# PowerDNS remote backend (HTTP connector) for an ip64.io-style wildcard zone.
# Maps <ip>.ip64.io -> A <ip> and exposes IP2Location geo data as LOC/TXT.
#

import os
import re
import json
import logging
import ipaddress
import threading

import IP2Location
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=os.environ.get("IP64_LOG_LEVEL", "WARNING").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ip64")

app = FastAPI()


def _fqdn(value: str) -> str:
    value = value.strip().lower()
    if not value.endswith("."):
        value += "."
    return value


# Configuration
IP64_HOST = os.environ.get("IP64_HOST", "127.0.0.1")
IP64_PORT = int(os.environ.get("IP64_PORT", "8080"))
IP64_FQDN = _fqdn(os.environ.get("IP64_FQDN", "ip64.io."))
IP64_TTL = int(os.environ.get("IP64_TTL", "300"))
IP64_IPDB = os.environ.get("IP64_IPDB", "IP2LOCATION-LITE-DB5.BIN")
IP64_IPDB_MODE = os.environ.get("IP64_IPDB_MODE", "SHARED_MEMORY")

# Wildcard answers when the subdomain carries no IP (e.g. test.ip64.io.)
IP64_ROOT_A = os.environ.get("IP64_ROOT_A", "127.0.0.1")
IP64_ROOT_AAAA = os.environ.get("IP64_ROOT_AAAA", "::1")

# SOA / NS for the apex so PowerDNS can serve the zone authoritatively.
IP64_SOA_MNAME = _fqdn(os.environ.get("IP64_SOA_MNAME", "ns1." + IP64_FQDN))
IP64_SOA_RNAME = _fqdn(os.environ.get("IP64_SOA_RNAME", "hostmaster." + IP64_FQDN))
IP64_SOA_SERIAL = os.environ.get("IP64_SOA_SERIAL", "1")
IP64_SOA_REFRESH = os.environ.get("IP64_SOA_REFRESH", "3600")
IP64_SOA_RETRY = os.environ.get("IP64_SOA_RETRY", "600")
IP64_SOA_EXPIRE = os.environ.get("IP64_SOA_EXPIRE", "604800")
IP64_SOA_MINIMUM = os.environ.get("IP64_SOA_MINIMUM", str(IP64_TTL))
# Parse IP64_NS_SERVERS: space-separated entries, each optionally carrying
# addresses as  name,ipv4,ipv6  (ipv4/ipv6 may be empty or omitted).
# Examples:
#   "ns1.ip64.io ns2.ip64.io"                              — names only (legacy)
#   "ns1.ip64.io,1.2.3.4,2001:db8::1 ns2.ip64.io,5.6.7.8"  — with addresses
_NS_ADDRS: dict[str, dict[str, str]] = {}  # fqdn -> {"A": ..., "AAAA": ...}

def _parse_ns_servers(raw: str) -> list[str]:
    names: list[str] = []
    for token in raw.split():
        token = token.strip()
        if not token:
            continue
        parts = token.split(",")
        fqdn = _fqdn(parts[0])
        names.append(fqdn)
        addrs: dict[str, str] = {}
        for part in parts[1:]:
            part = part.strip()
            if not part:
                continue
            try:
                addr = ipaddress.ip_address(part)
                if addr.version == 4:
                    addrs["A"] = str(addr)
                else:
                    addrs["AAAA"] = str(addr)
            except ValueError:
                logger.warning("Ignoring invalid address %r for NS %s", part, fqdn)
        if addrs:
            _NS_ADDRS[fqdn] = addrs
    return names

IP64_NS_SERVERS = _parse_ns_servers(
    os.environ.get("IP64_NS_SERVERS", IP64_SOA_MNAME)
)

SOA_CONTENT = (
    f"{IP64_SOA_MNAME} {IP64_SOA_RNAME} {IP64_SOA_SERIAL} "
    f"{IP64_SOA_REFRESH} {IP64_SOA_RETRY} {IP64_SOA_EXPIRE} {IP64_SOA_MINIMUM}"
)

# The IP must sit immediately before the zone FQDN, so search() picks the
# right-most address (e.g. a.b.5.6.7.8.ip64.io. -> 5.6.7.8). qname is already
# lower-cased before matching, so no IGNORECASE flag is needed.
IP_PATTERN = re.compile(
    r"(((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?))\."
    + re.escape(IP64_FQDN)
)


def _open_geo_db(path: str):
    """Open the IP2Location DB once at startup. SHARED_MEMORY (mmap) avoids a
    per-query disk seek; fall back to FILE_IO when the file can't be mapped
    (e.g. mounted read-only). Returns None if it can't be opened at all."""
    modes = [IP64_IPDB_MODE] + [m for m in ("SHARED_MEMORY", "FILE_IO") if m != IP64_IPDB_MODE]
    for mode in modes:
        try:
            db = IP2Location.IP2Location(path, mode)
            logger.info("Opened IP2Location DB %s in %s mode", path, mode)
            return db
        except Exception as exc:
            logger.warning("Could not open IP2Location DB %s in %s mode: %s", path, mode, exc)
    logger.error("Geo lookups disabled: no usable IP2Location DB at %s", path)
    return None


GEO_DB = _open_geo_db(IP64_IPDB)
# IP2Location stores per-query state on the instance, so serialize access.
# The actual read is a memory/file lookup, so the critical section is tiny.
_GEO_LOCK = threading.Lock()


def is_bogon(ip: str) -> bool:
    """True for non-global IPs (private, loopback, link-local, reserved,
    multicast, unspecified). IP2Location has no real data for these, so we
    skip geo for them and only echo the A record."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return not addr.is_global or addr.is_multicast


def lookup_geo(ip: str):
    if GEO_DB is None:
        return None
    try:
        with _GEO_LOCK:
            return GEO_DB.get_all(ip)
    except Exception:
        logger.warning("IP2Location lookup failed for %s", ip, exc_info=True)
        return None


def make_txt_content(text: str) -> str:
    """Render a string as DNS TXT rdata: <=255-char character-strings, with
    " and \\ escaped per RFC 1035."""
    chunks = [text[i:i + 255] for i in range(0, len(text), 255)] or [""]
    return " ".join(
        '"' + chunk.replace("\\", "\\\\").replace('"', '\\"') + '"' for chunk in chunks
    )


def convert_to_dns_loc(lat: float, lon: float) -> str:
    """Convert float coordinates to DNS LOC text format (RFC 1876)."""
    def to_dms(val, pos_char, neg_char):
        direction = pos_char if val >= 0 else neg_char
        val = abs(val)
        d = int(val)
        m = int((val - d) * 60)
        s = int((val - d - m / 60) * 3600)
        return f"{d} {m} {s} {direction}"

    lat_str = to_dms(lat, "N", "S")
    lon_str = to_dms(lon, "E", "W")
    return f"{lat_str} {lon_str} 0m 1m 10000m 10m"


def _record(qtype: str, qname: str, content: str, ttl: int = None):
    return {"qtype": qtype, "qname": qname, "content": content, "ttl": ttl or IP64_TTL}


def handle_lookup(req_params: dict) -> dict:
    qname = req_params.get("qname", "").lower()
    qtype = req_params.get("qtype", "ANY").upper()

    if not qname.endswith(IP64_FQDN):
        # Not our zone: empty result -> PowerDNS produces NXDOMAIN/NODATA.
        # (Returning false would signal a backend failure -> SERVFAIL.)
        return {"result": []}

    results = []

    # Apex records so the zone is authoritative.
    if qname == IP64_FQDN:
        if qtype in ("SOA", "ANY"):
            results.append(_record("SOA", qname, SOA_CONTENT))
        if qtype in ("NS", "ANY"):
            for ns in IP64_NS_SERVERS:
                results.append(_record("NS", qname, ns))


    # Glue records for configured nameservers (e.g. ns1.ip64.io -> A/AAAA).
    # PowerDNS auto-queries these when returning NS records and includes
    # the answers in the additional section of NS responses.
    if qname in _NS_ADDRS:
        addrs = _NS_ADDRS[qname]
        if qtype in ("A", "ANY") and "A" in addrs:
            results.append(_record("A", qname, addrs["A"]))
        if qtype in ("AAAA", "ANY") and "AAAA" in addrs:
            results.append(_record("AAAA", qname, addrs["AAAA"]))
        # Always return here so the name doesn't fall through to
        # the wildcard / IP-in-hostname branches.
        return {"result": results}

    match = IP_PATTERN.search(qname)
    if match:
        # An IP sits directly before the zone FQDN (e.g. anything.8.8.8.8.ip64.io.).
        resolved_ip = match.group(1)

        if qtype in ("A", "ANY"):
            results.append(_record("A", qname, resolved_ip))

        if qtype in ("LOC", "TXT", "ANY") and not is_bogon(resolved_ip):
            rec = lookup_geo(resolved_ip)
            if rec is not None:
                lat = rec.latitude
                lon = rec.longitude

                if qtype in ("LOC", "ANY") and lat is not None and lon is not None:
                    try:
                        results.append(
                            _record("LOC", qname, convert_to_dns_loc(float(lat), float(lon)))
                        )
                    except (TypeError, ValueError):
                        pass

                if qtype in ("TXT", "ANY"):
                    geo_data = {
                        "ip": resolved_ip,
                        "country_code": rec.country_short,
                        "country_name": rec.country_long,
                        "region": rec.region,
                        "city": rec.city,
                        "latitude": lat,
                        "longitude": lon,
                    }
                    # Drop empty fields and the "not available" placeholders the
                    # Lite databases use; keep 0/0.0 coordinates.
                    geo_data = {
                        k: v
                        for k, v in geo_data.items()
                        if v not in (None, "") and "not available" not in str(v).lower()
                    }
                    if geo_data:
                        results.append(
                            _record("TXT", qname, make_txt_content(json.dumps(geo_data)))
                        )

    else:
        # No IP in the name (apex or e.g. test.abc.ip64.io.) -> loopback wildcard.
        if qtype in ("A", "ANY"):
            results.append(_record("A", qname, IP64_ROOT_A))
        if qtype in ("AAAA", "ANY"):
            results.append(_record("AAAA", qname, IP64_ROOT_AAAA))

    return {"result": results}


@app.post("/dns")
async def dns_backend(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"result": False})

    method = body.get("method", "")

    if method == "initialize":
        return {"result": True}
    if method == "lookup":
        # IP2Location access is blocking; run it off the event loop.
        return await run_in_threadpool(handle_lookup, body.get("parameters", {}))

    return {"result": False}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=IP64_HOST, port=IP64_PORT, log_level="warning")
