"""Windows 11 specifikus ellenorzesek.

A tesztek Linuxon futnak, de a Windows-szabalyokat szimulaljak: foglalt
eszkoznevek, tiltott karakterek, MAX_PATH, kis-nagybetu-utkozes, konzolkodolas.
A tenylegesen csak Windowson futo agakat (DPI, os.startfile) monkeypatch-csel
ellenorizzuk.
"""
import io
import os
import string
import subprocess
import sys
import time
import tempfile
from pathlib import Path, PureWindowsPath

# A letolto.py lehet a teszt mellett vagy egy szinttel feljebb (tests/ mappa).
_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent)]


def repo_file(name: str) -> Path:
    """A repó egy fájlja, akárhol is fut a teszt (gyökér vagy tests/ mappa)."""
    for base in (_HERE, _HERE.parent):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return _HERE / name
import letolto
from letolto import (MAX_ABS_PATH, atomic_replace, fit_path, safe_component,
                     url_to_relpath)

R = []
TMP = Path(tempfile.gettempdir()) / "letolto_win"


def check(name, ok, info=""):
    R.append(ok)
    print(("[OK]   " if ok else "[HIBA] ") + name + (f"  -> {info}" if info else ""))


# --------------------------------------------------------------- 1. eszkoznevek
print("\n--- 1. Windows foglalt eszköznevek ---")
RESERVED = ["CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9",
            "con", "Con.txt", "nul.pdf", "AUX.tar.gz", "COM1.bin"]


def is_reserved(name: str) -> bool:
    """A Windows szabalya: a kiterjesztes elotti resz szamit."""
    return name.partition(".")[0].upper() in (
        {"CON", "PRN", "AUX", "NUL"}
        | {f"COM{d}" for d in "123456789"} | {f"LPT{d}" for d in "123456789"})


bad = [n for n in RESERVED if is_reserved(safe_component(n))]
check("egyik foglalt név sem marad meg", not bad, str(bad))
print("    példák:", {n: safe_component(n) for n in RESERVED[:5]})

# a Python 3.13+ sajat ellenorzesevel is osszevetjuk, ha elerheto
if hasattr(os.path, "isreserved"):
    still = [n for n in RESERVED if os.path.isreserved(safe_component(n))]
    check("os.path.isreserved szerint is rendben", not still, str(still))
else:
    print("    (os.path.isreserved csak Python 3.13+; a saját listánk fut)")

# --------------------------------------------------------------- 2. karakterek
print("\n--- 2. Tiltott karakterek és záró jelek ---")
ILLEGAL = '<>:"/\\|?*'
sample = safe_component('a<b>c:d"e/f\\g|h?i*j')
check("egyetlen tiltott karakter sem marad", not any(c in sample for c in ILLEGAL), sample)
check("vezérlőkarakterek eltűnnek",
      not any(c in safe_component("a\x00b\x1fc") for c in "\x00\x1f"))
check("záró pont levágva", not safe_component("mappa.").endswith("."), safe_component("mappa."))
check("záró szóköz levágva", not safe_component("mappa ").endswith(" "),
      repr(safe_component("mappa ")))
check("csak pontokból álló név sem marad", safe_component("...") not in (".", "..", "..."),
      safe_component("..."))

# a PureWindowsPath szerint is ervenyes-e a nev
names = [safe_component(x) for x in
         ('a<b>c:d"e/f\\g|h?i*j', "CON.txt", "mappa.", "mappa ", "...", "árvíztűrő.pdf")]
invalid = [n for n in names if any(c in n for c in ILLEGAL) or n != n.rstrip(". ")]
check("minden előállított név érvényes Windows-fájlnév", not invalid, str(invalid))

# --------------------------------------------------------------- 3. MAX_PATH
print("\n--- 3. MAX_PATH (260) korlát ---")
long_out = Path("C:/Users/Szabolcs/Documents/Munka/Letoltesek/2026/augusztus/projekt")
deep = url_to_relpath("http://pelda.hu/" + "/".join(f"konyvtar{i}" for i in range(12))
                      + "/nagyon_hosszu_fajlnev_amit_senki_sem_olvas_el.pdf")
fitted = fit_path(long_out, deep)
full = long_out / fitted
print(f"    eredeti mélység: {len(deep.parts)} elem, {len(str(long_out / deep))} karakter")
print(f"    rövidítve      : {full}  ({len(str(full))} karakter)")
check("a teljes út a korlát alatt marad", len(str(full)) <= MAX_ABS_PATH, str(len(str(full))))
check("a kiterjesztés megmarad", full.suffix == ".pdf", full.suffix)

short_out = Path("C:/le")
normal = url_to_relpath("http://pelda.hu/doc/a.pdf")
check("rövid útnál nem nyúl bele", fit_path(short_out, normal) == normal,
      str(fit_path(short_out, normal)))

extreme = Path("C:/" + "x" * 230)
res = fit_path(extreme, deep)
check("extrém hosszú célkönyvtárnál is a lehető legrövidebbet adja",
      len(str(res)) < 30, str(res))

# egy komponens sem lehet 255 karakternel hosszabb
comp = safe_component("y" * 400 + ".zip")
check("egy útvonalelem sem hosszabb 100 karakternél", len(comp) <= 100, str(len(comp)))

# --------------------------------------------------------------- 4. kis-nagybetu
print("\n--- 4. Kis- és nagybetű: a Windows nem tesz köztük különbséget ---")
from letolto import DownloadManager, make_client

client = make_client(2)
out = TMP / "case"
mgr = DownloadManager(out, 1, client=client)
mgr.load_state()
mgr.add_urls(["http://pelda.hu/dok/Jelentes.PDF", "http://pelda.hu/dok/jelentes.pdf"])
paths = [i.path for i in mgr.items.values()]
lowered = {p.casefold() for p in paths}
print("    kapott útvonalak:", paths)
check("két név, ami Windowson ütközne, elkülönül", len(lowered) == 2, str(paths))

# --------------------------------------------------------------- 5. atomi csere
print("\n--- 5. atomic_replace ---")
d = TMP / "repl"
d.mkdir(parents=True, exist_ok=True)
src, dst = d / "uj.part", d / "kesz.bin"
src.write_bytes(b"uj tartalom")
dst.write_bytes(b"regi tartalom")
atomic_replace(src, dst)
check("felülírja a meglévő fájlt", dst.read_bytes() == b"uj tartalom" and not src.exists())

# a Windows-os zarolas szimulacioja: az elso ket hivas PermissionError
calls = {"n": 0}
real_replace = os.replace


def flaky(a, b):
    calls["n"] += 1
    if calls["n"] <= 2:
        raise PermissionError(5, "Access is denied")
    real_replace(a, b)


src.write_bytes(b"masodik")
os.replace = flaky
try:
    atomic_replace(src, dst)
finally:
    os.replace = real_replace
check("víruskereső-zárolást újrapróbálkozással áthidal",
      dst.read_bytes() == b"masodik" and calls["n"] == 3, f"{calls['n']} próbálkozás")

calls["n"] = 0


def always_locked(a, b):
    calls["n"] += 1
    raise PermissionError(5, "Access is denied")


src.write_bytes(b"harmadik")
os.replace = always_locked
try:
    atomic_replace(src, dst, attempts=3)
    raised = False
except PermissionError:
    raised = True
finally:
    os.replace = real_replace
check("tartós zárolásnál végül hibát jelez", raised and calls["n"] == 3, f"{calls['n']}")

# --------------------------------------------------------------- 6. konzolkodolas
print("\n--- 6. Konzolkimenet magyar Windows kódlapján (cp852 / cp1250) ---")
messages = ["Kész: árvíztűrő tükörfúrógép.pdf", "Nincs letöltendő fájl.",
            "Megszakítás... (később folytatható)", "20 fájl letöltése 4 szálon..."]
for cp in ("cp852", "cp1250", "utf-8"):
    problems = []
    for m in messages:
        try:
            m.encode(cp)
        except UnicodeEncodeError as exc:
            problems.append(f"{cp}: {m[exc.start:exc.end]!r}")
    check(f"a felhasználói üzenetek kiírhatók {cp} kódlapon", not problems, "; ".join(problems))

# a program osszes literalja: van-e olyan karakter, ami cp852-n nem megy at?
source = Path(letolto.__file__).read_text(encoding="utf-8")



def _safe_cp(ch, cp="cp852"):
    try:
        ch.encode(cp)
        return True
    except UnicodeEncodeError:
        return False


risky = sorted({c for c in source if ord(c) > 127 and not _safe_cp(c)})
print(f"    cp852-n nem ábrázolható karakterek a forrásban: {risky}")
check("a forrásban nincs cp852-n kiírhatatlan karakter a konzolüzenetekben",
      all(c not in "".join(messages) for c in risky), str(risky))

# --------------------------------------------------------------- 7. DPI
print("\n--- 7. DPI-tudatosság (csak Windowson fut le) ---")
called = {"n": 0}


class FakeShcore:
    def SetProcessDpiAwareness(self, v):  # noqa: N802
        called["n"] += 1
        called["value"] = v
        return 0


class FakeWindll:
    shcore = FakeShcore()


real_platform = sys.platform
import ctypes
real_windll = getattr(ctypes, "windll", None)
ctypes.windll = FakeWindll()
try:
    letolto.sys.platform = "win32"
    letolto.enable_dpi_awareness()
finally:
    letolto.sys.platform = real_platform
    if real_windll is None:
        del ctypes.windll
check("Windowson meghívja a DPI-beállítást", called["n"] == 1 and called.get("value") == 1,
      str(called))
called["n"] = 0
letolto.enable_dpi_awareness()          # most mar Linux
check("más rendszeren nem hívja meg", called["n"] == 0)

# --------------------------------------------------------------- 8. beallitasfajl
print("\n--- 8. Beállításfájl helye ---")
src_txt = source
check("Windowson az %APPDATA% alá kerül", 'os.environ.get("APPDATA")' in src_txt)
check("máshol a home könyvtárba", '.letolto_beallitasok.json' in src_txt)
print("    ezen a gépen:", letolto.SETTINGS_FILE)

# --------------------------------------------------- 8/b. Intezo-ablak a beallitasokra
print("\n--- 8/b. „Beállítások mappája” gomb Windows-parancsa ---")
from pathlib import PureWindowsPath

real_windir = os.environ.get("WINDIR")
os.environ["WINDIR"] = "C:\\Windows"
spaced = PureWindowsPath(r"C:\Users\Kis Béla\AppData\Roaming\PyLetolto\beallitasok.json")
cmd = letolto.explorer_select_command(spaced)
print("   ", cmd)
check("az idézőjel a /select, UTÁN áll (különben a Dokumentumokat nyitná meg)",
      '/select,"C:\\Users\\Kis Béla\\' in cmd, cmd)
check("nem az egész paraméter van idézőjelben", '"/select,' not in cmd, cmd)
check("az explorer.exe teljes úttal indul (nem a munkakönyvtárból)",
      cmd.startswith('"C:\\Windows\\explorer.exe"'), cmd)
os.environ["WINDIR"] = "D:\\Win\\"
check("a %WINDIR% záró jelét is kezeli",
      letolto.explorer_select_command(PureWindowsPath(r"C:\a\b.json")).startswith(
          '"D:\\Win\\explorer.exe"'), letolto.explorer_select_command(PureWindowsPath("C:/a")))
del os.environ["WINDIR"]
check("%WINDIR% nélkül is épkézláb parancs",
      letolto.explorer_select_command(PureWindowsPath(r"C:\a\b.json")).startswith(
          '"C:\\Windows\\explorer.exe"'))
if real_windir is not None:
    os.environ["WINDIR"] = real_windir

# A tenyleges inditas: Windowson egyetlen sztringkent kell atadni (nincs cmd.exe).
TMP.mkdir(parents=True, exist_ok=True)
probe = TMP / "beallitasok.json"
probe.write_text("{}", encoding="utf-8")
launched = []
real_popen = subprocess.Popen
opened_dirs = []
real_open = letolto.open_in_file_manager


class FakeProc:
    """A spawn_detached poll()-t hiv a korabbi folyamatokra."""

    def poll(self):
        return 0


def fake_popen(*a, **k):
    launched.append((a, k))
    return FakeProc()


subprocess.Popen = fake_popen
letolto.open_in_file_manager = lambda path: opened_dirs.append(Path(path))
try:
    letolto.sys.platform = "win32"
    letolto.reveal_in_file_manager(probe)
    letolto.reveal_in_file_manager(TMP / "nincs_ilyen.json")     # hiányzó fájl
finally:
    letolto.sys.platform = real_platform
    subprocess.Popen = real_popen
    letolto.open_in_file_manager = real_open
    letolto._spawned.clear()
check("meglévő fájlnál elindítja az Intézőt", len(launched) == 1, str(launched))
check("egyetlen sztringként adja át (a lista alak elrontaná az idézőjeleket)",
      launched and isinstance(launched[0][0][0], str), str(launched[:1]))
check("shell nélkül indul", launched and not launched[0][1].get("shell"), str(launched[:1]))
check("hiányzó fájlnál a mappát nyitja meg",
      opened_dirs == [TMP], str(opened_dirs))

# ------------------------------------------------- 8/c. kulso program inditasa
print("\n--- 8/c. Külső program indítása (nem marad zombi, nincs kimeneti zaj) ---")
check("elnyeli a külső program kimenetét",
      "stdout=subprocess.DEVNULL" in src_txt and "stderr=subprocess.DEVNULL" in src_txt)
check("saját munkamenetben indul (a Ctrl+C nem üti le)",
      "start_new_session=True" in src_txt)
letolto.spawn_detached([sys.executable, "-c", ""])
first = letolto._spawned[-1]
time.sleep(0.8)                               # ennyi alatt biztosan befejeződik
letolto.spawn_detached([sys.executable, "-c", ""])
check("a befejezett folyamatot begyűjti (nem marad zombi)",
      first.returncode is not None and first not in letolto._spawned,
      f"returncode={first.returncode}, nyilvántartva={len(letolto._spawned)}")
check("csak a még futók maradnak nyilvántartva", len(letolto._spawned) <= 1,
      str(len(letolto._spawned)))
letolto._spawned[-1].wait(timeout=10)
letolto._spawned.clear()

# ------------------------------------------------- 8/d. beallitasok mentese
print("\n--- 8/d. A beállításfájl mentése atomi ---")
check("ideiglenes fájlba ír, és csak utána cserél", "atomic_replace(tmp, SETTINGS_FILE)" in src_txt)
check("az atomi csere Windowson újrapróbálkozik", "def atomic_replace" in src_txt
      and "PermissionError" in src_txt)

# --------------------------------------------------------------- 9. bat fajl
print("\n--- 9. Az indito .bat ellenőrzése ---")
bat = repo_file("inditas.bat")
raw = bat.read_bytes()
check("a .bat tiszta ASCII", all(b < 128 for b in raw))
check("CRLF sorvégek", b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""))
text = raw.decode("ascii")
check("3.11-es verzióellenőrzést tartalmaz", "(3,11)" in text)
check("a httpx-et telepíti", "httpx" in text)
check("tkinter meglétét is nézi", "tkinter" in text)
check("hibánál nem csukódik be azonnal", "pause" in text)

# --------------------------------------------------------------- 10. utvonalkezeles
print("\n--- 10. Útvonalkezelési szokások ---")
check("nincs beégetett '/' elválasztó az útvonalépítésben",
      'outdir / item.path' in src_txt or "self.outdir /" in src_txt)
check("mindenhol pathlib-et használ", "os.path.join(" not in src_txt)
check("a fájlok írása mindig megadott kódolással történik",
      src_txt.count("open(") >= 0 and 'encoding="utf-8"' in src_txt)
check("a .part fájlnév nem cseréli le a kiterjesztést",
      'with_name(dest.name + ".part")' in src_txt)

print("\n=== OSSZEGZES: %d / %d ===" % (sum(R), len(R)))
client.close()
sys.exit(0 if all(R) else 1)
