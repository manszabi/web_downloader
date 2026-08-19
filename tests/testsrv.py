"""Teszt HTTP szerver: Range, ETag, gzip, lassu es hibas vegpontokkal.

A GUI-tesztekhez itt van a beallitasfajl elkulonitese is (temp_settings).
"""
import gzip
import hashlib
import os
import re
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HTML_INDEX = """<html><body>
<h1>Index</h1>
<a href="files/a.bin">a.bin</a>
<a href="files/b.bin">b.bin</a>
<a href="files/c%20space.bin">c space.bin</a>
<a href="files/d.pdf">d.pdf</a>
<a href="norange/e.bin">e.bin (nincs range)</a>
<a href="gz/f.bin">f.bin (gzip)</a>
<a href="huge/nolink">extension nelkuli nagy fajl</a>
<a href="sub/page2.html">alolap</a>
<a href="/sub/page2.html#frag">alolap ismet</a>
<a href="http://kulso.example.com/x.bin">kulso domain</a>
<a href="javascript:void(0)">js</a>
<img src="files/img.png">
<a href="files/CON.txt">reserved nev</a>
</body></html>"""

HTML_PAGE2 = """<html><body>
<a href="../files/g.bin">g.bin</a>
<a href="../files/a.bin">a.bin ismet (duplikatum)</a>
<a href="page3.html">page3</a>
</body></html>"""

HTML_PAGE3 = """<html><body><a href="../files/h.bin">h.bin</a></body></html>"""

FILES = {}


def make_files():
    for name, size in [("a.bin", 300_000), ("b.bin", 150_000), ("c space.bin", 50_000),
                       ("d.pdf", 80_000), ("img.png", 20_000), ("g.bin", 120_000),
                       ("h.bin", 60_000), ("CON.txt", 1_000)]:
        FILES["/files/" + name] = os.urandom(size)
    FILES["/norange/e.bin"] = os.urandom(200_000)
    FILES["/gz/f.bin"] = os.urandom(100_000)
    FILES["/huge/nolink"] = os.urandom(5_000_000)   # extension nelkuli, nagy


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    stall = 0.0            # osztalyszintu lassitas byte-onkent
    robots_status = 404    # a /robots.txt valasza (200 eseten a robots_body megy vissza)
    robots_body = ""

    def log_message(self, *a):
        pass

    def _html(self, body, head=False):
        """HTML valasz. HEAD keresre CSAK a fejlec megy ki.

        Ha a torzset is elkuldenenk, a keep-alive kapcsolaton a kovetkezo valasz
        elejenek nezne a kliens: a HEAD-et hasznalo meretlekerdezes ettol
        veletlenszeruen elbukott.
        """
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._html(HTML_INDEX, head)
        if path == "/sub/page2.html":
            return self._html(HTML_PAGE2, head)
        if path == "/sub/page3.html":
            return self._html(HTML_PAGE3, head)
        if path == "/robots.txt":
            if H.robots_status == 200:
                body = H.robots_body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not head:
                    self.wfile.write(body)
                return
            self.send_response(H.robots_status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        from urllib.parse import unquote
        key = unquote(path)
        if key not in FILES:
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return

        data = FILES[key]
        etag = '"%s"' % hashlib.md5(data).hexdigest()[:16]

        # gzip vegpont: Content-Encoding: gzip, tomoritett tartalommal
        if key.startswith("/gz/"):
            body = gzip.compress(data)
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", etag)
            self.end_headers()
            if not head:
                self.wfile.write(body)
            return

        # norange vegpont: figyelmen kivul hagyja a Range-t
        if key.startswith("/norange/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if not head:
                self.wfile.write(data)
            return

        rng = self.headers.get("Range")
        start = 0
        if rng:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            if m:
                start = int(m.group(1))
                if start >= len(data):
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % len(data))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, len(data) - 1, len(data)))
            else:
                self.send_response(200)
        else:
            self.send_response(200)
        chunk = data[start:]
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.end_headers()
        if head:
            return
        # darabokban kuldjuk, hogy meg lehessen szakitani
        step = 16384
        for i in range(0, len(chunk), step):
            try:
                self.wfile.write(chunk[i:i + step])
                if H.stall:
                    time.sleep(H.stall)
            except (BrokenPipeError, ConnectionResetError):
                return


def temp_settings(letolto, name):
    """A GUI-teszt sajat, ures beallitasfajlt kapjon a valodi helyett.

    Enelkul minden futas a fejleszto (vagy az elozo teszt) beallitasait toltene
    be es irna felul: a HTML-kapcsolo, a szalszam vagy a szuro atszivarogna a
    kovetkezo tesztbe, es a hibak nehezen reprodukalhatova valnanak.
    A run_gui() elott kell meghivni.
    """
    path = Path(tempfile.gettempdir()) / "letolto_teszt_beallitasok" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    letolto.SETTINGS_FILE = path
    letolto.LOG_FILE = path.with_name(f"{name}.log")   # a naplo se a valodi helyere menjen
    return path


def serve(port=8765):
    make_files()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


if __name__ == "__main__":
    s = serve()
    print("http://127.0.0.1:8765/")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        s.shutdown()
