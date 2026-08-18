"""GUI: beallitasok-mappa gomb es a kiterjesztes-panel <-> Kiterjesztesek mezo szinkron."""
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

srv = testsrv.serve(8807)
BASE = "http://127.0.0.1:8807/"
TMP = Path(tempfile.gettempdir()) / "letolto_gui_szinkron"
shutil.rmtree(TMP, ignore_errors=True)
R = []


def check(name, ok, info=""):
    R.append(ok)
    print(("[OK]   " if ok else "[HIBA] ") + name + (f"  -> {info}" if info else ""))


# A beallitasfajl a teszt sajat mappajaba keruljon, ne a valodi helyere.
SETTINGS = TMP / "beallitasok" / "beallitasok.json"
SETTINGS.parent.parent.mkdir(parents=True, exist_ok=True)
letolto.SETTINGS_FILE = SETTINGS

opened = []
letolto.open_in_file_manager = lambda path: opened.append(("open", Path(path)))

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


def checked():
    """A panelen bepipalt cimkek, a panel sorrendjeben."""
    return [n for n, v in app._ext_vars.items() if v.get()]


def selected_labels():
    """Amelyik cimkehez tartozo fajlokbol legalabb egy ki van jelolve."""
    return {i.label for i in app.manager.items.values() if i.selected}


print("--- A) Beállítások mappája gomb ---")
check("a gomb létrejött", app.b_settings.cget("text") == "Beállítások mappája")
check("a beállításfájl még nincs meg", not SETTINGS.exists())
app._open_settings_dir()
pump(0.2)
check("a mappa létrejött", SETTINGS.parent.is_dir())
check("a beállításfájl is elkészült, hogy legyen mit megnézni", SETTINGS.is_file())
check("megnyílt a fájlkezelő a beállítások mappájával",
      opened and opened[-1][1] == SETTINGS.parent, str(opened))

print("\n--- Átvizsgálás ---")
app.v_url.set(BASE)
app.v_dir.set(str(TMP))
app.v_ext.set("")
app.v_html.set(False)
app.v_depth.set(2)
app.v_threads.set(4)
pump(0.6)
app._scan()
pump(8)
names = list(app._ext_vars)
print("    panel elemei:", names)
check("a panel feltöltődött", len(names) >= 5, str(len(names)))
check("üres mező: minden be van pipálva a html kivételével",
      set(checked()) == set(names) - {letolto.HTML_LABEL}, str(checked()))

print("\n--- B) panel -> mező: egy kiterjesztés kipipálása ---")
app._ext_vars["pdf"].set(False)
app._on_extension_toggled("pdf")
pump(0.6)
print("    mező:", repr(app.v_ext.get()))
check("a mezőben megjelent a lista", app.v_ext.get() == ", ".join(checked()), app.v_ext.get())
check("a pdf kimaradt a mezőből", "pdf" not in letolto.parse_ext_filter(app.v_ext.get()))
check("a pdf a panelen sem maradt pipálva", not app._ext_vars["pdf"].get())
check("a pdf fájlokról lekerült a pipa", "pdf" not in selected_labels())

print("\n--- C) panel -> HTML jelölőnégyzet ---")
app._ext_vars[letolto.HTML_LABEL].set(True)
app._on_extension_toggled(letolto.HTML_LABEL)
pump(0.6)
check("a HTML letöltése bepipálódott", app.v_html.get())
check("a html nem kerül bele a mezőbe",
      letolto.HTML_LABEL not in letolto.parse_ext_filter(app.v_ext.get()), app.v_ext.get())
check("a html fájlok kijelölődtek", letolto.HTML_LABEL in selected_labels())
app._ext_vars[letolto.HTML_LABEL].set(False)
app._on_extension_toggled(letolto.HTML_LABEL)
pump(0.6)
check("kipipálva a HTML kapcsoló is kikapcsol", not app.v_html.get())

print("\n--- D) mező -> panel: kézi beírás ---")
app.v_ext.set("png")
pump(1.0)
check("csak a png maradt kipipálva", checked() == ["png"], str(checked()))
check("a fájlok kijelölése is követi", selected_labels() == {"png"}, str(selected_labels()))
check("a beírt szöveg érintetlen maradt", app.v_ext.get() == "png", app.v_ext.get())

app.v_ext.set("png, .PDF")
pump(1.0)
check("több kiterjesztés, pont és nagybetű is jó",
      set(checked()) == {"png", "pdf"}, str(checked()))

print("\n--- E) mező -> panel: HTML jelölőnégyzet ---")
app.v_html.set(True)
pump(1.0)
check("a html a panelen is bepipálódott", app._ext_vars[letolto.HTML_LABEL].get())
check("a többi maradt", set(checked()) == {"png", "pdf", letolto.HTML_LABEL}, str(checked()))
app.v_html.set(False)
pump(1.0)
check("kikapcsolva a panelről is lekerül", not app._ext_vars[letolto.HTML_LABEL].get())

print("\n--- F) üres mező = minden kiterjesztés ---")
app.v_ext.set("")
pump(1.0)
check("üres mezőre minden bepipálódik (html nélkül)",
      set(checked()) == set(names) - {letolto.HTML_LABEL}, str(checked()))

print("\n--- G) Összes / Egyik sem gomb és a mező ---")
app._set_all_extensions(False)
pump(1.0)
check("egyik sem: a mező jelzi az üres kijelölést",
      app.v_ext.get() == letolto.NONE_TOKEN, app.v_ext.get())
check("a panelen sincs pipa", checked() == [], str(checked()))
check("egy pumpálás után sem pipálódik vissza semmi", checked() == [], str(checked()))
check("nincs kijelölt fájl sem", selected_labels() == set(), str(selected_labels()))
app._set_all_extensions(True)
pump(1.0)
check("összes: minden címke a mezőbe kerül",
      letolto.parse_ext_filter(app.v_ext.get()) == {n.lower() for n in names
                                                    if n != letolto.HTML_LABEL},
      app.v_ext.get())
check("a html a kapcsolóra került", app.v_html.get())
check("a panel pipái megmaradtak", set(checked()) == set(names), str(checked()))

print("\n--- H) a mező és a panel egyensúlyban marad ---")
before = (app.v_ext.get(), app.v_html.get(), checked())
pump(1.5)
check("magától nem változik tovább semmi",
      (app.v_ext.get(), app.v_html.get(), checked()) == before, str(before))

print("\n--- I) újabb átvizsgálás a mező szerint ---")
app.v_ext.set("png")
app.v_html.set(False)
pump(1.0)
app._scan()
pump(8)
check("átvizsgálás után is csak a png van kipipálva", checked() == ["png"], str(checked()))
check("a mező a beírt maradt", app.v_ext.get() == "png", app.v_ext.get())

app._on_close()
print("\n=== GUI SZINKRON OSSZEGZES: %d / %d ===" % (sum(R), len(R)))
srv.shutdown()
sys.exit(0 if all(R) else 1)
