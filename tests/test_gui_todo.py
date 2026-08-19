"""A negy uj GUI-funkcio: atvizsgalas megszakitasa, ket torles, fajlmeretek."""
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

srv = testsrv.serve(8814)
BASE = "http://127.0.0.1:8814/"
TMP = Path(tempfile.gettempdir()) / "letolto_gui_todo"
shutil.rmtree(TMP, ignore_errors=True)
R = []


def check(name, ok, info=""):
    R.append(bool(ok))
    print(("[OK]   " if ok else "[HIBA] ") + name + (f"  -> {info}" if info else ""))


import tkinter as tk

cap = {}
orig = tk.Tk.mainloop
tk.Tk.mainloop = lambda self: cap.setdefault("app", self)
SETTINGS = testsrv.temp_settings(letolto, "gui_todo")
letolto.run_gui()
app = cap["app"]
tk.Tk.mainloop = orig


def pump(seconds, amig=None):
    """Esemenyhurok porgetese; ha az "amig" igazza valik, hamarabb visszaterunk."""
    veg = time.time() + seconds
    while time.time() < veg:
        app.update()
        time.sleep(0.02)
        if amig is not None and amig():
            return True
    return amig is None


def allapot(gomb):
    return "disabled" if "disabled" in gomb.state() else "normal"


app.v_url.set(BASE)
app.v_dir.set(str(TMP / "cel"))
app.v_depth.set(2)
app.v_ext.set("")
app.v_html.set(False)

print("--- 1. A gombsor ---")
check("megvan az 'Átvizsgálás megszakítása' gomb",
      app.b_scan_stop.cget("text") == "Átvizsgálás megszakítása")
check("megvan a 'Beállítások törlése' gomb",
      app.b_settings_reset.cget("text") == "Beállítások törlése")
check("megvan a 'Letöltési állapot törlése' gomb",
      app.b_state_reset.cget("text") == "Letöltési állapot törlése")
app.update()
for nev, sor in (("felso", app.b_scan.master), ("also", app.b_open.master)):
    check(f"a {nev} gombsor kifér a legkisebb ablakban",
          sor.winfo_reqwidth() <= app.minsize()[0],
          f"{sor.winfo_reqwidth()} / {app.minsize()[0]} px")
check("a torlo gombok tavolabb vannak a mappa-gomboktol",
      app.b_settings_reset.winfo_rootx() - (app.b_settings.winfo_rootx()
                                            + app.b_settings.winfo_width()) > 60,
      f"{app.b_settings_reset.winfo_rootx() - app.b_settings.winfo_rootx()} px")
check("indulaskor a megszakito gomb szurke", allapot(app.b_scan_stop) == "disabled")
check("megszakitas atvizsgalas nelkul nem csinal semmit", app._cancel_scan() is None)

print("\n--- 2. Az atvizsgalas megszakithato ---")
testsrv.H.stall = 0.02                      # lassitott kiszolgalo, legyen ido kozbeszolni
app._scan()
# A gombok allapotat a _scan() meg a hattérszal inditasa elott allitja be, tehat
# itt meg biztosan az atvizsgalas kozbeni allapotot latjuk (nincs versenyhelyzet).
check("atvizsgalas kozben a megszakito gomb elo", allapot(app.b_scan_stop) == "normal")
check("kozben az Atvizsgalas gomb szurke", allapot(app.b_scan) == "disabled")
check("az atvizsgalas fut", app.scanning)
app._cancel_scan()
check("megszakitas utan a gomb ujra szurke", allapot(app.b_scan_stop) == "disabled")
pump(15, amig=lambda: not app.scanning)
testsrv.H.stall = 0
check("az atvizsgalas tenyleg befejezodott", not app.scanning)
check("a megszakitas nyoma a naploban", "megszakítva" in app.log.get("1.0", "end"))
check("az addigi talalatok megmaradtak", app.manager is not None)

print("\n--- 3. A letoltesi allapot torolheto ---")
app.v_url.set(BASE)
app._scan()
pump(20, amig=lambda: not app.scanning and app.manager and len(app.manager.items) > 5)
darab = len(app.manager.items)
check("van mit torolni", darab > 5, f"{darab} elem")
allapotfajl = app.manager.store.path
app.manager.store.save(app.manager.items, force=True)
check("az allapotfajl letezik", allapotfajl.is_file())

valasz = {"ertek": False}
letolto_gui = sys.modules["letolto_gui"]
eredeti_kerdes = letolto_gui.messagebox.askyesno
letolto_gui.messagebox.askyesno = lambda *a, **k: valasz["ertek"]
try:
    app._reset_state()                       # "nem" valasz
    check("nemleges valasznal nem tortenik semmi",
          len(app.manager.items) == darab and allapotfajl.is_file())
    valasz["ertek"] = True
    app._reset_state()                       # "igen" valasz
    pump(0.5)                                # a "reset" esemeny a sorbol jon
    check("a lista kiurult", not app.manager.items, str(len(app.manager.items)))
    check("az allapotfajl eltunt", not allapotfajl.exists())
    check("a tablazat is kiurult", not app.tree.get_children())
    check("a szamlalo nullazodott", app.v_count.get().startswith("0 / 0"), app.v_count.get())
    check("a kiterjesztes-panel is urult", not app._ext_vars)
finally:
    letolto_gui.messagebox.askyesno = eredeti_kerdes

print("\n--- 4. A beallitasok torolhetok ---")
app.v_url.set("http://valami.hu/")
app.v_depth.set(5)
app.v_robots5xx.set(True)
app._save_settings()
check("a beallitasfajl elkeszult", SETTINGS.is_file())
letolto_gui.messagebox.askyesno = lambda *a, **k: True
try:
    app._reset_settings()
finally:
    letolto_gui.messagebox.askyesno = eredeti_kerdes
check("a beallitasfajl eltunt", not SETTINGS.exists())
check("az URL a gyari erteken", app.v_url.get() == letolto_gui.ALAPERTEK["url"],
      app.v_url.get())
check("a melyseg a gyari erteken", app.v_depth.get() == letolto_gui.ALAPERTEK["depth"],
      str(app.v_depth.get()))
check("az 5xx kapcsolo a gyari erteken", app.v_robots5xx.get() is False)
check("a torles nyoma a naploban", "beállítások törölve" in app.log.get("1.0", "end").lower())
check("minden mentett kulcsnak van gyari erteke",
      set(app._settings_vars()) == set(letolto_gui.ALAPERTEK))

print("\n--- 5. Fajlmeretek az atvizsgalas utan ---")
app.v_url.set(BASE)
app.v_dir.set(str(TMP / "cel2"))
app._scan()
pump(25, amig=lambda: app.manager and len(app.manager.items) > 5
     and sum(1 for i in app.manager.items.values() if not i.total) <= 1)
pump(0.6)                     # a "sizes" esemeny a sorbol jon: a sorok utana frissulnek
elemek = list(app.manager.items.values())
meretesek = [i for i in elemek if i.total]
# Egyetlen kivetel a gzip-vegpont: ott a kiszolgalo a tomoritett hosszt kozli,
# a kicsomagolt meret pedig nem derul ki elore - ezt a program szandekosan
# hagyja ismeretlenul, nehogy rossz szamot mutasson.
meret_nelkul = [i.url for i in elemek if not i.total]
check("a gzip-vegponton kivul mindenhol megvan a meret",
      all("/gz/" in u for u in meret_nelkul), str(meret_nelkul))
check("a talalatok tobbsegenel megvan a meret", len(meretesek) >= len(elemek) - 1,
      f"{len(meretesek)}/{len(elemek)}")
check("a meret a valodi fajlmeret",
      any(i.total == len(testsrv.FILES.get("/files/a.bin", b"")) for i in elemek),
      str(sorted({i.total for i in elemek})[:4]))
sorok = [app.tree.item(u, "values") for u in app.tree.get_children()][:3]
check("a tablazatban is latszik a meret", all(s[2] != "-" for s in sorok), str(sorok))
check("a szamlalo az osszmeretet is mutatja", "·" in app.v_count.get(), app.v_count.get())
check("az osszmeret a kijeloltekbol jon",
      app.manager.totals.bytes_total == sum(i.total for i in elemek if i.selected),
      app.v_count.get())

app._on_close()
shutil.rmtree(TMP, ignore_errors=True)
print("\n=== GUI TODO OSSZEGZES: %d / %d ===" % (sum(R), len(R)))
srv.shutdown()
sys.exit(0 if all(R) else 1)
