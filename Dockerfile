FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ip64.py .

# Listen on all interfaces inside the container; the port is not published to
# the host in docker-compose (only PowerDNS is reachable from outside).
ENV IP64_HOST=0.0.0.0 \
    IP64_PORT=8080 \
    IP64_IPDB=./data/IP2LOCATION-LITE-DB5.BIN

EXPOSE 8080

# Run as an unprivileged user.
RUN useradd --system --no-create-home --uid 53035 ip64
USER ip64

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8080/dns', data=b'{\"method\":\"initialize\"}', headers={'Content-Type':'application/json'}), timeout=2)" || exit 1

CMD ["python", "ip64.py"]
