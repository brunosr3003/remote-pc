#!/usr/bin/env python3
"""Browser access to a Linux X11 desktop.

Serves noVNC (static files) and bridges WebSocket -> x11vnc on the display.
Also serves /pad: a phone trackpad that types and moves the mouse on the
display through XTEST, without going through VNC (see xtest.py).
Standard library only, on purpose: it has to run on machines where you
can't install packages without root, so any external dependency would
become a chore on the next reinstall.

Listens on 127.0.0.1 by default. Exposing it to the outside world (an SSH
reverse tunnel to a VPS with TLS, Tailscale, etc.) is up to you.
"""

import base64
import hashlib
import http.server
import json
import os
import select
import socket
import struct
import threading
import time
import urllib.parse

import xtest

HERE = os.path.dirname(os.path.abspath(__file__))
NOVNC = os.path.join(HERE, "novnc")
PAD = os.path.join(HERE, "pad")

BIND = os.environ.get("REMOTE_PC_BIND", "127.0.0.1")
PORT = int(os.environ.get("REMOTE_PC_PORT", "8786"))
VNC_HOST = os.environ.get("REMOTE_PC_VNC_HOST", "127.0.0.1")
VNC_PORT = int(os.environ.get("REMOTE_PC_VNC_PORT", "5900"))
DISPLAY = os.environ.get("REMOTE_PC_DISPLAY", ":0")
EXTRA_BINDS = [s.strip() for s in
               os.environ.get("REMOTE_PC_EXTRA_BINDS", "").split(",")]
HOSTNAME = socket.gethostname()

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _read_secret(fname):
    try:
        with open(os.path.join(HERE, "etc", fname)) as f:
            return f.read().strip()
    except OSError:
        return ""


TOKEN = os.environ.get("REMOTE_PC_TOKEN") or _read_secret("token.txt")
VNCPASS = _read_secret("vncpass.txt")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".map": "application/json",
}


# ---------------------------------------------------------------- websocket


def ws_frame(payload, opcode=0x2):
    """Builds a server-to-client frame. The server never masks."""
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


class FrameReader:
    """Splits the client stream into frames.

    The browser sends masked frames and may split one frame across several
    TCP packets, so this buffers and only yields complete frames.
    """

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data

    def __iter__(self):
        while True:
            b = self.buf
            if len(b) < 2:
                return
            fin_op = b[0]
            opcode = fin_op & 0x0F
            masked = b[1] & 0x80
            ln = b[1] & 0x7F
            i = 2
            if ln == 126:
                if len(b) < i + 2:
                    return
                ln = struct.unpack(">H", b[i:i + 2])[0]
                i += 2
            elif ln == 127:
                if len(b) < i + 8:
                    return
                ln = struct.unpack(">Q", b[i:i + 8])[0]
                i += 8
            mask = b""
            if masked:
                if len(b) < i + 4:
                    return
                mask = bytes(b[i:i + 4])
                i += 4
            if len(b) < i + ln:
                return
            payload = bytes(b[i:i + ln])
            if masked:
                payload = bytes(c ^ mask[j % 4] for j, c in enumerate(payload))
            del self.buf[:i + ln]
            yield opcode, payload


# ---------------------------------------------------------------- pad utils


def _num(v, default=0, cap=4000):
    """A number coming from the network could be anything. Clamp it before it
    becomes an event, so a glitchy finger (or a hostile client) can't throw
    the cursor to infinity."""
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return default
    return max(-cap, min(n, cap))


def _mods(v):
    if not isinstance(v, list):
        return ()
    return tuple(m for m in v if m in xtest.MOD_KEYSYM)


# ------------------------------------------------------------------ handler


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "remote-pc"

    def log_message(self, fmt, *args):
        pass  # the journal already timestamps everything; access logs are noise

    # -- helpers

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self, q):
        if not TOKEN:
            return True
        if q.get("t", [""])[0] == TOKEN:
            return True
        cookie = self.headers.get("Cookie", "")
        return f"pc_token={TOKEN}" in cookie

    # -- routes

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path, q = url.path, urllib.parse.parse_qs(url.query)

        if path == "/ws":
            return self.do_ws(q)

        if not self._authorized(q):
            return self._send(403, "invalid token - open with ?t=YOUR_TOKEN")

        extra = {}
        if TOKEN and q.get("t", [""])[0] == TOKEN:
            extra["Set-Cookie"] = (
                f"pc_token={TOKEN}; Path=/; SameSite=Lax; "
                "HttpOnly; Secure; Max-Age=31536000")

        if path == "/":
            # noVNC reads everything from the query string. Tell it to connect
            # on its own so the link opens straight into the desktop, with no
            # "click to connect" screen.
            params = {
                "autoconnect": "true",
                "path": f"ws?t={TOKEN}" if TOKEN else "ws",
                "resize": "scale",
                "quality": "6",
                "compression": "3",
                "reconnect": "true",
                "reconnect_delay": "2000",
            }
            if VNCPASS:
                params["password"] = VNCPASS
            dest = "/vnc.html?" + urllib.parse.urlencode(params)
            return self._send(302, b"", extra={**extra, "Location": dest})

        if path == "/healthz":
            return self._send(200, "ok\n", extra=extra)

        if path in ("/pad", "/pad/", "/pad/index.html"):
            return self.serve_pad(extra)

        return self.serve_static(path, extra)

    def serve_pad(self, extra=None):
        """The trackpad. The token is stamped into the HTML because the cookie
        is Secure: over plain http (e.g. direct Tailscale access, no TLS) it
        doesn't exist, and without the token the WebSocket would get a 403."""
        try:
            with open(os.path.join(PAD, "index.html"), encoding="utf-8") as f:
                html = f.read()
        except OSError:
            return self._send(404, "pad not installed")
        html = html.replace("__TOKEN__", TOKEN or "")
        return self._send(200, html, "text/html; charset=utf-8", extra)

    def serve_static(self, path, extra=None):
        rel = path.lstrip("/") or "vnc.html"
        full = os.path.abspath(os.path.join(NOVNC, rel))
        # Without this, a ../../ in the URL reads any file in the home dir.
        if not full.startswith(os.path.abspath(NOVNC) + os.sep):
            return self._send(403, "outside the web root")
        if not os.path.isfile(full):
            return self._send(404, "not found")
        ctype = MIME.get(os.path.splitext(full)[1].lower(),
                         "application/octet-stream")
        with open(full, "rb") as f:
            return self._send(200, f.read(), ctype, extra)

    # -- websocket -> vnc bridge

    def do_ws(self, q):
        if not self._authorized(q):
            return self._send(403, "invalid token")

        if not self.headers.get("Sec-WebSocket-Key") or \
                "websocket" not in self.headers.get("Upgrade", "").lower():
            return self._send(400, "not a websocket handshake")

        # Same endpoint for both modes, so a reverse proxy only needs a
        # WebSocket upgrade rule for /ws.
        if q.get("mode", [""])[0] == "pad":
            return self.do_pad_ws()

        try:
            vnc = socket.create_connection((VNC_HOST, VNC_PORT), timeout=5)
        except OSError as e:
            return self._send(503, f"x11vnc {VNC_HOST}:{VNC_PORT} refused: {e}")

        self.ws_accept()
        self.close_connection = True
        try:
            self.relay(self.connection, vnc)
        finally:
            for s in (vnc,):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()

    def ws_accept(self):
        key = self.headers.get("Sec-WebSocket-Key")
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        lines = [
            "HTTP/1.1 101 Switching Protocols",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Accept: {accept}",
        ]
        # Older noVNC negotiates the 'binary' subprotocol; confirm it if asked.
        want = self.headers.get("Sec-WebSocket-Protocol", "")
        if "binary" in want:
            lines.append("Sec-WebSocket-Protocol: binary")
        self.wfile.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        self.wfile.flush()

    # ------------------------------------------------------ trackpad (XTEST)

    def do_pad_ws(self):
        try:
            inj = xtest.Injector(DISPLAY)
        except xtest.XTestError as e:
            return self._send(503, f"XTEST unavailable: {e}")

        self.ws_accept()
        self.close_connection = True
        sock = self.connection
        reader = FrameReader()

        def reply(obj):
            sock.sendall(ws_frame(json.dumps(obj).encode(), 0x1))

        try:
            reply({"t": "hello", "display": DISPLAY, "screen": HOSTNAME})
            while True:
                r, _, x = select.select([sock], [], [sock], 60)
                if x or not r:
                    # No traffic at all for 60s: ping to find out whether the
                    # phone is still there (iOS freezes background tabs).
                    if x:
                        return
                    try:
                        sock.sendall(ws_frame(b"", 0x9))
                        continue
                    except OSError:
                        return
                try:
                    data = sock.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                reader.feed(data)
                for opcode, payload in reader:
                    if opcode == 0x8:
                        return
                    if opcode == 0x9:
                        sock.sendall(ws_frame(payload, 0xA))
                        continue
                    if opcode == 0xA or not payload:
                        continue
                    try:
                        msg = json.loads(payload.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    out = self.pad_event(inj, msg)
                    if out is not None:
                        reply(out)
        except OSError:
            pass
        finally:
            # Release any stuck button/modifier: a tab closed mid-drag would
            # otherwise leave the desktop selecting forever.
            inj.close()

    def pad_event(self, inj, m):
        t = m.get("t")
        if t == "m":
            inj.move(_num(m.get("x")), _num(m.get("y")))
        elif t == "abs":
            inj.move_to(_num(m.get("x")), _num(m.get("y")))
        elif t == "c":
            inj.click(_num(m.get("b"), 1), _num(m.get("n"), 1))
        elif t == "b":
            inj.button(_num(m.get("b"), 1), bool(m.get("d")))
        elif t == "s":
            inj.scroll(str(m.get("d", "down")), _num(m.get("n"), 1))
        elif t == "k":
            inj.key(m.get("k", ""), _mods(m.get("mods")))
        elif t == "mod":
            inj.set_mod(str(m.get("m", "")), bool(m.get("d")))
        elif t == "type":
            inj.type_text(str(m.get("s", "")))
        elif t == "ping":
            return {"t": "pong"}
        return None

    def relay(self, ws, vnc):
        """Pumps both directions until one side closes.

        VNC is a raw TCP stream; the browser only speaks in frames. So:
        client -> unwrap frame, write the raw bytes to x11vnc
        x11vnc -> read raw bytes, wrap as a binary frame, send to the client
        """
        ws.setblocking(False)
        vnc.setblocking(False)
        reader = FrameReader()

        while True:
            try:
                r, _, x = select.select([ws, vnc], [], [ws, vnc], 30)
            except (OSError, ValueError):
                return
            if x:
                return

            if ws in r:
                try:
                    data = ws.recv(65536)
                except BlockingIOError:
                    data = b""
                except OSError:
                    return
                if data == b"" and ws in r:
                    return
                reader.feed(data)
                for opcode, payload in reader:
                    if opcode == 0x8:  # close
                        return
                    if opcode == 0x9:  # ping -> pong
                        try:
                            ws.sendall(ws_frame(payload, 0xA))
                        except OSError:
                            return
                        continue
                    if opcode == 0xA:  # pong, ignored
                        continue
                    if payload:
                        try:
                            vnc.sendall(payload)
                        except OSError:
                            return

            if vnc in r:
                try:
                    data = vnc.recv(65536)
                except BlockingIOError:
                    data = b""
                except OSError:
                    return
                if data == b"":
                    try:
                        ws.sendall(ws_frame(b"", 0x8))
                    except OSError:
                        pass
                    return
                try:
                    ws.sendall(ws_frame(data, 0x2))
                except OSError:
                    return


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    if not os.path.isdir(NOVNC):
        raise SystemExit(f"noVNC not found at {NOVNC} "
                         "(run ./scripts/get-novnc.sh)")

    tail = f"?t={TOKEN}" if TOKEN else ""

    # Besides 127.0.0.1, optionally listen on extra addresses (e.g. the
    # Tailscale IP). For the trackpad this matters: through a public proxy
    # every touch travels to the VPS and back; over Tailscale it goes straight
    # to the PC, and you can feel the difference under your finger.
    # At boot the network interface often comes up after this service, so the
    # first bind fails; keep retrying in the background until the IP exists.
    # Failing here must never take down the main listener.
    def listen_extra(extra):
        while True:
            try:
                side = Server((extra, PORT), Handler)
            except OSError as e:
                print(f"warning: could not listen on {extra}:{PORT} ({e}); "
                      "retrying in 10s", flush=True)
                time.sleep(10)
                continue
            print(f"remote-pc at http://{extra}:{PORT}/pad{tail}", flush=True)
            side.serve_forever()
            return

    for extra in filter(None, EXTRA_BINDS):
        threading.Thread(target=listen_extra, args=(extra,),
                         daemon=True).start()

    srv = Server((BIND, PORT), Handler)
    print(f"remote-pc at http://{BIND}:{PORT}/{tail}", flush=True)
    print(f"bridge -> {VNC_HOST}:{VNC_PORT} | trackpad at /pad on {DISPLAY}",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
