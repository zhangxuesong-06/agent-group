import asyncio
import ipaddress
import socket

import pytest

from mvp.app import tools


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a",
        "http://",
        "http://user:pass@example.com/",
        "http://localhost/",
        "http://api.localhost/",
        "http://metadata.google.internal/",
        "http://[fe80::1%25eth0]/",
        "http://example.com:99999/",
        "http://example.com:0/",
    ],
)
def test_fetch_url_rejects_unsafe_url_shapes(url):
    with pytest.raises(ValueError):
        tools._parse_fetch_url(url)


@pytest.mark.parametrize(
    "url",
    ["http://example.com/a?b=1", "https://[2606:4700:4700::1111]:8443/x", "http://例子.测试/"],
)
def test_fetch_url_accepts_normal_public_urls(url):
    assert tools._parse_fetch_url(url).host


def test_fetch_url_normalizes_host_for_host_header_and_tls():
    parsed = tools._parse_fetch_url("http://Example.COM.:8080/x")
    assert parsed.netloc == b"example.com:8080"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.1.1",
        "100.64.0.1", "0.0.0.0", "224.0.0.1", "255.255.255.255",
        "::1", "fc00::1", "fe80::1", "::", "ff02::1",
        "::ffff:127.0.0.1", "::ffff:10.0.0.1", "2002:7f00:1::1", "2001::1",
        "64:ff9b::a00:1",
    ],
)
def test_private_and_special_addresses_are_blocked(address):
    assert tools._is_public_ip(ipaddress.ip_address(address)) is False


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_unicast_addresses_are_allowed(address):
    assert tools._is_public_ip(ipaddress.ip_address(address)) is True


def fake_addrinfo(*addresses):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80)) for address in addresses]


@pytest.mark.parametrize(
    "addresses",
    [(), ("10.0.0.1",), ("93.184.216.34", "127.0.0.1"), ("127.0.0.1", "93.184.216.34")],
)
def test_resolution_rejects_empty_or_mixed_answers(monkeypatch, addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: fake_addrinfo(*addresses))
    with pytest.raises(ValueError):
        asyncio.run(tools._resolve_public_addresses("example.com", 80))


def test_resolution_returns_all_public_answers(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs: fake_addrinfo("93.184.216.34", "8.8.8.8")
    )
    assert asyncio.run(tools._resolve_public_addresses("example.com", 80)) == ["93.184.216.34", "8.8.8.8"]


def test_resolution_failure_never_falls_back_to_hostname(monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror("dns down")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(ValueError):
        asyncio.run(tools._resolve_public_addresses("example.com", 80))


class RecordingDelegate:
    def __init__(self):
        self.calls = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.calls.append(host)
        return object()

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AssertionError("unix sockets must not be used")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


def test_backend_dials_validated_numeric_address_not_domain(monkeypatch):
    async def resolve(host, port):
        assert host == "example.com"
        return ["93.184.216.34"]

    monkeypatch.setattr(tools, "_resolve_public_addresses", resolve)
    backend = tools._PublicNetworkBackend()
    delegate = RecordingDelegate()
    backend._delegate = delegate
    asyncio.run(backend.connect_tcp("example.com", 443, timeout=5))
    assert delegate.calls == ["93.184.216.34"]


def test_backend_refuses_connection_when_resolution_is_blocked(monkeypatch):
    async def resolve(host, port):
        raise ValueError("禁止访问内网地址")

    monkeypatch.setattr(tools, "_resolve_public_addresses", resolve)
    backend = tools._PublicNetworkBackend()
    delegate = RecordingDelegate()
    backend._delegate = delegate
    with pytest.raises(ValueError):
        asyncio.run(backend.connect_tcp("evil.example", 80, timeout=5))
    assert delegate.calls == []


class FakeResponse:
    def __init__(self, chunks, headers=None):
        self.chunks = chunks
        self.headers = headers or []
        self.closed = False

    async def aiter_stream(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def test_body_limit_accepts_exact_size_and_rejects_one_byte_over():
    limit = 10
    body = asyncio.run(tools._read_limited_body(FakeResponse([b"a" * 6, b"b" * 4]), limit))
    assert body == b"a" * 6 + b"b" * 4
    with pytest.raises(ValueError):
        asyncio.run(tools._read_limited_body(FakeResponse([b"a" * 6, b"b" * 5]), limit))


def test_body_limit_rejects_declared_or_encoded_oversize():
    with pytest.raises(ValueError):
        asyncio.run(
            tools._read_limited_body(
                FakeResponse([b"ok"], [(b"content-length", b"999999")]), 100
            )
        )
    with pytest.raises(ValueError):
        asyncio.run(
            tools._read_limited_body(
                FakeResponse([b"ok"], [(b"content-encoding", b"gzip")]), 100
            )
        )


def test_fetch_url_fails_closed_before_connecting_when_dns_is_private(monkeypatch):
    async def resolve(host, port):
        raise ValueError("禁止访问内网地址")

    monkeypatch.setattr(tools, "_resolve_public_addresses", resolve)
    with pytest.raises(ValueError):
        asyncio.run(tools._fetch_url({"url": "http://internal.example/"}, {}))
