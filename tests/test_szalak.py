"""Menet kozbeni szalszam-valtoztatas tesztje (mag es GUI)."""
import hashlib
import shutil
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

# A letolto.py lehet a teszt mellett vagy egy szinttel feljebb (tests/ mappa).
_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent)]
import testsrv
from letolto import MAX_THREADS, DownloadManager, Status, make_client

srv = testsrv.serve(8795)
BASE = "http://127.0.0.1:8795/"
R = []


def check(name, ok, info=""):
    R.append(bool(ok))
    print(("[OK]   " if ok else "[HIBA] ") + name + (f"  -> {info}" if info else ""))


TMP = Path(tempfile.gettempdir()) / "letolto_teszt"


def fresh(name):
    d = TMP / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


def running_count(mgr):
    return sum(1 for i in mgr.items.values() if i.status == Status.RUNNING)


def md5f(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


# 24 lassu fajl, hogy legyen mit parhuzamositani
for i in range(48):
    testsrv.FILES[f"/files/t{i}.bin"] = testsrv.FILES["/files/a.bin"]
URLS = [BASE + f"files/t{i}.bin" for i in range(48)]
client = make_client(MAX_THREADS)

print("\n--- 1. Novelés futás közben (2 -> 8) ---")
OUT = fresh("th_a")
testsrv.H.stall = 0.03
mgr = DownloadManager(OUT, 2, client=client)
mgr.load_state()
mgr.add_urls(URLS)
mgr.start()
time.sleep(1.5)
before = running_count(mgr)
active_before = mgr._active
mgr.set_threads(8)
time.sleep(0.4)                     # a felügyelő 0,15 s-enként lép: ennyi bőven elég
after = running_count(mgr)
active_after = mgr._active
print(f"    parhuzamosan futo fajlok: {before} -> {after}   (munkasszalak: {active_before} -> {active_after})")
check("2 szalon kb. 2 fajl fut", before <= 2, str(before))
check("novelés utan azonnal tobb fajl fut", after >= 6, str(after))
check("a munkasszalak szama is nott", active_after == 8, str(active_after))

print("\n--- 2. Csokkentes futas kozben (8 -> 2) ---")
mgr.set_threads(2)
time.sleep(2.5)                     # a fölös szálak befejezik a fájljukat, majd kilépnek
after2 = running_count(mgr)
print(f"    parhuzamosan futo fajlok: {after2}  (munkasszalak: {mgr._active})")
check("csokkentes utan visszaall", mgr._active <= 2, str(mgr._active))
check("nincs elarvult 'letoltes' statusz", after2 <= 2, str(after2))

testsrv.H.stall = 0
mgr._worker.join(120)
done = sum(1 for i in mgr.items.values() if i.status == Status.DONE)
check("a valtoztatasok utan is minden fajl elkeszul", done == len(URLS), f"{done}/{len(URLS)}")
bad = [i.path for i in mgr.items.values()
       if md5f(OUT / i.path) != hashlib.md5(
           testsrv.FILES[urllib.parse.unquote(urllib.parse.urlparse(i.url).path)]).hexdigest()]
check("minden fajl bitre ep", not bad, "; ".join(bad[:3]))

print("\n--- 3. Ismetelt fel-le kapcsolgatas ---")
OUT = fresh("th_b")
testsrv.H.stall = 0.02
mgr = DownloadManager(OUT, 1, client=client)
mgr.load_state()
mgr.add_urls(URLS)
mgr.start()
for n in (6, 1, 12, 3, 16, 2):
    mgr.set_threads(n)
    time.sleep(0.4)
testsrv.H.stall = 0
mgr.set_threads(8)
mgr._worker.join(120)
done = sum(1 for i in mgr.items.values() if i.status == Status.DONE)
check("24 gyors valtas utan is minden kesz", done == len(URLS), f"{done}/{len(URLS)}")
check("nem maradt elszabadult szal", mgr._active == 0, str(mgr._active))
check("nem maradt .part", not list(OUT.rglob("*.part")))

print("\n--- 4. Hatarertekek ---")
mgr.set_threads(0)
check("0 helyett 1", mgr.threads == 1, str(mgr.threads))
mgr.set_threads(999)
check("felso hatarra vagva", mgr.threads == MAX_THREADS, str(mgr.threads))

print("\n--- 5. Szunet + szalszam egyutt ---")
OUT = fresh("th_c")
testsrv.H.stall = 0.02
mgr = DownloadManager(OUT, 2, client=client)
mgr.load_state()
mgr.add_urls(URLS[:8])
mgr.start()
time.sleep(0.8)
mgr.pause(True)
mgr.set_threads(8)
time.sleep(1.0)
a = mgr.totals.bytes_done
time.sleep(0.8)
check("szunetben a novelés sem indit uj forgalmat", a == mgr.totals.bytes_done,
      f"{a} -> {mgr.totals.bytes_done}")
testsrv.H.stall = 0
mgr.pause(False)
mgr._worker.join(120)
check("folytatas utan minden kesz",
      sum(1 for i in mgr.items.values() if i.status == Status.DONE) == 8)

print("\n--- 6. GUI: a lepteto azonnal hat ---")
import tkinter as tk
import letolto
cap = {}
ml = tk.Tk.mainloop
tk.Tk.mainloop = lambda s: cap.setdefault("a", s)
testsrv.temp_settings(letolto, "szalak")
letolto.run_gui()
app = cap["a"]
tk.Tk.mainloop = ml
OUT = fresh("th_d")
testsrv.H.stall = 0.03
app.v_dir.set(str(OUT))
app.v_threads.set(2)
m = app._ensure_manager()
m.add_urls(URLS)
app._start()
for _ in range(50):
    app.update()
    time.sleep(0.03)
before = running_count(m)
app.v_threads.set(10)            # <<< a felhasznalo felhuzza a leptetot
for _ in range(15):
    app.update()
    time.sleep(0.03)
after = running_count(m)
print(f"    GUI-bol: {before} -> {after} parhuzamos fajl, manager.threads={m.threads}")
check("a lepteto atallitasa azonnal hat a magra", m.threads == 10, str(m.threads))
check("tenylegesen tobb fajl fut parhuzamosan", after > before, f"{before} -> {after}")
check("a naplo jelzi a valtozast", "Szálak száma" in app.log.get("1.0", "end"))
testsrv.H.stall = 0
for _ in range(300):
    app.update()
    time.sleep(0.02)
    if not m.running:
        break
check("GUI-bol vezerelve is minden fajl elkeszul",
      sum(1 for i in m.items.values() if i.status == Status.DONE) == len(URLS))
app._on_close()

print("\n=== OSSZEGZES: %d / %d ===" % (sum(R), len(R)))
client.close()
srv.shutdown()
sys.exit(0 if all(R) else 1)
