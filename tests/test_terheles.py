"""Terheles, ismetelt megszakitas, osszeomlas-szimulacio, memoria."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# A letolto.py lehet a teszt mellett vagy egy szinttel feljebb (tests/ mappa).
_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent)]
import tempfile
TMP = Path(tempfile.gettempdir()) / "letolto_terheles"
import kozos
import testsrv
from letolto import DownloadManager, Item, Status, human, make_client

srv = testsrv.serve(8771)
BASE = "http://127.0.0.1:8771/"
R = []


def check(name, ok, info=""):
    R.append(bool(ok))
    print(("[OK]   " if ok else "[HIBA] ") + name + (f"  -> {info}" if info else ""))


def fresh(name):
    d = TMP / Path(name).name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


def rss():
    """Pillanatnyi memoriahasznalat MB-ban; ahol nem merheto, ott 0.0."""
    return kozos.rss_mb() or 0.0


def md5f(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


client = make_client(16)

# ---------------------------------------------------- 1. sok elem memoriaja
print("\n--- 1. 20 000 elem memoriaigenye ---")
OUT = fresh("s_a")
before = rss()
mgr = DownloadManager(OUT, 4, client=client)
mgr.load_state()
t0 = time.perf_counter()
mgr.add_urls([f"http://pelda.hu/dir{i//100}/fajl{i}.pdf" for i in range(20_000)])
dt = time.perf_counter() - t0
after = rss()
per = (after - before) * 1024 / 20_000
print(f"    {dt:.2f}s, +{after - before:.1f} MB  (~{per:.0f} KB/elem)")
if kozos.merheto():
    check("20 000 elem < 40 MB", after - before < 40, f"+{after - before:.1f} MB")
else:
    print("    (ezen a rendszeren nem merheto a memoria, az ellenorzes kimarad)")
check("nyilvantartasba vetel < 5 s", dt < 5, f"{dt:.2f}s")

t0 = time.perf_counter()
mgr.store.save(mgr.items, force=True)
size = (OUT / "_letoltes_allapot.json").stat().st_size / 1e6
print(f"    allapotmentes: {time.perf_counter() - t0:.2f}s, {size:.1f} MB")
check("allapotmentes < 3 s", time.perf_counter() - t0 < 3)

t0 = time.perf_counter()
mgr2 = DownloadManager(OUT, 4, client=client)
mgr2.load_state()
print(f"    visszatoltes: {time.perf_counter() - t0:.2f}s, {len(mgr2.items)} elem")
check("visszatoltes hianytalan", len(mgr2.items) == 20_000)

# ---------------------------------------------------- 2. 10x megszakitas
print("\n--- 2. Tizszer megszakitott letoltes ---")
OUT = fresh("s_b")
testsrv.H.stall = 0.05
url = BASE + "huge/nolink"
rounds, sizes = 0, []
while rounds < 12:
    mgr = DownloadManager(OUT, 1, client=client)
    mgr.load_state()
    mgr.add_urls([url])
    if mgr.items[url].status == Status.DONE:
        break
    mgr.start()
    time.sleep(0.35)
    mgr.stop()
    mgr._worker.join(20)
    parts = list(OUT.rglob("*.part"))
    sizes.append(parts[0].stat().st_size if parts else -1)
    rounds += 1
print(f"    korok: {rounds}, reszmeretek: {sizes[:12]}")
check("minden korben nott a reszfajl",
      all(b >= a for a, b in zip(sizes, sizes[1:]) if a > 0 and b > 0), str(sizes))
testsrv.H.stall = 0
mgr = DownloadManager(OUT, 1, client=client)
mgr.load_state()
mgr.start()
mgr._worker.join(60)
dest = OUT / mgr.items[url].path
real = hashlib.md5(testsrv.FILES["/huge/nolink"]).hexdigest()
check("12 megszakitas utan is bitre ep a fajl", dest.exists() and md5f(dest) == real,
      f"{dest.stat().st_size if dest.exists() else -1} B")

# ---------------------------------------------------- 3. osszeomlas (kill -9)
print("\n--- 3. Osszeomlas-szimulacio (kill -9 letoltes kozben) ---")
OUT = fresh("s_c")
# Az utak es az URL repr-rel: Windowson a nyers "C:\\Users\\..." escape-nek latszana.
worker = f"""
import sys, time
sys.path[:0] = [{str(_HERE)!r}, {str(_HERE.parent)!r}]
from letolto import DownloadManager
m = DownloadManager({str(OUT)!r}, 1)
m.load_state(); m.add_urls([{url!r}]); m.start()
time.sleep(60)
"""
CRASH = TMP / "_crash.py"
CRASH.write_text(worker, encoding="utf-8")
testsrv.H.stall = 0.05
proc = subprocess.Popen([sys.executable, str(CRASH)])
time.sleep(4.0)
proc.kill()
proc.wait()
parts = list(OUT.rglob("*.part"))
partial = parts[0].stat().st_size if parts else 0
print(f"    kill utan .part = {partial} B, allapotfajl = {(OUT / '_letoltes_allapot.json').exists()}")
check("osszeomlas utan is van reszfajl", partial > 0)
testsrv.H.stall = 0
mgr = DownloadManager(OUT, 1, client=client)
mgr.load_state()
check("betoltes a .part valos meretevel szinkronban",
      mgr.items[url].done == partial, f"{mgr.items[url].done} vs {partial}")
mgr.start()
mgr._worker.join(60)
dest = OUT / mgr.items[url].path
check("osszeomlas utan folytatva ep a fajl", dest.exists() and md5f(dest) == real)

# ---------------------------------------------------- 4. serult allapotfajl
print("\n--- 4. Serult allapotfajl ---")
OUT = fresh("s_d")
(OUT / "_letoltes_allapot.json").write_text("{ ez nem json ]]", encoding="utf-8")
mgr = DownloadManager(OUT, 1, client=client)
try:
    mgr.load_state()
    check("serult allapotfajl nem okoz osszeomlast", True, f"{len(mgr.items)} elem")
except Exception as exc:
    check("serult allapotfajl nem okoz osszeomlast", False, repr(exc))

# ---------------------------------------------------- 5. 16 szal
print("\n--- 5. 16 szal parhuzamosan ---")
OUT = fresh("s_e")
urls = [BASE + f"files/{n}" for n in
        ("a.bin", "b.bin", "d.pdf", "img.png", "g.bin", "h.bin")] * 3
urls = [u + f"?v={i}" for i, u in enumerate(urls)]
mgr = DownloadManager(OUT, 16, client=client)
mgr.load_state()
mgr.add_urls(urls)
t0 = time.perf_counter()
before = rss()
mgr.start()
mgr._worker.join(120)
dt = time.perf_counter() - t0
done = sum(1 for i in mgr.items.values() if i.status == Status.DONE)
print(f"    {done}/{len(urls)} fajl {dt:.2f}s alatt, RSS {rss():.1f} MB (+{rss() - before:.1f})")
check("16 szalon minden fajl kesz", done == len(urls))
if kozos.merheto():
    check("nincs memoriaduzzadas 16 szalon", rss() - before < 40, f"+{rss() - before:.1f} MB")
else:
    print("    (ezen a rendszeren nem merheto a memoria, az ellenorzes kimarad)")

# ---------------------------------------------------- 6. szalbiztonsag
print("\n--- 6. Konyveles szalbiztonsaga ---")
tot = sum(i.done for i in mgr.items.values())
check("osszesito == elemek osszege", mgr.totals.bytes_done == tot,
      f"{mgr.totals.bytes_done} vs {tot}")
real_bytes = sum(p.stat().st_size for p in OUT.rglob("*")
                 if p.is_file() and p.name != "_letoltes_allapot.json")
check("osszesito == lemezen levo bajtok", mgr.totals.bytes_done == real_bytes,
      f"{mgr.totals.bytes_done} vs {real_bytes}")

print("\n=== OSSZEGZES: %d / %d ===" % (sum(R), len(R)))
client.close()
srv.shutdown()
sys.exit(0 if all(R) else 1)
