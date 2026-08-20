"""A konyveles (osszesito), a szalbiztonsag es az allapotfajl tesztje.

Ezek a felulvizsgalat soran talalt hibakat rogzitik:

* a lista bejarasa kozben egy masik szal bovitett -> "dictionary changed size
  during iteration" (a recount() es a pending() elszallt);
* az osszesito a mark_intact() utan elcsuszott a valosagtol;
* az "Atvizsgalas megszakitasa" nem allitotta le az epseg-ellenorzest;
* a Retry-After varakozasa utan a program megvarta a sajat szunetet is;
* minden fajl elejen volt egy folosleges fsync;
* az allapotfajlbol jovo ismeretlen statusz valtozatlanul bekerult a listaba.
"""
import json
import random
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

# A letolto.py lehet a teszt mellett vagy egy szinttel feljebb (tests/ mappa).
_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent)]
import letolto
import testsrv
from letolto import (DownloadManager, Item, RobotsRules, Status, Totals, disk_size,
                     make_client)

srv = testsrv.serve(8815)
BASE = "http://127.0.0.1:8815/"
TMP = Path(tempfile.gettempdir()) / "letolto_konyveles"
client = make_client(4)
R = []


def check(name, ok, info=""):
    R.append(bool(ok))
    print(("[OK]   " if ok else "[HIBA] ") + name + (f"  -> {info}" if info else ""))


def fresh(name):
    d = TMP / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


def egyezik(mgr, mit):
    """A karbantartott osszesito megegyezik-e a teljes ujraszamolassal?"""
    elotte = Totals(**{m: getattr(mgr.totals, m) for m in Totals.__slots__})
    mgr.recount()
    check(f"a konyveles helyes {mit} utan", elotte == mgr.totals,
          f"karbantartott={elotte}, ujraszamolt={mgr.totals}")


# ------------------------------------------------------------------ 1.
print("--- 1. Az osszesito karbantartasa ---")
OUT = fresh("k1")
mgr = DownloadManager(OUT, 4, client=client)
urls = [BASE + f"files/{n}" for n in ("a.bin", "b.bin", "d.pdf")]
mgr.add_urls(urls[:2])
egyezik(mgr, "add_urls (kipipalva)")
mgr.add_urls(urls[2:], selected=False)
egyezik(mgr, "add_urls (pipa nelkul)")
check("csak a kipipaltak szamitanak", mgr.totals.files == 2, str(mgr.totals.files))

mgr.set_selected([urls[2]], True)
egyezik(mgr, "set_selected(True)")
mgr.set_selected([urls[2]], False)
egyezik(mgr, "set_selected(False)")
mgr.set_selected([urls[2], urls[2]], False)      # ismetles nem szamolhat ketszer
egyezik(mgr, "ismetelt set_selected")

items = {i.url: i for i in mgr.snapshot()}
a = items[urls[0]]
mgr._set_total(a, 300_000)
mgr._set_done(a, 1234)
egyezik(mgr, "_set_total / _set_done")

(OUT / a.path).parent.mkdir(parents=True, exist_ok=True)
(OUT / a.path).write_bytes(testsrv.FILES["/files/a.bin"])
mgr.mark_intact(a)
egyezik(mgr, "mark_intact")
check("az ep fajlrol lekerul a pipa", a.status == Status.DONE and not a.selected)
mgr.mark_for_redownload(a)
egyezik(mgr, "mark_for_redownload")
mgr.mark_broken(a)
egyezik(mgr, "mark_broken")

mgr.start()
mgr._worker.join(60)
egyezik(mgr, "a letoltes befejezese")
check("a kesz fajlok szama stimmel", mgr.totals.done_files == mgr.totals.files,
      f"{mgr.totals.done_files}/{mgr.totals.files}")

print("\n--- 2. A kijelolesvaltas nem jarja vegig a listat ---")
OUT = fresh("k2")
nagy = DownloadManager(OUT, 4, client=client)
nagy.store.save = lambda *a, **k: None            # most a lemez nem erdekes
nagy.add_urls([f"http://pelda.hu/x/{i}.bin" for i in range(20000)])
t = time.perf_counter()
for i in range(200):
    nagy.set_selected([f"http://pelda.hu/x/{i}.bin"], i % 2 == 0)
dt = (time.perf_counter() - t) * 1000
print(f"    200 kijelolesvaltas 20 000 elem mellett: {dt:.1f} ms")
check("a valtas fuggetlen a lista meretetol", dt < 100, f"{dt:.1f} ms")
egyezik(nagy, "200 kijelolesvaltas")

# ------------------------------------------------------------------ 3.
print("\n--- 3. Bovites es bejaras egyszerre (verseny) ---")
hibak = []


def bovit():
    for i in range(20000, 26000):
        try:
            nagy.add_urls([f"http://pelda.hu/x/{i}.bin"])
        except Exception as exc:                  # noqa: BLE001 - epp ezt keressuk
            hibak.append(f"add_urls: {exc!r}")
            return


def bejar(nev, fv):
    for _ in range(4000):
        try:
            fv()
        except Exception as exc:                  # noqa: BLE001
            hibak.append(f"{nev}: {exc!r}")
            return


szalak = [threading.Thread(target=bovit),
          threading.Thread(target=bejar, args=("recount", nagy.recount)),
          threading.Thread(target=bejar, args=("pending", nagy.pending)),
          threading.Thread(target=bejar, args=("snapshot", nagy.snapshot))]
for s in szalak:
    s.start()
for s in szalak:
    s.join()
check("a parhuzamos bejaras nem szall el", not hibak, "; ".join(hibak[:3]))

# ------------------------------------------------------------------ 4.
print("\n--- 4. Az epseg-ellenorzes megszakithato ---")
OUT = fresh("k4")
mgr = DownloadManager(OUT, 4, client=client)
mgr.add_urls([BASE + f"files/{n}" for n in ("a.bin", "b.bin", "d.pdf", "g.bin")])
for item in mgr.snapshot():                       # mindegyik legyen a lemezen, csonkan
    (OUT / item.path).parent.mkdir(parents=True, exist_ok=True)
    (OUT / item.path).write_bytes(b"x" * 10)
stop = threading.Event()
stop.set()
kerdesek = []
eredeti_head = mgr._remote_size
mgr._remote_size = lambda url: (kerdesek.append(url), eredeti_head(url))[1]
ep = mgr.classify_existing(mgr.snapshot(), stop=stop)
check("megszakitva egy kerdes sem megy ki", not kerdesek, str(len(kerdesek)))
check("es egy fajlt sem mond epnek", ep == 0, str(ep))
ep = mgr.classify_existing(mgr.snapshot(), stop=threading.Event())
check("megszakitas nelkul viszont dolgozik", len(kerdesek) == 4, str(len(kerdesek)))
mgr._remote_size = eredeti_head

# ------------------------------------------------------------------ 5.
print("\n--- 5. Allapotfajl: hibas es idegen tartalom ---")
OUT = fresh("k5")
mgr = DownloadManager(OUT, 2, client=client)
(OUT / letolto.STATE_FILE).write_text(json.dumps({
    "version": 99,                                # ujabb formatum
    "items": {
        "http://a/1.bin": {"url": "http://a/1.bin", "path": "a/1.bin",
                           "status": "kacsa", "total": -5, "done": "3"},
        "http://a/2.bin": {"url": "http://a/2.bin", "path": "b\\c\\2.bin",
                           "status": "kész", "total": 10, "done": 10},
        "http://a/3.bin": {"nincs": "url"},       # hibas bejegyzes
    }}), encoding="utf-8")
mgr.load_state()
elemek = {i.url: i for i in mgr.snapshot()}
check("a hibas bejegyzes kimarad", len(elemek) == 2, str(len(elemek)))
check("az ismeretlen statusz varakozo lesz",
      elemek["http://a/1.bin"].status == Status.PENDING, elemek["http://a/1.bin"].status)
check("a negativ meret nulla lesz", elemek["http://a/1.bin"].total == 0)
check("a windowsos elvalaszto atirodik", elemek["http://a/2.bin"].path == "b/c/2.bin",
      elemek["http://a/2.bin"].path)

print("\n--- 6. Mentes es visszatoltes (ekezet, ures lista) ---")
OUT = fresh("k6")
mgr = DownloadManager(OUT, 2, client=client)
mgr.add_urls(["http://pelda.hu/á ő/adat, vessző.bin", "http://pelda.hu/b.bin"])
for i, item in enumerate(mgr.snapshot()):
    item.total, item.done, item.validator = 100 + i, i, f'W/"jel{i}"'
mgr.store.save(mgr.items, force=True)
nyers = json.loads((OUT / letolto.STATE_FILE).read_text(encoding="utf-8"))
check("a mentes ervenyes JSON, a verzioval", nyers["version"] == letolto.STATE_VERSION)
check("minden elem bekerult", len(nyers["items"]) == 2, str(len(nyers["items"])))
masik = DownloadManager(OUT, 2, client=client)
masik.load_state()
check("a visszatoltott adat azonos",
      {i.url: (i.total, i.validator) for i in masik.snapshot()}
      == {i.url: (i.total, i.validator) for i in mgr.snapshot()})
ures = DownloadManager(fresh("k6b"), 2, client=client)
ures.store.save({}, force=True)
check("az ures lista is ervenyes JSON",
      json.loads(ures.store.path.read_text(encoding="utf-8"))["items"] == {})

# ------------------------------------------------------------------ 7.
print("\n--- 7. disk_size: egyetlen kerdes a lemezhez ---")
p = OUT / "proba.bin"
check("hianyzo fajlra None", disk_size(p) is None)
p.write_bytes(b"12345")
check("meglevo fajlra a meret", disk_size(p) == 5, str(disk_size(p)))
check("konyvtarra sem hibazik el", disk_size(OUT) is not None)

# ------------------------------------------------------------------ 8.
print("\n--- 8. Retry-After: egyszer varunk, nem ketszer ---")


class Valasz:
    def __init__(self, status, fejlec):
        self.status_code = status
        self.headers = fejlec
        self.reason_phrase = ""


mgr = DownloadManager(fresh("k8"), 2, client=client)
altunk = []
mgr._sleep = lambda mp: altunk.append(mp)
item = Item(url="http://a/1.bin", path="1.bin")
try:
    mgr._check_status(Valasz(503, {"retry-after": "12"}), item, OUT / "x", OUT / "x.part", 0)
except letolto.Retryable as exc:
    check("a kert varakozas a kivetelben jon", exc.wait == 12, str(exc.wait))
check("es kozben nem alszik el a szal", not altunk, str(altunk))
try:
    mgr._check_status(Valasz(503, {"retry-after": "9999"}), item, OUT / "x", OUT / "x.part", 0)
except letolto.Retryable as exc:
    check("a tulzo keres korlatozva van", exc.wait == letolto.MAX_RETRY_AFTER, str(exc.wait))
try:
    mgr._check_status(Valasz(500, {}), item, OUT / "x", OUT / "x.part", 0)
except letolto.Retryable as exc:
    check("fejlec nelkul a sajat szunet dont", exc.wait is None, str(exc.wait))

# ------------------------------------------------------------------ 9.
print("\n--- 9. Nincs folosleges fsync a fajl elejen ---")
OUT = fresh("k9")
mgr = DownloadManager(OUT, 2, client=client)
mgr.add_urls([BASE + "files/a.bin"])
fsyncek = []
eredeti_fsync = letolto.os.fsync
letolto.os.fsync = lambda fd: (fsyncek.append(fd), eredeti_fsync(fd))[1]
mgr.start()
mgr._worker.join(60)
letolto.os.fsync = eredeti_fsync
check("a gyors fajl egy fsync nelkul is elkeszul", not fsyncek, str(len(fsyncek)))
check("a fajl viszont ep",
      (OUT / mgr.snapshot()[0].path).read_bytes() == testsrv.FILES["/files/a.bin"])

# ------------------------------------------------------------------ 10.
print("\n--- 10. robots.txt: a rendezes ugyanazt donti, mint a vegigjaras ---")


def referencia(rules, url):
    """A korabbi, minden szabalyt vegignezo dontes - ezzel vetjuk ossze."""
    from urllib.parse import urlparse, unquote
    parts = urlparse(url)
    path = unquote(parts.path or "/")
    if parts.query:
        path += "?" + unquote(parts.query)
    best = None
    for rule in rules._rules:
        if rule.pattern.match(path) and (best is None or rule.length > best.length
                                         or (rule.length == best.length and rule.allow)):
            best = rule
    return best is None or best.allow


rnd = random.Random(20260820)
darabok = ["", "a", "b", "kep", "admin", "*", "titok", "2024"]
elteres = 0
for _ in range(60):
    sorok = ["User-agent: *"]
    for _ in range(rnd.randrange(1, 12)):
        ut = "/" + "/".join(rnd.choice(darabok) for _ in range(rnd.randrange(1, 4)))
        sorok.append(f"{rnd.choice(('Allow', 'Disallow'))}: {ut}{rnd.choice(('', '$', '*'))}")
    rules = RobotsRules.parse("\n".join(sorok))
    for _ in range(40):
        url = "http://pelda.hu/" + "/".join(rnd.choice(darabok) for _ in range(rnd.randrange(1, 4)))
        if rules.allowed(url) != referencia(rules, url):
            elteres += 1
check("2400 veletlen dontes egyezik a referenciaval", elteres == 0, str(elteres))

# ------------------------------------------------------------------ 11.
print("\n--- 11. A bejaras nem gyujti be ketszer ugyanazt a cimet ---")


class Lap:
    """HTML-valasz helyettesitese: minden lapon ugyanaz a menu."""

    def __init__(self, url, html):
        self.status_code = 200
        self.headers = {"content-type": "text/html"}
        self.url = url
        self._html = html
        self.num_bytes_downloaded = 0

    def iter_text(self, size=8192):
        for i in range(0, len(self._html), size):
            darab = self._html[i:i + size]
            self.num_bytes_downloaded += len(darab.encode())
            yield darab

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class MenusKiszolgalo:
    def stream(self, method, url, timeout=None):
        if url.endswith("/robots.txt"):
            valasz = Lap(url, "User-agent: *\nDisallow: /tiltott/\n")
            valasz.headers = {"content-type": "text/plain"}
            return valasz
        menu = "".join(f'<a href="/lap{i}.html">l{i}</a>' for i in range(20))
        return Lap(url, f"<html><body>{menu}<a href='{url}#tetejere'>ugyanez</a>"
                        f"<a href='/kozos.pdf'>kozos</a></body></html>")


cfg = letolto.ScanConfig(root="http://pelda.hu/#nyito", depth=2, same_host=True,
                         respect_robots=True, max_pages=25)
sc = letolto.Scanner(cfg, MenusKiszolgalo(), lambda m: None, threading.Event())
dontesek = []
eredeti_allowed = sc._allowed
sc._allowed = lambda u: (dontesek.append(u), eredeti_allowed(u))[1]
eredmeny = sc.run()
# Cimenkent legfeljebb ket robots-dontes szuletik: egy a sorba tetelnel, egy a
# megnyitasnal. A duplikatumok kiszurese nelkul ez a lapok szamaval szorzodott
# (21 lap x 21 hivatkozas = 441 dontes 22 cimre).
egyedi = len(set(dontesek))
check("a menu nem sokszorozza a robots-donteseket", len(dontesek) <= 2 * egyedi,
      f"{len(dontesek)} dontes {egyedi} kulonbozo cimre")
check("a kozos fajl egyszer szerepel", eredmeny.files.count("http://pelda.hu/kozos.pdf") == 1,
      str(eredmeny.files))
check("a horgony nem kepez uj lapot", len(eredmeny.pages) == len(set(eredmeny.pages)),
      f"{len(eredmeny.pages)} lap")
check("a kezdolap horgony nelkul kerul be",
      eredmeny.pages[0] == "http://pelda.hu/", eredmeny.pages[0])

print("\n=== OSSZEGZES: %d / %d ===" % (sum(R), len(R)))
client.close()
srv.shutdown()
sys.exit(0 if all(R) else 1)
