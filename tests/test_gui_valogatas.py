"""GUI: kiterjesztes-panel, pipa-oszlop, kijelolo gombok - valodi ablakon."""
import shutil
import sys
import tempfile
import time
from pathlib import Path

# A letolto.py lehet a teszt mellett vagy egy szinttel feljebb (tests/ mappa).
_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent)]
import testsrv
import letolto

srv = testsrv.serve(8806)
BASE = "http://127.0.0.1:8806/"
TMP = Path(tempfile.gettempdir()) / "letolto_gui_valogatas"
shutil.rmtree(TMP, ignore_errors=True)
R = []


def check(name, ok, info=""):
    R.append(bool(ok))
    print(("[OK]   " if ok else "[HIBA] ") + name + (f"  -> {info}" if info else ""))


import tkinter as tk

cap = {}
orig = tk.Tk.mainloop
tk.Tk.mainloop = lambda self: cap.setdefault("app", self)
testsrv.temp_settings(letolto, "gui_valogatas")
letolto.run_gui()
app = cap["app"]
tk.Tk.mainloop = orig


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.02)


app.v_url.set(BASE)
app.v_dir.set(str(TMP))
app.v_ext.set("")
app.v_html.set(False)
app.v_depth.set(2)
app.v_threads.set(4)
app._scan()
pump(8)

print("\n--- A) kiterjesztés-panel ---")
names = list(app._ext_vars)
print("    panel elemei:", names)
check("a panel feltöltődött", len(names) >= 5, str(len(names)))
check("a kiterjesztés nélküli is szerepel", letolto.NO_EXT_LABEL in names)
check("a html is szerepel", letolto.HTML_LABEL in names)
check("üres szűrőnél minden be van pipálva, a html kivéve",
      all(v.get() for n, v in app._ext_vars.items() if n != letolto.HTML_LABEL)
      and not app._ext_vars[letolto.HTML_LABEL].get())

sel = sum(1 for i in app.manager.items.values() if i.selected)
total = len(app.manager.items)
print("    kijelölve:", app.v_count.get())
check("a HTML-ek nincsenek kijelölve", sel == total - len(app.manager.items and
      [i for i in app.manager.items.values() if i.label == letolto.HTML_LABEL]),
      f"{sel}/{total}")

print("\n--- B) 'Egyik sem' és 'Összes' gomb a kiterjesztéseknél ---")
app._set_all_extensions(False)
pump(0.3)
check("egyik sem -> nulla kijelölt",
      sum(1 for i in app.manager.items.values() if i.selected) == 0, app.v_count.get())
app._set_all_extensions(True)
pump(0.3)
check("összes -> minden kijelölt",
      sum(1 for i in app.manager.items.values() if i.selected) == total, app.v_count.get())

print("\n--- C) egyetlen kiterjesztés ki-be pipálása ---")
app._ext_vars["pdf"].set(False)
app._on_extension_toggled("pdf")
pump(0.3)
pdfs = [i for i in app.manager.items.values() if i.label == "pdf"]
check("a pdf fájlok kijelölése megszűnt", all(not i.selected for i in pdfs), str(len(pdfs)))
check("a többi érintetlen",
      all(i.selected for i in app.manager.items.values() if i.label != "pdf"))
row = app.tree.item(pdfs[0].url)["values"]
check("a táblázatban is üres pipa látszik", row[0] == letolto.UNCHECKED_MARK, str(row[0]))

print("\n--- D) fájllista: sor pipálása ---")
app._toggle_files([pdfs[0].url])
pump(0.2)
check("egy sor bepipálása működik", app.manager.items[pdfs[0].url].selected)
check("a táblázat is követi",
      app.tree.item(pdfs[0].url)["values"][0] == letolto.CHECKED_MARK)
app._toggle_files([pdfs[0].url])
pump(0.2)
check("visszapipálás is működik", not app.manager.items[pdfs[0].url].selected)

print("\n--- E) 'Összes kijelölése' / 'Kijelölés törlése' a listánál ---")
app._set_all_files(False)
pump(0.3)
check("kijelölés törlése", sum(1 for i in app.manager.items.values() if i.selected) == 0,
      app.v_count.get())
marks = {app.tree.item(u)["values"][0] for u in app.tree.get_children()}
check("minden sorban üres pipa", marks == {letolto.UNCHECKED_MARK}, str(marks))
app._set_all_files(True)
pump(0.3)
check("összes kijelölése", sum(1 for i in app.manager.items.values() if i.selected) == total,
      app.v_count.get())

print("\n--- F) csak a kipipáltak töltődnek le ---")
app._set_all_files(False)
pngs = [i.url for i in app.manager.items.values() if i.label == "png"]
app._toggle_files(pngs)
pump(0.3)
app._start()
for _ in range(400):
    app.update()
    time.sleep(0.02)
    if app.manager and not app.manager.running:
        break
files = [p for p in TMP.rglob("*") if p.is_file() and p.name != "_letoltes_allapot.json"]
print("    letöltve:", [f.name for f in files])
check("csak a png jött le", files and all(f.suffix == ".png" for f in files),
      str([f.name for f in files]))

print("\n--- G) HTML kapcsoló ---")
app.v_html.set(True)
app._scan()
pump(8)
check("html bekapcsolva: a lapok is kijelöltek",
      all(i.selected for i in app.manager.items.values() if i.label == letolto.HTML_LABEL))
check("a panelen is be van pipálva", app._ext_vars[letolto.HTML_LABEL].get())

print("\n--- H) A pipa valtasa kotegelve tortenik ---")
# A set_selected() minden hivasa vegigjarja a teljes listat (osszesites), ezert
# sok sornal elemenkent hivva a felulet masodpercekre megallna.
hivasok = []
eredeti_set = app.manager.set_selected
app.manager.set_selected = lambda urls, value: (hivasok.append(len(list(urls))),
                                                eredeti_set(urls, value))[1]
sorok = list(app.manager.items)[:20]
try:
    app._toggle_files(sorok)
finally:
    app.manager.set_selected = eredeti_set
check("husz sor valtasa legfeljebb ket hivas", len(hivasok) <= 2, f"{len(hivasok)} hivas")
check("es mind a husz sor benne van", sum(hivasok) == len(sorok), str(hivasok))
check("a pipak tenyleg atvaltottak",
      all(app.manager.items[u].selected is not None for u in sorok))

print("\n--- I) A kijelöltek szama az osszesitobol jon ---")
app.manager.set_selected(list(app.manager.items), False)
app._update_count()
check("nulla kijelolt", app.v_count.get().startswith("0 / "), app.v_count.get())
app.manager.set_selected(sorok, True)
app._update_count()
check("husz kijelolt", app.v_count.get().startswith(f"{len(sorok)} / "), app.v_count.get())
check("a szam egyezik a tenyleges pipakkal",
      app.manager.totals.files == sum(1 for i in app.manager.items.values() if i.selected))

app._on_close()
print("\n=== GUI OSSZEGZES: %d / %d ===" % (sum(R), len(R)))
srv.shutdown()
sys.exit(0 if all(R) else 1)
