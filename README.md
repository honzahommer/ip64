# ip64

An [xip.io](https://web.archive.org/web/20191115173723/http://xip.io/)-style wildcard DNS service: any hostname with an embedded IPv4 address resolves to that address (e.g. `app.10.0.0.1.ip64.io` → `10.0.0.1`). For globally-routable IPs it also exposes IP2Location geo data as `LOC` and `TXT` records.

It runs as a [PowerDNS](https://www.powerdns.com/) Authoritative server (`pdns`) talking to a small FastAPI [remote backend](https://docs.powerdns.com/authoritative/backends/remote.html) (`ip64.py`) over the HTTP connector. A [dnsdist](https://dnsdist.org/) front-end adds DNS-over-HTTPS (DoH).

```
dig  ─Do53─────────────────> pdns (authoritative, :53) ─HTTP /dns─> ip64 (FastAPI remote backend)
DoH client ─HTTPS─> dnsdist ─Do53─┘                                    └─> IP2Location BIN (optional)
             (:443 /dns-query)
```

## Quick start

```bash
docker compose up -d --build
dig @127.0.0.1 1.2.3.4.ip64.io +short      # -> 1.2.3.4
dig @127.0.0.1 ip64.io SOA +short
```

`pdns` publishes `53/udp` and `53/tcp`. If port 53 is taken on the host (e.g. by `systemd-resolved`), pick another:

```bash
IP64_DNS_PORT=5354 docker compose up -d --build
dig @127.0.0.1 -p 5354 8.8.8.8.ip64.io +short
```

The `ip64` backend is internal only (not published to the host).

## DNS-over-HTTPS (DoH)

`dnsdist` serves DoH at `https://<host>/dns-query` (port 443) and forwards to `pdns`. On first `up` a `certgen` service writes a **self-signed** cert to `./dnsdist/certs/` (`cert.pem` + `key.pem`) if none exists — drop your own there to use a real certificate. Set the cert CN via `IP64_DOH_CN`.

```bash
IP64_DOH_PORT=8443 docker compose up -d --build
# query over DoH, trusting the generated self-signed cert
kdig +https=/dns-query @127.0.0.1 -p 8443 \
  +tls-ca=./dnsdist/certs/cert.pem +tls-hostname=ip64.io \
  1.2.3.4.ip64.io A +short            # -> 1.2.3.4
```

With a real (CA-signed) cert and the domain delegated to this host, any DoH client (browsers, `curl --doh-url`, etc.) can use `https://<your-domain>/dns-query`. `dnsdist`'s ACL is set to allow all clients since this is a public service.

## Geo (LOC/TXT) records

`LOC`/`TXT` need an IP2Location BIN database (e.g. the free `IP2LOCATION-LITE-DB5.BIN`). Drop it into `./data/`:

```
data/IP2LOCATION-LITE-DB5.BIN
```

Without it the service still answers `A`/`AAAA`/`SOA`/`NS`; geo is simply disabled. Geo is also skipped for bogon (non-globally-routable) IPs, which have no useful geo data.

## Configuration

The zone name and records are configured via environment variables (see `docker-compose.yml`):

| Variable | Default | Description |
| --- | --- | --- |
| `IP64_FQDN` | `ip64.io.` | Zone the backend is authoritative for |
| `IP64_DNS_PORT` | `53` | Host port mapped to PowerDNS (Do53) |
| `IP64_DOH_PORT` | `443` | Host port mapped to dnsdist (DoH) |
| `IP64_DOH_CN` | `ip64.io` | CN used for the auto-generated self-signed DoH cert |
| `IP64_TTL` | `300` | TTL for generated records |
| `IP64_IPDB` | `/data/IP2LOCATION-LITE-DB5.BIN` | Path to the IP2Location BIN |
| `IP64_ROOT_A` / `IP64_ROOT_AAAA` | `127.0.0.1` / `::1` | Answer for apex |
| `IP64_WILDCARD_A` / `IP64_WILDCARD_AAAA` | `127.0.0.1` / `::1` | Wildcard answer for names without an embedded IP |
| `IP64_SOA_MNAME` / `IP64_SOA_RNAME` | `ns1.<fqdn>` / `hostmaster.<fqdn>` | SOA primary / contact |
| `IP64_NS_SERVERS` | `IP64_SOA_MNAME` | Space-separated `NS` entries: `name[,ipv4][,ipv6]` |
| `IP64_LOG_LEVEL` | `WARNING` | Backend log level |

### Multiple nameservers with glue

Each `IP64_NS_SERVERS` entry can carry optional IPv4/IPv6 addresses after the name, separated by commas. PowerDNS will automatically include these as **glue records** (additional section) in NS responses.

```bash
IP64_NS_SERVERS="ns1.ip64.io,1.2.3.4,2001:db8::1 ns2.ip64.io,5.6.7.8,2001:db8::2"
```

When a client queries `ip64.io. NS`, the response will contain:

- **Answer section**: `NS ns1.ip64.io.` + `NS ns2.ip64.io.`
- **Additional section**: `ns1.ip64.io. A 1.2.3.4`, `ns1.ip64.io. AAAA 2001:db8::1`, `ns2.ip64.io. A 5.6.7.8`, `ns2.ip64.io. AAAA 2001:db8::2`

Plain names without addresses still work (backward compatible):

```bash
IP64_NS_SERVERS="ns1.ip64.io ns2.ip64.io"
```

To serve a real domain, set `IP64_FQDN` (e.g. `IP64_FQDN=ip64.example.com.`) and delegate that domain's `NS` to this server.
