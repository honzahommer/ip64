import json

import pytest
from fastapi.testclient import TestClient

import ip64


@pytest.fixture()
def client():
    return TestClient(ip64.app)


def lookup(client, qname, qtype="ANY"):
    resp = client.post(
        "/dns",
        json={"method": "lookup", "parameters": {"qname": qname, "qtype": qtype}},
    )
    assert resp.status_code == 200
    return resp.json()


class FakeRecord:
    """Mimics an IP2Location record. lat/lon default to the equator/prime
    meridian (0.0) to verify those are not dropped as falsy."""

    def __init__(self, **kw):
        self.country_short = kw.get("country_short", "US")
        self.country_long = kw.get("country_long", "United States of America")
        self.region = kw.get("region", "California")
        self.city = kw.get("city", "Mountain View")
        self.latitude = kw.get("latitude", 0.0)
        self.longitude = kw.get("longitude", -122.0838)


@pytest.fixture()
def fake_geo(monkeypatch):
    """Install a fake GEO_DB that returns the given record for any IP."""

    def _install(record=None):
        rec = record if record is not None else FakeRecord()

        class FakeDB:
            def get_all(self, ip):
                return rec

        monkeypatch.setattr(ip64, "GEO_DB", FakeDB())
        return rec

    return _install


# --- /dns protocol ---------------------------------------------------------

def test_initialize(client):
    assert client.post("/dns", json={"method": "initialize"}).json() == {"result": True}


def test_unknown_method(client):
    assert client.post("/dns", json={"method": "bogus"}).json() == {"result": False}


def test_bad_json_returns_400(client):
    resp = client.post("/dns", content=b"not json")
    assert resp.status_code == 400
    assert resp.json() == {"result": False}


# --- A / wildcard ----------------------------------------------------------

def test_a_rightmost_ip_wins(client):
    res = lookup(client, "a.b.5.6.7.8.ip64.io.", "A")
    assert res["result"] == [
        {"qtype": "A", "qname": "a.b.5.6.7.8.ip64.io.", "content": "5.6.7.8", "ttl": 300}
    ]


def test_a_with_label_prefix(client):
    res = lookup(client, "prod.9.9.9.9.ip64.io.", "A")
    assert res["result"][0]["content"] == "9.9.9.9"


def test_aaaa_on_ip_name_is_nodata(client):
    # Name exists (IP branch) but has no AAAA -> empty result, not false.
    assert lookup(client, "8.8.8.8.ip64.io.", "AAAA") == {"result": []}


def test_out_of_zone_is_empty(client):
    assert lookup(client, "www.example.com.", "A") == {"result": []}


def test_wildcard_non_ip_subdomain(client):
    res = lookup(client, "test.abc.ip64.io.", "ANY")
    by_type = {r["qtype"]: r["content"] for r in res["result"]}
    assert by_type == {"A": "127.0.0.1", "AAAA": "::1"}


# --- apex SOA / NS ---------------------------------------------------------

def test_apex_soa(client):
    res = lookup(client, "ip64.io.", "SOA")
    assert res["result"] == [
        {
            "qtype": "SOA",
            "qname": "ip64.io.",
            "content": "ns1.ip64.io. hostmaster.ip64.io. 1 3600 600 604800 300",
            "ttl": 300,
        }
    ]


def test_apex_ns(client):
    res = lookup(client, "ip64.io.", "NS")
    assert [r["qtype"] for r in res["result"]] == ["NS"]
    assert res["result"][0]["content"] == "ns1.ip64.io."


def test_apex_any_has_soa_ns_a_aaaa(client):
    res = lookup(client, "ip64.io.", "ANY")
    assert sorted({r["qtype"] for r in res["result"]}) == ["A", "AAAA", "NS", "SOA"]


# --- multi-NS parsing & glue records ---------------------------------------

def test_parse_ns_servers_legacy_names_only():
    ip64._NS_ADDRS.clear()
    names = ip64._parse_ns_servers("ns1.ip64.io ns2.ip64.io")
    assert names == ["ns1.ip64.io.", "ns2.ip64.io."]
    assert ip64._NS_ADDRS == {}


def test_parse_ns_servers_with_addresses():
    ip64._NS_ADDRS.clear()
    names = ip64._parse_ns_servers(
        "ns1.ip64.io,1.2.3.4,2001:db8::1 ns2.ip64.io,5.6.7.8"
    )
    assert names == ["ns1.ip64.io.", "ns2.ip64.io."]
    assert ip64._NS_ADDRS["ns1.ip64.io."] == {"A": "1.2.3.4", "AAAA": "2001:db8::1"}
    assert ip64._NS_ADDRS["ns2.ip64.io."] == {"A": "5.6.7.8"}


def test_parse_ns_servers_ipv6_only():
    ip64._NS_ADDRS.clear()
    names = ip64._parse_ns_servers("ns1.ip64.io,,2001:db8::1")
    assert names == ["ns1.ip64.io."]
    assert ip64._NS_ADDRS["ns1.ip64.io."] == {"AAAA": "2001:db8::1"}


def test_ns_glue_a_record(client, monkeypatch):
    monkeypatch.setitem(ip64._NS_ADDRS, "ns1.ip64.io.", {"A": "1.2.3.4", "AAAA": "2001:db8::1"})
    res = lookup(client, "ns1.ip64.io.", "A")
    assert res["result"] == [
        {"qtype": "A", "qname": "ns1.ip64.io.", "content": "1.2.3.4", "ttl": 300}
    ]


def test_ns_glue_aaaa_record(client, monkeypatch):
    monkeypatch.setitem(ip64._NS_ADDRS, "ns1.ip64.io.", {"A": "1.2.3.4", "AAAA": "2001:db8::1"})
    res = lookup(client, "ns1.ip64.io.", "AAAA")
    assert res["result"] == [
        {"qtype": "AAAA", "qname": "ns1.ip64.io.", "content": "2001:db8::1", "ttl": 300}
    ]


def test_ns_glue_any_returns_both(client, monkeypatch):
    monkeypatch.setitem(ip64._NS_ADDRS, "ns1.ip64.io.", {"A": "1.2.3.4", "AAAA": "2001:db8::1"})
    res = lookup(client, "ns1.ip64.io.", "ANY")
    by_type = {r["qtype"]: r["content"] for r in res["result"]}
    assert by_type == {"A": "1.2.3.4", "AAAA": "2001:db8::1"}


def test_ns_glue_unmatched_qtype_returns_empty(client, monkeypatch):
    """NS name exists but requested type (TXT) is not configured -> NODATA."""
    monkeypatch.setitem(ip64._NS_ADDRS, "ns1.ip64.io.", {"A": "1.2.3.4"})
    res = lookup(client, "ns1.ip64.io.", "TXT")
    assert res == {"result": []}


def test_ns_glue_does_not_fall_through_to_wildcard(client, monkeypatch):
    """NS name must NOT return the wildcard 127.0.0.1."""
    monkeypatch.setitem(ip64._NS_ADDRS, "ns1.ip64.io.", {"A": "1.2.3.4"})
    res = lookup(client, "ns1.ip64.io.", "A")
    assert res["result"][0]["content"] == "1.2.3.4"


# --- is_bogon --------------------------------------------------------------

@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",
        "127.0.0.1",
        "192.168.1.1",
        "169.254.0.1",
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        "255.255.255.255",
        "not-an-ip",
    ],
)
def test_is_bogon_true(ip):
    assert ip64.is_bogon(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "9.9.9.9"])
def test_is_bogon_false(ip):
    assert ip64.is_bogon(ip) is False


# --- geo (with fake DB) ----------------------------------------------------

def test_public_ip_returns_a_loc_txt(client, fake_geo):
    fake_geo()
    res = lookup(client, "8.8.8.8.ip64.io.", "ANY")
    by_type = {r["qtype"]: r["content"] for r in res["result"]}
    assert by_type["A"] == "8.8.8.8"
    # latitude 0.0 must be preserved in the LOC output.
    assert by_type["LOC"].startswith("0 0 0 N 122 5 1 W")
    assert "TXT" in by_type


def test_bogon_skips_geo_even_with_db(client, fake_geo):
    fake_geo()  # DB would return data, but bogons must skip geo
    res = lookup(client, "10.0.0.1.ip64.io.", "ANY")
    assert [r["qtype"] for r in res["result"]] == ["A"]
    assert res["result"][0]["content"] == "10.0.0.1"
    assert lookup(client, "127.0.0.1.ip64.io.", "TXT") == {"result": []}
    assert lookup(client, "192.168.5.6.ip64.io.", "LOC") == {"result": []}


def test_loc_only_query(client, fake_geo):
    fake_geo()
    res = lookup(client, "8.8.8.8.ip64.io.", "LOC")
    assert [r["qtype"] for r in res["result"]] == ["LOC"]


def test_txt_is_valid_and_roundtrips(client, fake_geo):
    fake_geo(FakeRecord(city='Mountain "View"', latitude=0.0))
    res = lookup(client, "8.8.8.8.ip64.io.", "TXT")
    txt = res["result"][0]["content"]
    # One quoted character-string, inner quotes escaped.
    assert txt.startswith('"') and txt.endswith('"')
    assert '\\"' in txt
    inner = txt[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    parsed = json.loads(inner)
    assert parsed["ip"] == "8.8.8.8"
    assert parsed["city"] == 'Mountain "View"'
    assert parsed["latitude"] == 0.0


def test_not_available_fields_dropped(client, fake_geo):
    fake_geo(FakeRecord(region="This region is not available", city=""))
    res = lookup(client, "8.8.8.8.ip64.io.", "TXT")
    inner = res["result"][0]["content"][1:-1].replace('\\"', '"').replace("\\\\", "\\")
    parsed = json.loads(inner)
    assert "region" not in parsed
    assert "city" not in parsed


# --- pure helpers ----------------------------------------------------------

def test_make_txt_content_escaping():
    assert ip64.make_txt_content('a"b\\c') == '"a\\"b\\\\c"'


def test_make_txt_content_chunks_long_strings():
    out = ip64.make_txt_content("x" * 300)
    chunks = out.split(" ")
    assert len(chunks) == 2
    assert len(chunks[0].strip('"')) == 255
    assert len(chunks[1].strip('"')) == 45


def test_make_txt_content_empty():
    assert ip64.make_txt_content("") == '""'


def test_convert_to_dns_loc():
    assert (
        ip64.convert_to_dns_loc(37.751, -97.822)
        == "37 45 3 N 97 49 19 W 0m 1m 10000m 10m"
    )


def test_convert_to_dns_loc_zero_equator():
    assert ip64.convert_to_dns_loc(0.0, 0.0) == "0 0 0 N 0 0 0 E 0m 1m 10000m 10m"
