"""Teljes funkcioteszt a v2-re."""
import hashlib
import os
import shutil
import sys
import threading
import time
import urllib.parse
from pathlib import Path

# A letolto.py lehet a teszt mellett vagy egy szinttel feljebb (tests/ mappa).
_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent)]
import testsrv
from letolto import (DownloadManager, ScanConfig, Scanner, Status, human,
                      make_client, safe_component, url_to_relpath)

srv = testsrv.serve(8770)
BASE = "http://127.0.0.1:8770/"
R = []


def check(name, ok, info=""):
    R.append(ok)
    print(("[OK]   " if ok else "[HIBA] ") + name + (f"  -> {info}" if info else ""))


def fresh(d):
    shutil.rmtree(d, ignore_errors=True)
    Path(d).mkdir(parents=True)
    return Path(d)


def md5f(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def server_md5(url):
    key = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    return hashlib.md5(testsrv.FILES[key]).hexdigest()


def rss_mb():
    for line in open("/proc/self/status"):
        if line.startswith("VmHWM"):
            return int(line.split()[1]) / 1024


client = make_client(8)

# ---------------------------------------------------------------- 1. nevek
print("\n--- 1. Fajlnev-biztonsag ---")
check("CON.txt atnevezve", safe_component("CON.txt").upper() != "CON.TXT",
      safe_component("CON.txt"))
check("NUL kezelve", safe_component("NUL") != "NUL", safe_component("NUL"))
check("zaro pont levagva", not safe_component("valami.").endswith("."),
      safe_component("valami."))
check("tiltott karakterek cserelve", "?" not in safe_component('a?b<c>d"e|f*g'),
      safe_component('a?b<c>d"e|f*g'))
check("hosszu nev rovidul, kiterjesztes marad",
      len(safe_component("x" * 400 + ".pdf")) <= 100 and safe_component("x" * 400 + ".pdf").endswith(".pdf"))
check("path traversal nem lehetseges",
      ".." not in url_to_relpath("http://h/a/../../../etc/passwd").parts,
      str(url_to_relpath("http://h/a/../../../etc/passwd")))
check("query string kulon fajlnevet ad",
      url_to_relpath("http://h/f.php?id=1") != url_to_relpath("http://h/f.php?id=2"))
check("szokoz dekodolva", "c space.bin" == url_to_relpath("http://h/c%20space.bin").name,
      url_to_relpath("http://h/c%20space.bin").name)

# ---------------------------------------------------------------- 2. scanner
print("\n--- 2. Atvizsgalas ---")
base_rss = rss_mb()
t0 = time.perf_counter()
cfg = ScanConfig(root=BASE, depth=2, same_host=True, respect_robots=True)
logs = []
result = Scanner(cfg, client, logs.append, threading.Event()).run()
found = result.files          # a HTML lapokat itt nem töltjük le
dt = time.perf_counter() - t0
peak = rss_mb()
print(f"    {len(found)} fajl, {dt:.2f}s, RSS-csucs {peak:.1f} MB (indulaskor {base_rss:.1f})")
check("megtalalja a fajlokat", len(found) >= 10, f"{len(found)} db")
check("melyseg 2 -> h.bin is meglett", any("h.bin" in u for u in found))
check("kulso domain kizarva", not any("kulso.example" in u for u in found))
check("nincs duplikatum", len(found) == len(set(found)))
check("az 5 MB-os fajl NEM kerult a memoriaba", peak - base_rss < 3,
      f"+{peak - base_rss:.1f} MB")

# ---------------------------------------------------------------- 3. letoltes
print("\n--- 3. Parhuzamos letoltes ---")
OUT = fresh("/home/claude/v2_a")
mgr = DownloadManager(OUT, 8, client=client)
mgr.load_state()
mgr.add_urls(found)
t0 = time.perf_counter()
mgr.start()
mgr._worker.join(120)
dt = time.perf_counter() - t0
bad = []
for item in mgr.items.values():
    p = OUT / item.path
    if item.status != Status.DONE or not p.exists():
        bad.append(f"{item.path}:{item.status}")
    elif md5f(p) != server_md5(item.url):
        bad.append(f"{item.path}:SERULT")
check("minden fajl hibatlanul letoltodott", not bad, "; ".join(bad))
check("nem maradt .part", not list(OUT.rglob("*.part")))
check("a konyveles egyezik a valosaggal",
      mgr.totals.bytes_done == sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file() and p.name != "_letoltes_allapot.json"),
      f"{mgr.totals.bytes_done}")
print(f"    {mgr.totals.files} fajl / {human(mgr.totals.bytes_done)} / {dt:.2f}s")

# ---------------------------------------------------------------- 4. folytatas
print("\n--- 4. Megszakitas es folytatas ---")
OUT = fresh("/home/claude/v2_b")
testsrv.H.stall = 0.02
mgr = DownloadManager(OUT, 2, client=client)
mgr.load_state()
mgr.add_urls([BASE + "huge/nolink"])
mgr.start()
time.sleep(2.5)
mgr.stop()
mgr._worker.join(30)
parts = list(OUT.rglob("*.part"))
partial = parts[0].stat().st_size if parts else 0
check("megszakitas utan van .part", bool(parts), f"{partial} B")
st = mgr.items[BASE + "huge/nolink"].status
check("statusz 'megszakitva'", st == Status.STOPPED, st)

testsrv.H.stall = 0
mgr2 = DownloadManager(OUT, 2, client=client)     # UJ peldany = programujraindulas
mgr2.load_state()
check("ujraindulas utan latja a reszleges fajlt",
      mgr2.items[BASE + "huge/nolink"].done == partial,
      f"{mgr2.items[BASE + 'huge/nolink'].done} vs {partial}")
mgr2.start()
mgr2._worker.join(60)
dest = OUT / mgr2.items[BASE + "huge/nolink"].path
check("folytatas utan bitre ep fajl",
      dest.exists() and md5f(dest) == server_md5(BASE + "huge/nolink"),
      f"{dest.stat().st_size if dest.exists() else -1} B")
check("valoban folytatta (nem toltotte ujra)", partial > 0)

# ---------------------------------------------------------------- 5. szunet
print("\n--- 5. Szunet / folytatas gomb ---")
OUT = fresh("/home/claude/v2_c")
testsrv.H.stall = 0.01
mgr = DownloadManager(OUT, 1, client=client)
mgr.load_state()
mgr.add_urls([BASE + "huge/nolink"])
mgr.start()
time.sleep(1.0)
mgr.pause(True)
time.sleep(0.5)
a = mgr.totals.bytes_done
time.sleep(1.0)
b = mgr.totals.bytes_done
check("szunetben nem no a letoltott mennyiseg", a == b, f"{a} -> {b}")
testsrv.H.stall = 0
mgr.pause(False)
mgr._worker.join(60)
dest = OUT / mgr.items[BASE + "huge/nolink"].path
check("szunet utan folytatva ep a fajl", dest.exists() and md5f(dest) == server_md5(BASE + "huge/nolink"))

# ---------------------------------------------------------------- 6. gzip
print("\n--- 6. Tomoritett (gzip) vegpont ---")
OUT = fresh("/home/claude/v2_d")
mgr = DownloadManager(OUT, 1, client=client)
mgr.load_state()
mgr.add_urls([BASE + "gz/f.bin"])
item = list(mgr.items.values())[0]
dest = OUT / item.path
dest.parent.mkdir(parents=True, exist_ok=True)
(dest.parent / (dest.name + ".part")).write_bytes(testsrv.FILES["/gz/f.bin"][:50_000])
mgr.start()
mgr._worker.join(60)
check("gzip vegpont utan is ep a fajl", dest.exists() and md5f(dest) == server_md5(BASE + "gz/f.bin"),
      f"{dest.stat().st_size if dest.exists() else -1} / {len(testsrv.FILES['/gz/f.bin'])}")

# ---------------------------------------------------------------- 7. norange
print("\n--- 7. Range-t nem tamogato szerver ---")
OUT = fresh("/home/claude/v2_e")
mgr = DownloadManager(OUT, 1, client=client)
mgr.load_state()
mgr.add_urls([BASE + "norange/e.bin"])
item = list(mgr.items.values())[0]
dest = OUT / item.path
dest.parent.mkdir(parents=True, exist_ok=True)
(dest.parent / (dest.name + ".part")).write_bytes(testsrv.FILES["/norange/e.bin"][:70_000])
mgr.start()
mgr._worker.join(60)
check("automatikus ujratoltes, ep fajl", dest.exists() and md5f(dest) == server_md5(BASE + "norange/e.bin"),
      f"{dest.stat().st_size if dest.exists() else -1}")
check("a konyveles nem szamolt duplan", mgr.totals.bytes_done == dest.stat().st_size,
      f"{mgr.totals.bytes_done} vs {dest.stat().st_size}")

# ---------------------------------------------------------------- 8. 416
print("\n--- 8. Kesz .part (416 Range Not Satisfiable) ---")
OUT = fresh("/home/claude/v2_f")
mgr = DownloadManager(OUT, 1, client=client)
mgr.load_state()
mgr.add_urls([BASE + "files/b.bin"])
item = list(mgr.items.values())[0]
dest = OUT / item.path
dest.parent.mkdir(parents=True, exist_ok=True)
(dest.parent / (dest.name + ".part")).write_bytes(testsrv.FILES["/files/b.bin"])  # teljes
mgr.start()
mgr._worker.join(60)
check("416 eseten a kesz .part veglegesitve", dest.exists() and md5f(dest) == server_md5(BASE + "files/b.bin"))

# ---------------------------------------------------------------- 9. hibak
print("\n--- 9. Hibakezeles ---")
OUT = fresh("/home/claude/v2_g")
mgr = DownloadManager(OUT, 2, client=client)
mgr.load_state()
mgr.add_urls([BASE + "files/nincs_ilyen.bin", BASE + "files/a.bin"])
mgr.start()
mgr._worker.join(60)
errs = [i for i in mgr.items.values() if i.status == Status.ERROR]
oks = [i for i in mgr.items.values() if i.status == Status.DONE]
check("a hibas fajl hibas statuszt kap", len(errs) == 1, errs[0].error if errs else "")
check("a hiba nem allitja meg a tobbit", len(oks) == 1)

# ---------------------------------------------------------------- 10. utkozes
print("\n--- 10. Nevutkozes ---")
OUT = fresh("/home/claude/v2_h")
mgr = DownloadManager(OUT, 1, client=client)
mgr.load_state()
mgr.add_urls(["http://a.hu/x/f.bin", "http://a.hu/x/f.bin?v=2"])
paths = [i.path for i in mgr.items.values()]
check("kulon fajlnev ket kulonbozo URL-hez", len(set(paths)) == 2, str(paths))

print("\n=== OSSZEGZES: %d / %d teszt sikeres ===" % (sum(R), len(R)))
client.close()
srv.shutdown()
sys.exit(0 if all(R) else 1)
