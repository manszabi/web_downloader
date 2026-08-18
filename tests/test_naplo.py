"""A rotalo naplofajl tesztje."""
import shutil
import sys
import tempfile
import threading
from pathlib import Path

# A letolto.py lehet a teszt mellett vagy egy szinttel feljebb (tests/ mappa).
_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent)]
import letolto
from letolto import DownloadManager, ScanConfig, Scanner, note, setup_file_log

TMP = Path(tempfile.gettempdir()) / "letolto_naplo_teszt"
R = []


def check(name, ok, info=""):
    R.append(ok)
    print(("[OK]   " if ok else "[HIBA] ") + name + (f"  -> {info}" if info else ""))


def fresh_log(name="naplo.log"):
    """Uj, ures naplokornyezet: leakasztjuk a korabbi kezelot es torlunk mindent."""
    for handler in list(letolto.file_log.handlers):
        handler.close()
        letolto.file_log.removeHandler(handler)
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True)
    return setup_file_log(TMP / name)


def read(path):
    return path.read_text(encoding="utf-8")


print("\n--- 1. Alapmukodes ---")
path = fresh_log()
check("a beallitas visszaadja a fajl helyet", path == TMP / "naplo.log", str(path))
check("a fajl meg nem jott letre (delay)", not path.exists())
note("Első sor, ékezetekkel: árvíztűrő tükörfúrógép")
check("az elso sor utan mar letezik", path.exists())
sor = read(path).strip()
check("a szoveg ekezethelyesen kerult bele", "árvíztűrő tükörfúrógép" in sor, sor)
check("van benne idobelyeg", sor[:4].isdigit() and sor[4] == "-", sor[:20])
check("van benne szalnev", "[MainThread]" in sor, sor)

print("\n--- 2. Ketszeri bekapcsolas ---")
again = setup_file_log(TMP / "masik.log")
check("nem akaszt masodik kezelot", len(letolto.file_log.handlers) == 1)
check("es a meglevo fajlt adja vissza", again == TMP / "naplo.log", str(again))

print("\n--- 3. Rotalas: fajlnevek es darabszam ---")
path = fresh_log()
letolto.file_log.handlers[0].maxBytes = 2000       # gyors rotalashoz
for i in range(400):
    note(f"{i:04d} sor a rotalas kiprobalasahoz")
nevek = sorted(f.name for f in TMP.iterdir())
check("az elo naplo a sajat neven van", "naplo.log" in nevek, str(nevek))
check("a mentesek szamozottak", [n for n in nevek if n.startswith("naplo.log.")]
      == ["naplo.log.1", "naplo.log.2", "naplo.log.3"], str(nevek))
check("nincs tobb mentes a beallitottnal", len(nevek) == letolto.LOG_BACKUPS + 1, str(nevek))
check("mas fajl nem keletkezett", all(n.startswith("naplo.log") for n in nevek), str(nevek))
check("a .1 frissebb, mint a .2",
      read(TMP / "naplo.log.1").splitlines()[0] > read(TMP / "naplo.log.2").splitlines()[0])
check("a legutolso sor az elo naploban van", "0399 sor" in read(TMP / "naplo.log"))
check("egyik darab sem lepi tul jelentosen a hatart",
      all(f.stat().st_size < 2500 for f in TMP.iterdir()),
      str({f.name: f.stat().st_size for f in TMP.iterdir()}))

print("\n--- 4. Sikertelen rotalas (Windowson zarolt fajl) ---")
path = fresh_log()
handler = letolto.file_log.handlers[0]
handler.maxBytes = 500
eredeti = letolto.atomic_replace
probalkozas = []


def zarolt(src, dst, attempts=1):
    probalkozas.append(dst.name)
    raise PermissionError("a fajlt egy masik folyamat hasznalja")


letolto.atomic_replace = zarolt
try:
    for i in range(200):
        note(f"{i:04d} sor zarolt naploval")
finally:
    letolto.atomic_replace = eredeti
check("a zarolt rotalas nem allitja meg a naplozast", "0199 sor" in read(path))
check("nem keletkezett csonka mentes", [f.name for f in TMP.iterdir()] == ["naplo.log"],
      str([f.name for f in TMP.iterdir()]))
check("a sikertelen rotalast nem probalja minden sornal", len(probalkozas) == 1,
      f"{len(probalkozas)} probalkozas")
check("a naplo tovabb no a regi fajlban", path.stat().st_size > 500)

print("\n--- 5. Nem irhato hely ---")
for h in list(letolto.file_log.handlers):
    h.close()
    letolto.file_log.removeHandler(h)
utkozo = TMP / "fajl_nem_mappa"
utkozo.write_text("x", encoding="utf-8")
check("nem irhato helyen None a valasz, kivetel nelkul",
      setup_file_log(utkozo / "melyebb" / "naplo.log") is None)
check("naplo nelkul is mukodik a note()", note("ez sehova nem kerul") is None)

print("\n--- 6. Mit ir bele a program ---")
path = fresh_log()


class FakeResponse:
    def __init__(self, status_code=404, text=""):
        self.status_code, self.text, self.headers = status_code, text, {}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeClient:
    def get(self, url, timeout=None):
        return FakeResponse(404, "")

    def stream(self, method, url, timeout=None):
        return FakeResponse(404, "")


cfg = ScanConfig(root="https://pelda.hu/", depth=1, same_host=True, respect_robots=True)
Scanner(cfg, FakeClient(), lambda _m: None, threading.Event()).run()
DownloadManager(TMP / "cel", threads=2)
szoveg = read(path)
check("benne van, mikor es mit vizsgalunk", "Átvizsgálás indul: https://pelda.hu/" in szoveg)
check("benne vannak az atvizsgalas beallitasai", "robots.txt: betartva" in szoveg)
check("benne van az atvizsgalas eredmenye", "Átvizsgálás vége:" in szoveg, szoveg.strip()[-90:])
check("benne van a celkonyvtar", "Célkönyvtár:" in szoveg and "2 szál" in szoveg)

for h in list(letolto.file_log.handlers):
    h.close()
    letolto.file_log.removeHandler(h)
shutil.rmtree(TMP, ignore_errors=True)

print("\n=== OSSZEGZES: %d / %d teszt sikeres ===" % (sum(R), len(R)))
sys.exit(0 if all(R) else 1)
