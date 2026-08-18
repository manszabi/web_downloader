"""Osszeomlas -> GUI ujraindul: most magatol betolti-e?"""
import subprocess, sys, time, shutil, os
from pathlib import Path
# A letolto.py lehet a teszt mellett vagy egy szinttel feljebb (tests/ mappa).
_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent)]
import testsrv
srv = testsrv.serve(8791); import tempfile
OUT = Path(tempfile.gettempdir()) / "letolto_osszeomlas"
shutil.rmtree(OUT, ignore_errors=True)
R=[]
def check(n, ok, i=""):
    R.append(ok); print(("[OK]   " if ok else "[HIBA] ")+n+(f"  -> {i}" if i else ""))

WORKER = Path(tempfile.gettempdir()) / "_letolto_crash_worker.py"
WORKER.write_text(f"""
import sys, time
sys.path[:0] = [r"{_HERE}", r"{_HERE.parent}"]
from letolto import DownloadManager
m = DownloadManager("{OUT}", 2); m.load_state()
m.add_urls(["http://127.0.0.1:8791/huge/nolink","http://127.0.0.1:8791/files/a.bin","http://127.0.0.1:8791/files/b.bin"])
m.start(); time.sleep(60)
""")
testsrv.H.stall = 0.05
p = subprocess.Popen([sys.executable, str(WORKER)]); time.sleep(4); p.kill(); p.wait()
testsrv.H.stall = 0
parts = [(f.name, f.stat().st_size) for f in OUT.rglob("*.part")]
print("kill utan .part:", parts)

import tkinter as tk, letolto
cap={}; ml=tk.Tk.mainloop; tk.Tk.mainloop=lambda s: cap.setdefault("a",s)
letolto.run_gui(); app=cap["a"]; tk.Tk.mainloop=ml
app.v_dir.set(str(OUT))
for _ in range(60): app.update(); time.sleep(0.03)   # ~1.8 s: a 800 ms-os kesleltetes lejar

print("\nINDULAS UTAN, gombnyomas nelkul:")
print("  tablazat sorai:", len(app.tree.get_children()))
print("  allapotsor    :", app.v_status.get())
print("  naplo         :", app.log.get("1.0","end").strip().splitlines()[-1:])
check("magatol betoltotte az elozo allapotot", app.manager is not None and len(app.manager.items) == 3,
      f"{len(app.manager.items) if app.manager else 0} elem")
check("a tablazat kitoltodott", len(app.tree.get_children()) == 3)
check("az allapotsor jelzi a folytathatot", "folytatható" in app.v_status.get(), app.v_status.get())
check("latja a reszben letoltott fajlt", any(i.done > 0 and i.status != letolto.Status.DONE
                                             for i in app.manager.items.values()))

# Inditas: folytassa is
app._start()
for _ in range(200):
    app.update(); time.sleep(0.03)
    if app.manager and not app.manager.running: break
done = sum(1 for i in app.manager.items.values() if i.status == letolto.Status.DONE)
check("az Inditas befejezte a felbemaradt munkat", done == 3, f"{done}/3")
import hashlib
def md5f(x):
    h=hashlib.md5()
    with open(x,'rb') as f:
        for c in iter(lambda: f.read(1<<20), b''): h.update(c)
    return h.hexdigest()
import urllib.parse
ok=True
for i in app.manager.items.values():
    k = urllib.parse.unquote(urllib.parse.urlparse(i.url).path)
    if md5f(OUT/i.path) != hashlib.md5(testsrv.FILES[k]).hexdigest(): ok=False
check("minden fajl bitre ep", ok)

# beallitasok megjegyzese
app._on_close()
cfg = Path.home()/".letolto_beallitasok.json"
check("bezaraskor elmentette a beallitasokat", cfg.exists())
if cfg.exists():
    import json; d=json.loads(cfg.read_text())
    check("a celkonyvtar is elmentodott", d.get("dir") == str(OUT), d.get("dir"))

# ujraindulas: emlekszik-e a konyvtarra es tolt-e magatol
cap2={}; tk.Tk.mainloop=lambda s: cap2.setdefault("a",s)
letolto.run_gui(); app2=cap2["a"]; tk.Tk.mainloop=ml
for _ in range(40): app2.update(); time.sleep(0.03)
check("uj inditasnal emlekszik a konyvtarra", app2.v_dir.get() == str(OUT), app2.v_dir.get())
check("es magatol be is toltotte", app2.manager is not None and len(app2.manager.items)==3,
      f"{len(app2.manager.items) if app2.manager else 0}")
print("  allapotsor:", app2.v_status.get())
app2._on_close()
print("\n=== %d / %d ===" % (sum(R), len(R)))
srv.shutdown(); sys.exit(0 if all(R) else 1)
