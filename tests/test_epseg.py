"""Automatikus pipalas epseg szerint + felulirasi kerdes (Igen/Nem/Osszes)."""
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import testsrv
import letolto
from letolto import DownloadManager, ScanConfig, Scanner, Status, make_client

srv = testsrv.serve(8812)
BASE = "http://127.0.0.1:8812/"
TMP = Path(tempfile.gettempdir()) / "letolto_epseg"
client = make_client(8)
R = []


def check(name, ok, info=""):
    R.append(ok)
    print(("[OK]   " if ok else "[HIBA] ") + name + (f"  -> {info}" if info else ""))


def fresh(name):
    d = TMP / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


def put(outdir, rel, data):
    p = outdir / "127.0.0.1_8812" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


# ---------------------------------------------------------------- mag
print("\n--- 1. is_intact: ép / csonka / hiányzó ---")
OUT = fresh("i_a")
mgr = DownloadManager(OUT, 4, client=client)
mgr.load_state()
mgr.add_urls([BASE + "files/a.bin", BASE + "files/b.bin", BASE + "files/d.pdf"])
put(OUT, "files/a.bin", testsrv.FILES["/files/a.bin"])          # ép
put(OUT, "files/b.bin", testsrv.FILES["/files/b.bin"][:1000])   # csonka
items = list(mgr.items.values())
by_url = {i.url: i for i in items}
check("ép fájlra igaz", mgr.is_intact(by_url[BASE + "files/a.bin"]))
check("csonka fájlra hamis", not mgr.is_intact(by_url[BASE + "files/b.bin"]))
check("hiányzó fájlra hamis", not mgr.is_intact(by_url[BASE + "files/d.pdf"]))

print("\n--- 2. classify_existing ---")
seen = {}
intact = mgr.classify_existing(items, lambda i, ok: seen.__setitem__(i.url, ok))
check("csak a lemezen lévőket vizsgálja", len(seen) == 2, f"{len(seen)} db")
check("egy ép fájlt talált", intact == 1, str(intact))
check("a hiányzót meg sem kérdezte", BASE + "files/d.pdf" not in seen)

print("\n--- 3. mark_intact / mark_for_redownload ---")
ep = by_url[BASE + "files/a.bin"]
mgr.mark_intact(ep)
check("ép fájl: kész és nincs kipipálva", ep.status == Status.DONE and not ep.selected)
check("ilyenkor nem is töltendő", ep not in mgr.pending())
mgr.mark_for_redownload(ep)
check("felülírásra jelölve: kipipált és várakozik",
      ep.selected and ep.status == Status.PENDING and ep.force)
check("így már töltendő", ep in mgr.pending())
mgr.start()
mgr._worker.join(60)
p = OUT / ep.path
check("a felülírás tényleg megtörtént", p.read_bytes() == testsrv.FILES["/files/a.bin"])
check("a kényszerítés a letöltés után elévül", not ep.force)
check("a csonka fájl is javult",
      (OUT / by_url[BASE + "files/b.bin"].path).read_bytes() == testsrv.FILES["/files/b.bin"])

# ---------------------------------------------------------------- GUI
import tkinter as tk

cap = {}
orig = tk.Tk.mainloop
tk.Tk.mainloop = lambda self: cap.setdefault("app", self)
letolto.run_gui()
app = cap["app"]
tk.Tk.mainloop = orig


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.02)


print("\n--- 4. GUI: átvizsgálás után automatikus pipálás ---")
OUT = fresh("i_b")
put(OUT, "files/a.bin", testsrv.FILES["/files/a.bin"])          # ép -> pipa le
put(OUT, "files/b.bin", testsrv.FILES["/files/b.bin"][:1000])   # csonka -> pipa fel
app.v_url.set(BASE)
app.v_dir.set(str(OUT))
app.v_ext.set("")
app.v_html.set(False)
app.v_depth.set(0)
app._scan()
pump(10)
items = {i.url: i for i in app.manager.items.values()}
a = items[BASE + "files/a.bin"]
b = items[BASE + "files/b.bin"]
d = items[BASE + "files/d.pdf"]
print("    a.bin:", a.status, a.selected, "| b.bin:", b.status, b.selected,
      "| d.pdf:", d.status, d.selected)
check("ép fájlról lekerült a pipa", not a.selected and a.status == Status.DONE)
check("csonka fájl ki van pipálva", b.selected and b.status != Status.DONE)
check("hiányzó fájl ki van pipálva", d.selected)
check("a táblázat is ezt mutatja",
      app.tree.item(a.url)["values"][0] == letolto.UNCHECKED_MARK
      and app.tree.item(b.url)["values"][0] == letolto.CHECKED_MARK)
check("a naplóban is megjelenik", "megvan épen" in app.log.get("1.0", "end"))

print("\n--- 5. GUI: felülírási kérdés - NEM ---")
app._toggle_files([a.url])            # a felhasználó kézzel kipipálja az ép fájlt
pump(0.3)
check("kézzel újra kipipálható", a.selected)
answers = iter(["nem"])


class FakeDialog:
    """A párbeszéd helyettesítése, hogy a válaszokat vezérelni tudjuk."""

    calls = []

    def __init__(self, parent, path, remaining):
        self.result = next(answers)
        FakeDialog.calls.append((path, remaining, self.result))


saved = app.overwrite_dialog
app.overwrite_dialog = FakeDialog

app._confirm_overwrites(app.manager)
pump(0.3)
check("a 'Nem' válasz leveszi a pipát", not a.selected)
check("egy kérdés hangzott el", len(FakeDialog.calls) == 1, str(FakeDialog.calls))

print("\n--- 6. GUI: felülírási kérdés - IGEN ---")
FakeDialog.calls.clear()
answers = iter(["igen"])
app._toggle_files([a.url])
pump(0.2)
app._confirm_overwrites(app.manager)
check("az 'Igen' felülírásra jelöli", a.selected and a.force and a.status == Status.PENDING)

print("\n--- 7. GUI: felülírási kérdés - ÖSSZES ---")
# hozzunk letre tobb ep, kesz fajlt
OUT = fresh("i_c")
for name in ("a.bin", "b.bin", "d.pdf"):
    put(OUT, f"files/{name}", testsrv.FILES[f"/files/{name}"])
app.v_dir.set(str(OUT))
app.v_depth.set(0)
app._scan()
pump(10)
kesz = [i for i in app.manager.items.values() if i.status == Status.DONE]
print(f"    {len(kesz)} ép fájl a mappában")
check("mind ép, egyik sincs kipipálva", len(kesz) >= 3 and not any(i.selected for i in kesz))
app._set_all_files(True)              # a felhasznalo mindent kipipal
pump(0.3)
FakeDialog.calls.clear()
answers = iter(["osszes"] + ["nem"] * 20)
app._confirm_overwrites(app.manager)
pump(0.3)
print("    kérdések száma:", len(FakeDialog.calls))
check("csak EGYSZER kérdez", len(FakeDialog.calls) == 1, str(len(FakeDialog.calls)))
check("mégis mindegyik felülírásra jelölt",
      all(i.force and i.selected for i in kesz), str([(i.path, i.force) for i in kesz]))

print("\n--- 8. a felülírás végig is megy ---")
app._start()
for _ in range(400):
    app.update()
    time.sleep(0.02)
    if app.manager and not app.manager.running:
        break
ok = all((OUT / i.path).exists() for i in kesz)
check("minden fájl a helyén", ok)
check("mind kész", all(i.status == Status.DONE for i in kesz))
check("a kényszerítés elévült", not any(i.force for i in kesz))

app.overwrite_dialog = saved
app._on_close()
print("\n=== OSSZEGZES: %d / %d ===" % (sum(R), len(R)))
client.close()
srv.shutdown()
sys.exit(0 if all(R) else 1)
