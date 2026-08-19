"""Verseny-, memoria- es processzoridovel kapcsolatos ellenorzesek.

A meresek nem abszolut hatarokat vizsgalnak (az a gepfuggo lenne), hanem
aranyokat es hivasszamokat: kotegel-e a program, ahol kell, es tulel-e egy
parhuzamos modositast.
"""
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

# A letolto.py lehet a teszt mellett vagy egy szinttel feljebb (tests/ mappa).
_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent)]
import testsrv
import letolto
from letolto import DownloadManager, Item

srv = testsrv.serve(8813)
BASE = "http://127.0.0.1:8813/"
TMP = Path(tempfile.gettempdir()) / "letolto_hatekonysag"
R = []


def check(name, ok, info=""):
    R.append(bool(ok))
    print(("[OK]   " if ok else "[HIBA] ") + name + (f"  -> {info}" if info else ""))


def fresh(nev):
    d = TMP / nev
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


def sok_url(n, elo=""):
    return [f"http://pelda.hu/{elo}f{i}.bin" for i in range(n)]


print("\n--- 1. Allapotmentes parhuzamos bovites kozben ---")
mgr = DownloadManager(fresh("verseny"), 1)
mgr.add_urls(sok_url(5000))
hibak = []
allj = threading.Event()


def mento():
    while not allj.is_set():
        try:
            mgr.store.mark_dirty()
            mgr.store.save(mgr.items)
        except Exception as exc:
            hibak.append(f"{type(exc).__name__}: {exc}")
            return


def bovito():
    for i in range(5000, 15000):
        url = f"http://uj.hu/{i}"
        mgr.items[url] = Item(url=url, path=f"u{i}")


t1 = threading.Thread(target=mento)
t2 = threading.Thread(target=bovito)
t1.start(); t2.start()
t2.join(); allj.set(); t1.join()
check("a mentes tullel egy parhuzamos bovitest", not hibak, str(hibak[:1]))
check("a mentett fajl ervenyes JSON",
      "items" in letolto.json.loads(mgr.store.path.read_text(encoding="utf-8")))

print("\n--- 2. A mento szal nem hal el csendben ---")
eredeti_alap, eredeti_elem = letolto.AUTOSAVE_SECONDS, letolto.AUTOSAVE_PER_ITEM
letolto.AUTOSAVE_SECONDS, letolto.AUTOSAVE_PER_ITEM = 0.01, 0.0
hivasok = []


class Hibas(DownloadManager):
    """Olyan kezelo, amelynek a mentese mindig elszall."""

    korok = 0

    @property
    def running(self):
        Hibas.korok += 1
        return Hibas.korok <= 5


bukos = Hibas(fresh("mento"), 1)
bukos.store.save = lambda *a, **k: (hivasok.append(1), (_ for _ in ()).throw(OSError("lemez")))[0]
try:
    bukos._autosave()
    elszallt = False
except Exception as exc:
    elszallt = f"{type(exc).__name__}: {exc}"
letolto.AUTOSAVE_SECONDS, letolto.AUTOSAVE_PER_ITEM = eredeti_alap, eredeti_elem
check("a hibas mentes nem dobja el a szalat", elszallt is False, str(elszallt))
check("es a kovetkezo korben ujra probalkozik", len(hivasok) >= 3, f"{len(hivasok)} kor")

print("\n--- 3. A mentes uteme a lista meretehez igazodik ---")
kicsi = DownloadManager(fresh("utem_kicsi"), 1)
kicsi.add_urls(sok_url(10))
nagy = DownloadManager(fresh("utem_nagy"), 1)
nagy.add_urls(sok_url(20000))
check("kis listanal a szokasos utem", kicsi._autosave_interval() <= 3.1,
      f"{kicsi._autosave_interval():.1f} s")
check("nagy listanal ritkabban ment", nagy._autosave_interval() > 10,
      f"{nagy._autosave_interval():.1f} s")
check("de sosem ritkabban a felso hatarnal",
      nagy._autosave_interval() <= letolto.AUTOSAVE_MAX_SECONDS)

t0 = time.perf_counter()
nagy.store.save(nagy.items, force=True)
egy_mentes = (time.perf_counter() - t0) * 1000
print(f"    20 000 elem mentese: {egy_mentes:.0f} ms, "
      f"{nagy.store.path.stat().st_size / 1024:.0f} KB, "
      f"{nagy._autosave_interval():.0f} masodpercenkent")

print("\n--- 4. A mentes Windowson is ujraprobalkozik ---")
cserek = []
eredeti_csere = letolto.atomic_replace
letolto.atomic_replace = lambda src, dst, attempts=5: (cserek.append(dst.name),
                                                       eredeti_csere(src, dst, attempts))[1]
try:
    kicsi.store.save(kicsi.items, force=True)
finally:
    letolto.atomic_replace = eredeti_csere
check("az allapotfajl az atomic_replace-en keresztul cserelodik",
      cserek == [letolto.STATE_FILE], str(cserek))

print("\n--- 5. A lemezre iras tenyleg kikerul a lemezre ---")
fsyncek = []
eredeti_fsync = letolto.os.fsync
eredeti_flush = letolto.FLUSH_SECONDS
letolto.os.fsync = lambda fd: (fsyncek.append(fd), eredeti_fsync(fd))[1]
letolto.FLUSH_SECONDS = 0.0            # minden darab utan urites, hogy gyors legyen
OUT = fresh("fsync")
client = letolto.make_client(2)
mgr2 = DownloadManager(OUT, 2, client=client)
mgr2.load_state()
mgr2.add_urls([BASE + "files/a.bin"])
try:
    mgr2.start()
    mgr2._worker.join(60)
finally:
    letolto.os.fsync = eredeti_fsync
    letolto.FLUSH_SECONDS = eredeti_flush
check("a letoltes kesz", all(i.status == letolto.Status.DONE for i in mgr2.items.values()))
check("kozben az fsync is lefutott", len(fsyncek) > 0, f"{len(fsyncek)} hivas")

print("\n--- 6. A darabolas merete ---")
darabok = []
with client.stream("GET", BASE + "huge/nolink",
                   headers={"Accept-Encoding": "identity"}) as resp:
    for chunk in resp.iter_bytes():
        darabok.append(len(chunk))
atlag = sum(darabok) / len(darabok)
print(f"    5 MB: {len(darabok)} darab, atlag {atlag / 1024:.0f} KB")
check("a halozati darabok nem aprozodnak el", atlag > 8 * 1024, f"{atlag / 1024:.0f} KB")
check("es nem is kell kezzel osszefuzni oket", len(darabok) < 500, f"{len(darabok)} darab")

client.close()
shutil.rmtree(TMP, ignore_errors=True)
srv.shutdown()
print("\n=== OSSZEGZES: %d / %d teszt sikeres ===" % (sum(R), len(R)))
sys.exit(0 if all(R) else 1)
