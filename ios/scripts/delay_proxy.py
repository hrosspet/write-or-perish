"""Reverse proxy in front of the local Docker backend (:5010) that adds
production-like latency to two requests, and logs every drafts/textmode
request with timestamps (received / forwarded / answered). Used by
TextModeDraftUITests (#425): with no latency, an autosave can never be in
flight when Send deletes the draft, so the race cannot show locally.

  python3 ios/scripts/delay_proxy.py [port] [node_delay]   # default 5099, 0

  POST /api/textmode/start  held 1.0 s before forwarding
  POST /api/drafts/         held 0.5 s before forwarding
  GET  /api/nodes/<id>      held `node_delay` s (NodeOpeningUITests: the
                            spinner shows while a node loads)
"""
import http.client
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = ("localhost", 5010)
DELAYS = {("POST", "/api/textmode/start"): 1.0, ("POST", "/api/drafts/"): 0.5}
NODE_GET = re.compile(r"^/api/nodes/\d+$")
NODE_DELAY = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
T0 = time.monotonic()
LOCK = threading.Lock()


def log(msg):
    with LOCK:
        print(f"{time.monotonic() - T0:8.3f}  {msg}", flush=True)


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _handle(self):
        path_only = self.path.split("?")[0]
        watched = path_only in ("/api/drafts/", "/api/textmode/start")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        delay = DELAYS.get((self.command, path_only), 0)
        if self.command == "GET" and NODE_GET.match(path_only):
            delay = NODE_DELAY
        if watched:
            log(f"recv      {self.command} {self.path}")
        if delay:
            time.sleep(delay)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")}
        conn = http.client.HTTPConnection(*UPSTREAM, timeout=600)
        conn.request(self.command, self.path, body=body, headers=headers)
        if watched:
            log(f"forwarded {self.command} {self.path}")
        resp = conn.getresponse()
        streaming = "text/event-stream" in (resp.getheader("Content-Type") or "")
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in ("transfer-encoding", "connection", "content-length"):
                continue
            self.send_header(k, v)
        if streaming:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            data = resp.read()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        if watched:
            log(f"answered  {self.command} {self.path} -> {resp.status}")
        conn.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5099
    ThreadingHTTPServer(("127.0.0.1", port), Proxy).serve_forever()
