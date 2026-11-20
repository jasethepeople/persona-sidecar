import base64
import http.client
import ipaddress
import os
import socket
import ssl
from urllib.parse import unquote, urlsplit

MAX_RESPONSE_BYTES = 1_048_576


def _recv_exact(sock, count):
    chunks = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise OSError("proxy closed the connection during SOCKS negotiation")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _socks5_connect(proxy_url, target_host, target_port, timeout):
    proxy = urlsplit(proxy_url)
    if proxy.scheme not in {"socks5", "socks5h"} or not proxy.hostname:
        raise ValueError("SOCKS egress requires socks5:// or socks5h:// proxy_url")
    proxy_port = proxy.port or 1080
    sock = socket.create_connection((proxy.hostname, proxy_port), timeout=timeout)
    sock.settimeout(timeout)
    username = unquote(proxy.username or "").encode("utf-8")
    password = unquote(proxy.password or "").encode("utf-8")
    methods = b"\x00\x02" if proxy.username is not None else b"\x00"
    sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
    version, method = _recv_exact(sock, 2)
    if version != 5 or method == 255:
        sock.close()
        raise OSError("SOCKS5 proxy rejected authentication methods")
    if method == 2:
        if not username or len(username) > 255 or len(password) > 255:
            sock.close()
            raise ValueError("SOCKS5 username/password must each be 1..255 bytes")
        sock.sendall(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
        auth_version, auth_status = _recv_exact(sock, 2)
        if auth_version != 1 or auth_status != 0:
            sock.close()
            raise OSError("SOCKS5 username/password authentication failed")
    elif method != 0:
        sock.close()
        raise OSError("SOCKS5 selected an unsupported authentication method")
    if proxy.scheme == "socks5h":
        encoded = target_host.encode("idna")
        if len(encoded) > 255:
            sock.close()
            raise ValueError("target hostname is too long for SOCKS5")
        address = b"\x03" + bytes([len(encoded)]) + encoded
    else:
        resolved = socket.getaddrinfo(target_host, target_port, type=socket.SOCK_STREAM)[0][4][0]
        parsed = ipaddress.ip_address(resolved)
        address = (b"\x01" + parsed.packed) if parsed.version == 4 else (b"\x04" + parsed.packed)
    sock.sendall(b"\x05\x01\x00" + address + int(target_port).to_bytes(2, "big"))
    version, reply, _, atyp = _recv_exact(sock, 4)
    if version != 5 or reply != 0:
        sock.close()
        raise OSError(f"SOCKS5 connect failed with reply {reply}")
    if atyp == 1:
        _recv_exact(sock, 4)
    elif atyp == 4:
        _recv_exact(sock, 16)
    elif atyp == 3:
        length = _recv_exact(sock, 1)[0]
        _recv_exact(sock, length)
    else:
        sock.close()
        raise OSError("SOCKS5 returned an unknown address type")
    _recv_exact(sock, 2)
    return sock


class _SocksHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, proxy_url, timeout):
        super().__init__(host, port, timeout=timeout)
        self.proxy_url = proxy_url

    def connect(self):
        self.sock = _socks5_connect(self.proxy_url, self.host, self.port, self.timeout)


class _SocksHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, proxy_url, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self.proxy_url = proxy_url

    def connect(self):
        raw = _socks5_connect(self.proxy_url, self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class _ProxyHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, proxy_host, proxy_port, proxy_headers, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.proxy_headers = proxy_headers
        self.set_tunnel(host, port, headers=proxy_headers)

    def connect(self):
        raw = socket.create_connection((self.proxy_host, self.proxy_port), timeout=self.timeout)
        self.sock = raw
        self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class Egress:
    def __init__(self, settings):
        self.settings = settings
        self.mode = settings["mode"]
        proxy_env = settings.get("proxy_url_env", "")
        self.proxy_url = os.environ.get(proxy_env, "") if proxy_env else ""
        self.proxy_url = self.proxy_url or settings.get("proxy_url", "")
        self.timeout = float(settings.get("timeout_seconds", 5))

    def request(self, url, method="GET", body=None, headers=None, timeout=None):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("egress supports absolute HTTP(S) URLs only")
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        timeout = float(timeout if timeout is not None else self.timeout)
        headers = dict(headers or {})
        context = ssl.create_default_context()
        conn = None
        request_target = path
        if self.mode == "direct":
            conn = (http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
                    if parsed.scheme == "https" else http.client.HTTPConnection(host, port, timeout=timeout))
        elif self.mode == "socks5":
            if not self.proxy_url:
                raise ValueError("egress.proxy_url is required for SOCKS5 mode")
            conn = (_SocksHTTPSConnection(host, port, self.proxy_url, timeout, context)
                    if parsed.scheme == "https" else _SocksHTTPConnection(host, port, self.proxy_url, timeout))
        elif self.mode == "http":
            proxy = urlsplit(self.proxy_url)
            if proxy.scheme != "http" or not proxy.hostname:
                raise ValueError("HTTP proxy mode requires an http:// proxy_url")
            proxy_port = proxy.port or 8080
            proxy_headers = {}
            if proxy.username is not None:
                credentials = f"{unquote(proxy.username)}:{unquote(proxy.password or '')}".encode("utf-8")
                proxy_headers["Proxy-Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
            if parsed.scheme == "https":
                conn = _ProxyHTTPSConnection(host, port, proxy.hostname, proxy_port, proxy_headers, timeout, context)
            else:
                conn = http.client.HTTPConnection(proxy.hostname, proxy_port, timeout=timeout)
                request_target = url
                headers.update(proxy_headers)
        else:
            raise ValueError("unsupported egress mode")
        try:
            conn.request(method, request_target, body=body, headers=headers)
            response = conn.getresponse()
            content = response.read(MAX_RESPONSE_BYTES + 1)
            if len(content) > MAX_RESPONSE_BYTES:
                raise OSError("egress response exceeded one megabyte")
            return response.status, content, dict(response.getheaders())
        finally:
            if conn is not None:
                conn.close()

    def observe_ip(self):
        url = self.settings.get("ip_observation_url", "")
        if not url:
            return None, "ip_observation_url_not_configured"
        try:
            status, body, _ = self.request(url, method="GET", headers={"Accept": "text/plain"})
            if not 200 <= status < 300:
                return None, f"ip_observer_http_{status}"
            value = body.decode("utf-8").strip()
            return str(ipaddress.ip_address(value)), None
        except Exception as exc:
            return None, type(exc).__name__
