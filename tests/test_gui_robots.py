"""A GUI uj kapcsoloja (robots.txt 5xx) es a naplofajl osszekotese."""
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

TMP = Path(tempfile.gettempdir()) / "letolto_gui_robots"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)
R = []


def check(name, ok, info=""):
    R.append(ok)
    print(("[OK]   " if ok else "[HIBA] ") + name + (f"  -> {info}" if info else ""))


import tkinter as tk

captured = {}
orig_mainloop = tk.Tk.mainloop
tk.Tk.mainloop = lambda self: captured.setdefault("app", self)
testsrv.temp_settings(letolto, "gui_robots")
letolto.close_file_log()
naplo = letolto.setup_file_log(TMP / "naplo.log")
letolto.run_gui()
app = captured["app"]
tk.Tk.mainloop = orig_mainloop


def widgets(root):
    for child in root.winfo_children():
        yield child
        yield from widgets(child)


def find_check(text):
    for w in widgets(app):
        if isinstance(w, tk.ttk.Checkbutton) and str(w.cget("text")) == text:
            return w
    return None


print("\n--- 1. A kapcsolo ott van a felületen ---")
app.update()
robots = find_check("robots.txt betartása")
ujj = find_check("5xx hibánál leáll")
check("megvan a robots.txt kapcsolo", robots is not None)
check("megvan az 5xx kapcsolo", ujj is not None)
check("latszik is (nem takarja semmi)", bool(ujj and ujj.winfo_ismapped()))
check("van merete", bool(ujj and ujj.winfo_width() > 10 and ujj.winfo_height() > 5),
      f"{ujj.winfo_width()}x{ujj.winfo_height()}" if ujj else "")
check("a robots.txt kapcsoloval egy sorban all",
      robots.grid_info()["row"] == ujj.grid_info()["row"], 
      f"{robots.grid_info()['row']} vs {ujj.grid_info()['row']}")
check("de nem egy oszlopban", robots.grid_info()["column"] != ujj.grid_info()["column"])
check("nem lognak egymasba",
      ujj.winfo_rootx() >= robots.winfo_rootx() + robots.winfo_width(),
      f"robots vege {robots.winfo_rootx() + robots.winfo_width()}, 5xx eleje {ujj.winfo_rootx()}")
check("alapbol nincs kipipalva", app.v_robots5xx.get() is False)
check("van kozotte hely", ujj.winfo_rootx() > robots.winfo_rootx() + robots.winfo_width(),
      f"{ujj.winfo_rootx() - robots.winfo_rootx() - robots.winfo_width()} px")

print("\n--- 1b. A robots.txt kikapcsolasa szurkiti az 5xx kapcsolot ---")
app.v_robots.set(False)
app.update()
check("kikapcsolt robots -> szurke 5xx kapcsolo", "disabled" in str(ujj.state()), str(ujj.state()))
app.v_robots.set(True)
app.update()
check("visszakapcsolva ujra hasznalhato", "disabled" not in str(ujj.state()), str(ujj.state()))

print("\n--- 2. A kapcsolo elér az átvizsgálásig ---")
kapott = {}


class FakeScanner:
    def __init__(self, cfg, client, on_log, stop):
        kapott["cfg"] = cfg
        on_log("teszt naplosor az atvizsgalasbol")

    def run(self):
        return letolto.ScanResult()


eredeti_scanner = letolto.Scanner
letolto.Scanner = FakeScanner
try:
    app.v_url.set("http://127.0.0.1:1/")
    app.v_dir.set(str(TMP / "cel"))
    app.v_robots5xx.set(True)
    app._scan()
    for _ in range(60):
        app.update()
        time.sleep(0.02)
        if "cfg" in kapott and not app.scanning:
            break
finally:
    letolto.Scanner = eredeti_scanner
check("a pipa atkerul a ScanConfig-ba", kapott["cfg"].stop_on_robots_error is True)
check("a robots.txt kapcsolo is atkerul", kapott["cfg"].respect_robots is True)

app.v_robots5xx.set(False)
kapott.clear()
letolto.Scanner = FakeScanner
try:
    app._scan()
    for _ in range(60):
        app.update()
        time.sleep(0.02)
        if "cfg" in kapott and not app.scanning:
            break
finally:
    letolto.Scanner = eredeti_scanner
check("pipa nelkul folytatas", kapott["cfg"].stop_on_robots_error is False)

print("\n--- 3. Beallitasok mentese es visszatoltese ---")
app.v_robots5xx.set(True)
app._save_settings()
mentett = letolto.SETTINGS_FILE.read_text(encoding="utf-8")
check("a mentesbe bekerul", '"robots5xx": true' in mentett, mentett[:120])
app.v_robots5xx.set(False)
app._restore_settings()
check("visszatoltve is megmarad", app.v_robots5xx.get() is True)

print("\n--- 4. A felulet naploja a fajlba is ir ---")
app._write_log("GUI-naplosor a fajlba")
app.update()
szoveg = naplo.read_text(encoding="utf-8")
check("a GUI uzenete a naplofajlban", "GUI-naplosor a fajlba" in szoveg)
check("az atvizsgalas uzenete is", "teszt naplosor az atvizsgalasbol" in szoveg)
check("a panelen is ott van", "GUI-naplosor a fajlba" in app.log.get("1.0", "end"))

app._on_close()
letolto.close_file_log()
shutil.rmtree(TMP, ignore_errors=True)
print("\n=== OSSZEGZES: %d / %d ===" % (sum(R), len(R)))
sys.exit(0 if all(R) else 1)
