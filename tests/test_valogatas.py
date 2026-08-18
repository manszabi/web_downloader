"""Kiterjesztes-panel, fajlonkenti pipa es HTML-kapcsolo tesztje."""
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

# A letolto.py lehet a teszt mellett vagy egy szinttel feljebb (tests/ mappa).
_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent)]
import testsrv
from letolto import (HTML_LABEL, NO_EXT_LABEL, NONE_TOKEN, DownloadManager, Item,
                     ScanConfig, Scanner, Status, choose_labels, ext_filter_text, ext_label,
                     item_label, make_client, matching_extensions, parse_ext_filter,
                     text_representable)

srv = testsrv.serve(8805)
BASE = "http://127.0.0.1:8805/"
TMP = Path(tempfile.gettempdir()) / "letolto_valogatas"
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


def scan(depth=1):
    cfg = ScanConfig(root=BASE, depth=depth, same_host=True, respect_robots=True)
    return Scanner(cfg, client, lambda _m: None, threading.Event()).run()


print("\n--- 1. Az átvizsgálás minden kiterjesztést megtalál ---")
res = scan(depth=2)
groups = res.by_extension()
print("    talált csoportok:", {k: len(v) for k, v in groups.items()})
check("bin megvan", "bin" in groups)
check("pdf megvan", "pdf" in groups)
check("png megvan", "png" in groups)
check("txt megvan", "txt" in groups)
check("html csoport is van (a bejárt lapok)", HTML_LABEL in groups, str(len(groups.get(HTML_LABEL, []))))
check("a kiterjesztés nélküli fájl külön csoportban",
      NO_EXT_LABEL in groups, str(groups.get(NO_EXT_LABEL)))
check("a csoportok lefedik az összes találatot",
      sum(len(v) for v in groups.values()) == len(res.all_urls))

print("\n--- 2. Címkézés ---")
check("kiterjesztés nélküli címke", ext_label("http://a/b/fajl") == NO_EXT_LABEL)
check("pontos kisbetűsítés", ext_label("http://a/b/FAJL.PDF") == "pdf")
check("lap mindig html", ext_label("http://a/b/valami", is_page=True) == HTML_LABEL)

print("\n--- 3. Kézi szűrő + HTML kapcsoló ---")
check("üres szűrő = minden, HTML nélkül",
      matching_extensions(res, set(), False) == set(groups) - {HTML_LABEL})
check("üres szűrő + HTML bekapcsolva = tényleg minden",
      matching_extensions(res, set(), True) == set(groups))
check("megadott szűrő csak azt hozza",
      matching_extensions(res, {"pdf", "png"}, False) == {"pdf", "png"})
check("a szűrő mellé is bejön a HTML, ha kérték",
      matching_extensions(res, {"pdf"}, True) == {"pdf", HTML_LABEL})
check("nem létező kiterjesztés nem okoz hibát",
      matching_extensions(res, {"docx"}, False) == set())

print("\n--- 4. Csak a kipipált fájlok töltődnek le ---")
OUT = fresh("v_a")
mgr = DownloadManager(OUT, 4, client=client)
mgr.load_state()
labels = {u: n for n, urls in groups.items() for u in urls}
for name, urls in groups.items():
    mgr.add_urls(urls, labels=labels, selected=(name == "pdf"))
check("minden találat bekerült a listába", len(mgr.items) == len(res.all_urls),
      f"{len(mgr.items)}")
check("de csak a pdf van kijelölve", len(mgr.pending()) == len(groups["pdf"]),
      f"{len(mgr.pending())}")
mgr.start()
mgr._worker.join(120)
files = [p for p in OUT.rglob("*") if p.is_file() and p.name != "_letoltes_allapot.json"]
print("    letöltött fájlok:", [f.name for f in files])
check("csak a pdf töltődött le", all(f.suffix == ".pdf" for f in files), str(len(files)))
check("a nem kijelöltek érintetlenek",
      all(i.status != Status.DONE for i in mgr.items.values() if not i.selected))

print("\n--- 5. Kijelölés váltása menet közben ---")
mgr.set_selected([u for u in groups["png"]], True)
check("az összesítő azonnal követi", mgr.totals.files == len(groups["pdf"]) + len(groups["png"]),
      f"{mgr.totals.files}")
mgr.start()
mgr._worker.join(120)
files = [p for p in OUT.rglob("*") if p.is_file() and p.name != "_letoltes_allapot.json"]
check("az újonnan kijelölt png is letöltődött",
      any(f.suffix == ".png" for f in files), str([f.name for f in files]))

print("\n--- 6. HTML letöltése ---")
OUT = fresh("v_b")
mgr = DownloadManager(OUT, 4, client=client)
mgr.load_state()
mgr.add_urls(groups[HTML_LABEL], labels=labels)
mgr.start()
mgr._worker.join(120)
html_files = [p for p in OUT.rglob("*") if p.is_file() and p.name != "_letoltes_allapot.json"]
print("    HTML fájlok:", [f.name for f in html_files])
check("a lapok is letölthetők", len(html_files) == len(groups[HTML_LABEL]),
      f"{len(html_files)}/{len(groups[HTML_LABEL])}")
check("a gyökér lap értelmes nevet kap", any(f.name.endswith(".html") for f in html_files),
      str([f.name for f in html_files]))

print("\n--- 7. A kijelölés túléli az újraindulást ---")
OUT = fresh("v_c")
mgr = DownloadManager(OUT, 1, client=client)
mgr.load_state()
for name, urls in groups.items():
    mgr.add_urls(urls, labels=labels, selected=(name == "pdf"))
mgr.store.save(mgr.items, force=True)
mgr2 = DownloadManager(OUT, 1, client=client)
mgr2.load_state()
check("a pipák megmaradtak",
      {i.url for i in mgr2.items.values() if i.selected} == set(groups["pdf"]),
      f"{sum(1 for i in mgr2.items.values() if i.selected)} kijelölt")
check("a címkék is megmaradtak",
      all(i.label for i in mgr2.items.values()))

print("\n--- 8. A szűrő szövegének értelmezése (mező <-> panel) ---")
check("vessző, pont, nagybetű, üres tagok",
      parse_ext_filter(" PDF, .zip ,, .Png ") == {"pdf", "zip", "png"},
      str(parse_ext_filter(" PDF, .zip ,, .Png ")))
check("csupa elválasztó = üres kérés", parse_ext_filter(" , ,, ") == set())
check("üres kérés = minden, a html nélkül",
      choose_labels(["pdf", "png", HTML_LABEL], set(), False) == {"pdf", "png"})
check("üres kérés + html kapcsoló",
      choose_labels(["pdf", HTML_LABEL], set(), True) == {"pdf", HTML_LABEL})
check("ismeretlen kiterjesztés = üres kijelölés",
      choose_labels(["pdf", "png"], {"docx"}, False) == set())
check("nem létező címkét nem talál ki",
      choose_labels(["pdf"], {"pdf", "zip"}, False) == {"pdf"})

print("\n--- 9. Panel -> mező szöveg ---")
check("a kipipáltak vesszővel", ext_filter_text(["pdf", "png"]) == "pdf, png")
check("a html nem a mezőbe kerül", ext_filter_text(["pdf", HTML_LABEL]) == "pdf")
check("üres kijelölésnek saját jelölése van", ext_filter_text([]) == NONE_TOKEN)
check("csak html esetén is az", ext_filter_text([HTML_LABEL]) == NONE_TOKEN)
check("a jelölés semmire nem illik",
      choose_labels(["pdf", "png"], parse_ext_filter(NONE_TOKEN), False) == set())

print("\n--- 10. Oda-vissza alakítás (a szinkron alapja) ---")
for case in (["pdf"], ["pdf", "png"], [NO_EXT_LABEL], ["pdf", NO_EXT_LABEL], ["bin", "txt"]):
    text = ext_filter_text(case)
    back = choose_labels(case, parse_ext_filter(text), False)
    check(f"{case} -> {text!r} -> vissza", back == set(case), str(back))

print("\n--- 11. Vesszős utótag (a mező nem tudná kifejezni) ---")
check("a címke tényleg tartalmazhat vesszőt", ext_label("http://a/b/adat.2024,csv") == "2024,csv")
check("ilyet nem tekintünk kifejezhetőnek", not text_representable("2024,csv"))
check("a mezőbe nem kerül bele", ext_filter_text(["pdf", "2024,csv"]) == "pdf")
check("a szokásos címkék kifejezhetők",
      all(text_representable(x) for x in ("pdf", NO_EXT_LABEL, HTML_LABEL)))

print("\n--- 12. Régi állapotfájl: hiányzó címke pótlása ---")
old_item = Item(url="http://a/b/regi.pdf", path="regi.pdf")      # label nélkül mentve
check("a címke a címből is kiolvasható", item_label(old_item) == "pdf", item_label(old_item))
check("a meglévő címke marad", item_label(Item(url="http://a/x", path="x", label="png")) == "png")
check("kiterjesztés nélküli régi elem",
      item_label(Item(url="http://a/b/regi", path="regi")) == NO_EXT_LABEL)

print("\n=== OSSZEGZES: %d / %d ===" % (sum(R), len(R)))
client.close()
srv.shutdown()
sys.exit(0 if all(R) else 1)
