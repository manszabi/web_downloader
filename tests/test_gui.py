"""A GUI valodi futtatasa (Xvfb): atvizsgalas -> letoltes -> szunet -> leallitas."""
import shutil
import sys
from pathlib import Path
import time
from pathlib import Path

# A letolto.py lehet a teszt mellett vagy egy szinttel feljebb (tests/ mappa).
_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent)]
import testsrv
import letolto

srv = testsrv.serve(8779)
BASE = "http://127.0.0.1:8779/"
OUT = Path("/home/claude/gui_out")
shutil.rmtree(OUT, ignore_errors=True)

R = []


def check(name, ok, info=""):
    R.append(ok)
    print(("[OK]   " if ok else "[HIBA] ") + name + (f"  -> {info}" if info else ""))


# a run_gui belsejeben definialt App osztalyt ugy erjuk el, hogy a mainloop-ot kicsereljuk
import tkinter as tk

captured = {}
orig_mainloop = tk.Tk.mainloop
tk.Tk.mainloop = lambda self: captured.setdefault("app", self)   # nem indit vegtelen ciklust
testsrv.temp_settings(letolto, "gui")      # sajat beallitasfajl, ne szivarogjon at semmi
letolto.run_gui()
app = captured["app"]
tk.Tk.mainloop = orig_mainloop
check("a foablak letrejott", app is not None)


def pump(seconds):
    """A tkinter esemenyhurok kezi porgetese."""
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.02)


app.v_url.set(BASE)
app.v_dir.set(str(OUT))
app.v_ext.set("")
app.v_depth.set(1)
app.v_html.set(False)
app.v_threads.set(4)
app.v_robots.set(True)

# --- atvizsgalas ---
app._scan()
pump(6)
check("az atvizsgalas lefutott", not app.scanning)
check("a manager megkapta a fajlokat", app.manager and len(app.manager.items) >= 10,
      f"{len(app.manager.items) if app.manager else 0} elem")
check("a tablazat feltoltodott", len(app.tree.get_children()) >= 10,
      f"{len(app.tree.get_children())} sor")

# --- letoltes inditasa, kozben szunet ---
testsrv.H.stall = 0.004
app._start()
pump(1.0)
check("indulaskor a Szunet gomb aktiv", str(app.b_pause["state"]) == "normal")
app._pause()
pump(0.6)
check("szunet allapotban a gomb felirata 'Folytatas'", app.b_pause["text"] == "Folytatás",
      app.b_pause["text"])
a = app.manager.totals.bytes_done
pump(1.0)
check("szunetben nem no a letoltott mennyiseg", a == app.manager.totals.bytes_done)
app._pause()
testsrv.H.stall = 0
pump(8)

done = sum(1 for i in app.manager.items.values() if i.status == letolto.Status.DONE)
kijelolt = sum(1 for i in app.manager.items.values() if i.selected)
check("minden KIJELOLT fajl elkeszult a GUI-bol vezerelve", done == kijelolt,
      f"{done}/{kijelolt} (a lista {len(app.manager.items)} elemu, a HTML nincs kijelolve)")
check("a lemezen is ott vannak", len(list(OUT.rglob("*"))) > 10)
check("az allapotsor kitoltott", bool(app.v_status.get()))
check("a naplo irodott", "Kész" in app.log.get("1.0", "end") or
      "Átvizsgálás" in app.log.get("1.0", "end"))
check("nem maradt .part", not list(OUT.rglob("*.part")))

# --- sorok tartalma ---
first = app.tree.get_children()[0]
vals = app.tree.item(first)["values"]
print("    elso sor:", vals)
check("a sorban ertelmes ertekek vannak", len(vals) == 6 and vals[5] in ("kész", "várakozik"))

# --- naplokorlat ---
for i in range(letolto.LOG_MAX_LINES + 200):
    app._write_log(f"sor {i}")
lines = int(app.log.index("end-1c").split(".")[0])
check("a naplo nem no korlatlanul", lines <= letolto.LOG_MAX_LINES + 5, f"{lines} sor")

# --- bezaras ---
app._on_close()
check("bezarasnal nincs kivetel", True)

print("\n=== GUI OSSZEGZES: %d / %d ===" % (sum(R), len(R)))
srv.shutdown()
sys.exit(0 if all(R) else 1)
