import http.client
import select
import socket
import socketserver
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from persona_sidecar.egress import Egress


class _TargetHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"proxy-route-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class _HTTPProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
        connection.request("GET", parsed.path or "/", headers={"Connection": "close"})
        response = connection.getresponse()
        body = response.read()
        self.send_response(response.status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        connection.close()

    def log_message(self, *_args):
        pass


class _SOCKSHandler(socketserver.StreamRequestHandler):
    def handle(self):
        version, count = self.rfile.read(2)
        methods = self.rfile.read(count)
        if version != 5 or 0 not in methods:
            self.wfile.write(b"\x05\xff")
            return
        self.wfile.write(b"\x05\x00")
        version, command, _, address_type = self.rfile.read(4)
        if version != 5 or command != 1:
            return
        if address_type == 1:
            host = socket.inet_ntoa(self.rfile.read(4))
        elif address_type == 3:
            length = self.rfile.read(1)[0]
            host = self.rfile.read(length).decode("idna")
        elif address_type == 4:
            host = socket.inet_ntop(socket.AF_INET6, self.rfile.read(16))
        else:
            return
        port = int.from_bytes(self.rfile.read(2), "big")
        upstream = socket.create_connection((host, port), timeout=2)
        self.wfile.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        self.wfile.flush()
        peers = (self.connection, upstream)
        try:
            while True:
                readable, _, _ = select.select(peers, [], [], 2)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    target = upstream if source is self.connection else self.connection
                    target.sendall(data)
        finally:
            upstream.close()


class EgressProxyTests(unittest.TestCase):
    def setUp(self):
        self.target = ThreadingHTTPServer(("127.0.0.1", 0), _TargetHandler)
        self.target_thread = threading.Thread(target=self.target.serve_forever, daemon=True)
        self.target_thread.start()
        self.target_url = f"http://127.0.0.1:{self.target.server_address[1]}/check"
        self.addCleanup(self._stop_target)

    def _stop_target(self):
        self.target.shutdown()
        self.target.server_close()
        self.target_thread.join(timeout=2)

    def _assert_route(self, egress):
        status, body, _headers = egress.request(self.target_url)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"proxy-route-ok")

    def test_http_proxy_route(self):
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), _HTTPProxyHandler)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            egress = Egress({
                "mode": "http",
                "proxy_url": f"http://127.0.0.1:{proxy.server_address[1]}",
                "timeout_seconds": 2,
            })
            self._assert_route(egress)
        finally:
            proxy.shutdown()
            proxy.server_close()
            thread.join(timeout=2)

    def test_socks5_proxy_route(self):
        class ProxyServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        proxy = ProxyServer(("127.0.0.1", 0), _SOCKSHandler)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            egress = Egress({
                "mode": "socks5",
                "proxy_url": f"socks5://127.0.0.1:{proxy.server_address[1]}",
                "timeout_seconds": 2,
            })
            self._assert_route(egress)
        finally:
            proxy.shutdown()
            proxy.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
