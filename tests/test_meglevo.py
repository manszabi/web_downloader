"""A mar meglevo fajlok kezelesenek tesztje (kihagyas / meret-ellenorzes / ujratoltes)."""
import hashlib
import shutil
import sys
import tempfile
import time
from pathlib import Path

# A letolto.py lehet a teszt mellett vagy egy szinttel feljebb (tests/ mappa).
_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent)]
import testsrv
from letolto import DownloadManager, Existing, Status, make_client

srv = testsrv.serve(8801)
BASE = "http://127.0.0.1:8801/"
URL = BASE + "files/a.bin"
REAL = testsrv.FILES["/files/a.bin"]
TMP = Path(tempfile.gettempdir()) / "letolto_meglevo"
client = make_client(4)
R = []


def check(name, ok, info=""):
    R.append(ok)
    print(("[OK]   " if ok else "[HIBA] ") + name + (f"  -> {info}" if info else ""))


def fresh(name):
    d = TMP / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


def place(outdir, data):
    """Fajl elhelyezese a celkonyvtarban, allapotfajl nelkul."""
    p = outdir / "127.0.0.1_8801" / "files"
    p.mkdir(parents=True, exist_ok=True)
    (p / "a.bin").write_bytes(data)
    return p / "a.bin"


def run(outdir, policy):
    mgr = DownloadManager(outdir, 1, client=client, existing=policy)
    mgr.load_state()
    mgr.add_urls([URL])
    mgr.start()
    mgr._worker.join(60)
    return mgr


print("\n--- 1. Ép fájl, saját korábbi letöltésből (állapotfájllal) ---")
OUT = fresh("m_a")
m = run(OUT, Existing.VERIFY)
first_bytes = m.totals.bytes_done
t0 = time.perf_counter()
m2 = run(OUT, Existing.VERIFY)
dt = time.perf_counter() - t0
check("második futásnál nem tölti újra", m2.items[URL].status == Status.DONE and dt < 1.0,
      f"{dt:.2f}s")
check("HEAD kérés sem kell hozzá (a méret ismert)", dt < 0.5, f"{dt:.2f}s")

print("\n--- 2. Ép fájl, de állapotfájl nélkül (kívülről bemásolva) ---")
OUT = fresh("m_b")
f = place(OUT, REAL)
m = run(OUT, Existing.VERIFY)
check("ép fájl elfogadva, nem tölti újra",
      m.items[URL].status == Status.DONE and f.read_bytes() == REAL)
check("a méretet a szervertől igazolta", m.items[URL].total == len(REAL), str(m.items[URL].total))

print("\n--- 3. CSONKA fájl, méret-ellenőrzéssel ---")
OUT = fresh("m_c")
f = place(OUT, REAL[:100_000])
m = run(OUT, Existing.VERIFY)
ok = f.read_bytes() == REAL
check("a csonka fájlt felismeri és újratölti", ok,
      f"{f.stat().st_size} bájt, elvárt {len(REAL)}")
check("a végeredmény bitre ép",
      hashlib.md5(f.read_bytes()).hexdigest() == hashlib.md5(REAL).hexdigest())

print("\n--- 4. CSONKA fájl, 'kihagyás' házirenddel ---")
OUT = fresh("m_d")
f = place(OUT, REAL[:100_000])
m = run(OUT, Existing.SKIP)
check("kihagyás módban tényleg nem nyúl hozzá", f.stat().st_size == 100_000,
      f"{f.stat().st_size}")

print("\n--- 5. Ép fájl, 'újratöltés' házirenddel ---")
OUT = fresh("m_e")
f = place(OUT, REAL)
m = run(OUT, Existing.REDOWNLOAD)
check("újratöltés módban letölti", m.totals.bytes_done == len(REAL), str(m.totals.bytes_done))
check("és utána is ép", f.read_bytes() == REAL)

print("\n--- 6. Túl NAGY fájl (más tartalom, nagyobb méret) ---")
OUT = fresh("m_f")
f = place(OUT, REAL + b"x" * 50_000)
m = run(OUT, Existing.VERIFY)
check("eltérő méretet felismeri és javítja", f.read_bytes() == REAL,
      f"{f.stat().st_size}")

print("\n--- 7. A régi fájl megmarad, amíg az új el nem készül ---")
# 5 MB-os fájllal, hogy a mérés biztosan a letöltés KÖZBEN történjen
OUT = fresh("m_g")
big = OUT / "127.0.0.1_8801" / "huge"
big.mkdir(parents=True)
f = big / "nolink"
f.write_bytes(b"regi tartalom" * 8000)          # ~104 KB, eltérő mérettel
testsrv.H.stall = 0.02
mgr = DownloadManager(OUT, 1, client=client, existing=Existing.VERIFY)
mgr.load_state()
mgr.add_urls([BASE + "huge/nolink"])
mgr.start()
time.sleep(1.5)
exists_during = f.exists()
size_during = f.stat().st_size if exists_during else -1
mgr.stop()
mgr._worker.join(30)
testsrv.H.stall = 0
check("újratöltés közben a régi fájl a helyén marad",
      exists_during and size_during == 8000 * len(b"regi tartalom"), f"{size_during}")
check("a részletek külön .part fájlba mennek", bool(list(OUT.rglob("*.part"))))

print("\n--- 8. Nem ellenőrizhető méret (a szerver nem ad Content-Length-et) ---")
OUT = fresh("m_h")
p = OUT / "127.0.0.1_8801" / "norange"
p.mkdir(parents=True)
(p / "e.bin").write_bytes(testsrv.FILES["/norange/e.bin"])
m = DownloadManager(OUT, 1, client=client, existing=Existing.VERIFY)
m.load_state()
m.add_urls([BASE + "norange/e.bin"])
m.start()
m._worker.join(60)
check("ellenőrizhető esetben is helyes marad",
      (p / "e.bin").read_bytes() == testsrv.FILES["/norange/e.bin"])

print("\n--- 9. Sok meglévő fájl: mennyi ideig tart az ellenőrzés ---")
OUT = fresh("m_i")
urls = []
for i in range(20):
    testsrv.FILES[f"/files/v{i}.bin"] = REAL
    urls.append(BASE + f"files/v{i}.bin")
    d = OUT / "127.0.0.1_8801" / "files"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"v{i}.bin").write_bytes(REAL)
m = DownloadManager(OUT, 8, client=client, existing=Existing.VERIFY)
m.load_state()
m.add_urls(urls)
t0 = time.perf_counter()
m.start()
m._worker.join(60)
dt = time.perf_counter() - t0
done = sum(1 for i in m.items.values() if i.status == Status.DONE)
print(f"    20 meglévő fájl ellenőrzése: {dt:.2f}s, letöltött bájt: {m.totals.bytes_done}")
check("mind a 20 kész lett", done == 20, f"{done}/20")
check("nem töltött le semmit fölöslegesen", m.totals.bytes_done == 20 * len(REAL),
      "csak a meglévők méretét könyveli")
check("az ellenőrzés gyors (HEAD kérések)", dt < 5, f"{dt:.2f}s")

print("\n=== OSSZEGZES: %d / %d ===" % (sum(R), len(R)))
client.close()
srv.shutdown()
sys.exit(0 if all(R) else 1)
