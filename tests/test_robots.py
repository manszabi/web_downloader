"""A robots.txt-szabalyok tesztje az RFC 9309 szerint."""
import sys
import threading
from pathlib import Path

# A letolto.py lehet a teszt mellett vagy egy szinttel feljebb (tests/ mappa).
_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent)]
import testsrv
import letolto
from letolto import (Cancelled, RobotsRules, RobotsUnavailable,
                     ScanConfig, Scanner)

R = []


def check(name, ok, info=""):
    R.append(ok)
    print(("[OK]   " if ok else "[HIBA] ") + name + (f"  -> {info}" if info else ""))


def rules(text, token="PyLetolto"):
    return RobotsRules.parse(text, token)


U = "https://pelda.hu"

print("\n--- 1. Allow felulirja a tagabb Disallow-ot (a leghosszabb minta nyer) ---")
r = rules("""User-agent: *
Disallow: /admin/
Allow: /admin/nyilvanos""")
check("a tiltott ag tiltva marad", not r.allowed(U + "/admin/titok"))
check("az Allow kivetele atmegy", r.allowed(U + "/admin/nyilvanos"))
check("az Allow ala eso melyebb ut is atmegy", r.allowed(U + "/admin/nyilvanos/x.pdf"))
check("a nem erintett ut szabad", r.allowed(U + "/index.html"))

print("\n--- 2. Forditva is a hosszabb minta dont ---")
r = rules("""User-agent: *
Allow: /a/
Disallow: /a/titok""")
check("a hosszabb Disallow nyer", not r.allowed(U + "/a/titok/1.zip"))
check("a tobbi marad engedve", r.allowed(U + "/a/nyilt"))

print("\n--- 3. Azonos hossz eseten az Allow nyer ---")
r = rules("""User-agent: *
Disallow: /x
Allow: /x""")
check("dontetlen -> engedve", r.allowed(U + "/x/1.bin"))

print("\n--- 4. Jokerek: * es a zaro $ ---")
r = rules("""User-agent: *
Disallow: /*.zip$
Allow: /nyilt/*.zip$""")
check("a joker a melyben is illeszkedik", not r.allowed(U + "/b/c/a.zip"))
check("a $ a cim vegehez kot", r.allowed(U + "/b/a.zip.htm"))
check("a specifikusabb Allow felulirja", r.allowed(U + "/nyilt/a.zip"))

print("\n--- 5. Ures Disallow: nem tiltas ---")
check("ures ertek -> minden szabad",
      rules("User-agent: *\nDisallow:").allowed(U + "/barmi"))
check("a puszta / mindent tilt",
      not rules("User-agent: *\nDisallow: /").allowed(U + "/barmi"))

print("\n--- 6. Csoportvalasztas ---")
txt = """User-agent: Masikbot
Disallow: /

User-agent: PyLetolto
Disallow: /privat

User-agent: *
Disallow: /
"""
check("a sajat csoport nyer a * felett", rules(txt).allowed(U + "/kepek/a.jpg"))
check("a sajat csoport tiltasa hat", not rules(txt).allowed(U + "/privat/a.jpg"))
check("ismeretlen bot a * csoportot kapja", not rules(txt, "Harmadikbot").allowed(U + "/x"))

print("\n--- 7. Csoporthatarok es zajos sorok ---")
r = rules("""Disallow: /szabalytalan

User-agent: A
User-agent: PyLetolto
Disallow: /kozos      # ket fejleces csoport
Sitemap: https://pelda.hu/sitemap.xml
Crawl-delay: 5

User-agent: A
Disallow: /csak-A
""")
check("a fejlec nelkuli szabaly kimarad", r.allowed(U + "/szabalytalan"))
check("a tobbfejleces csoport rank is vonatkozik", not r.allowed(U + "/kozos/a"))
check("a szabalysor utani User-agent uj csoportot nyit", r.allowed(U + "/csak-A"))
check("az ismeretlen kulcsok nem zavarnak", r.allowed(U + "/sitemap.xml"))

print("\n--- 8. Kis-nagybetu es ismetlodo csoportok ---")
r = rules("""user-agent: PYLETOLTO
DISALLOW: /egy

User-agent: pyletolto
Disallow: /ketto""")
check("a kulcsok es a nevek kis-nagybetu-fuggetlenek", not r.allowed(U + "/egy"))
check("az azonos nevu csoportok osszeolvadnak", not r.allowed(U + "/ketto"))

print("\n--- 9. Szazalekkodolas es lekerdezes ---")
r = rules("""User-agent: *
Disallow: /doksik/%C3%A9v
Disallow: /kereses?q=""")
check("a kodolt es a nyers alak ugyanaz", not r.allowed(U + "/doksik/%C3%A9v/1.pdf"))
check("az ekezetes nyers alak is illeszkedik", not r.allowed(U + "/doksik/év/1.pdf"))
check("a lekerdezes is szamit", not r.allowed(U + "/kereses?q=abc"))
check("lekerdezes nelkul szabad", r.allowed(U + "/kereses"))

print("\n--- 10. Ures szabalykeszlet ---")
check("szabaly nelkul minden szabad", RobotsRules().allowed(U + "/barmi"))
check("ismeretlen bot, nincs * csoport",
      rules("User-agent: Masikbot\nDisallow: /").allowed(U + "/barmi"))


class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text
        self.headers = {}

    def iter_text(self, size=8192):
        for i in range(0, len(self.text), size):
            yield self.text[i:i + size]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeClient:
    """Csak a robots.txt lekereset szolgalja ki; szamolja a hivasokat.

    A valaszok listajat sorban adja vissza, az utolsot ismetelve. Egy httpx
    kivetel-peldany a listaban halozati hibat jelent.
    """

    def __init__(self, *responses):
        self.responses = list(responses) or [FakeResponse(200, "")]
        self.calls = []

    def stream(self, method, url, timeout=None):
        if not url.endswith("/robots.txt"):
            return FakeResponse(404, "")  # az oldalak maguk nem erdekesek itt
        self.calls.append(url)
        resp = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(resp, Exception):
            raise resp
        return resp


def scanner(client, respect=True, stop_on_error=False, stop=None):
    cfg = ScanConfig(root=U + "/", depth=1, same_host=True, respect_robots=respect,
                     stop_on_robots_error=stop_on_error)
    return Scanner(cfg, client, lambda _m: LOG.append(_m), stop or threading.Event())


LOG = []
letolto.ROBOTS_RETRY_WAIT = 0     # a tesztben ne varjunk a probalkozasok kozott

print("\n--- 11. A Scanner hasznalata ---")
cli = FakeClient(FakeResponse(200,
    "User-agent: *\nDisallow: /admin/\nAllow: /admin/nyilvanos"))
sc = scanner(cli)
check("a Scanner tiltja a tiltottat", not sc._allowed(U + "/admin/titok"))
check("a Scanner atengedi az Allow kivetelt", sc._allowed(U + "/admin/nyilvanos/a.pdf"))
check("a robots.txt kiszolgalonkent egyszer tolt le", len(cli.calls) == 1, str(cli.calls))

cli404 = FakeClient(FakeResponse(404, ""))
check("hianyzo robots.txt -> minden szabad", scanner(cli404)._allowed(U + "/admin/titok"))

cli_off = FakeClient(FakeResponse(200, "User-agent: *\nDisallow: /"))
check("kikapcsolt robots -> nincs letoltes sem",
      scanner(cli_off, respect=False)._allowed(U + "/admin/titok") and not cli_off.calls)

print("\n--- 12. Elerhetetlen robots.txt: ujraprobalkozas ---")
cli = FakeClient(FakeResponse(503, ""))
sc = scanner(cli)
check("5xx utan is folytat, ha nincs pipa", sc._allowed(U + "/a.pdf"))
check("haromszor probalkozott", len(cli.calls) == letolto.ROBOTS_TRIES, str(len(cli.calls)))

cli = FakeClient(FakeResponse(503, ""), FakeResponse(200, "User-agent: *\nDisallow: /admin/"))
sc = scanner(cli, stop_on_error=True)
check("a masodik probalkozas mar sikerul", sc._allowed(U + "/a.pdf"))
check("es a szabalyok ervenyesek", not sc._allowed(U + "/admin/x"))
check("ket lekeres kellett", len(cli.calls) == 2, str(len(cli.calls)))

cli404 = FakeClient(FakeResponse(404, ""))
check("a 404 nem hiba: nincs ujraprobalkozas",
      scanner(cli404, stop_on_error=True)._allowed(U + "/a.pdf") and len(cli404.calls) == 1)

print("\n--- 13. Elerhetetlen robots.txt: leallas pipaval ---")
LOG.clear()
cli = FakeClient(FakeResponse(503, ""))
sc = scanner(cli, stop_on_error=True)
try:
    sc._allowed(U + "/a.pdf")
    check("5xx + pipa -> RobotsUnavailable", False)
except RobotsUnavailable as exc:
    check("5xx + pipa -> RobotsUnavailable", True)
    check("a naplouzenet egyertelmu", "leáll" in str(exc) and "503" in str(exc), str(exc))

LOG.clear()
cli = FakeClient(letolto.httpx.ConnectTimeout("ido"))
try:
    scanner(cli, stop_on_error=True)._allowed(U + "/a.pdf")
    check("a halozati hiba is leallit", False)
except RobotsUnavailable:
    check("a halozati hiba is leallit", True)
check("halozati hibanal is ujraprobalkozik", len(cli.calls) == letolto.ROBOTS_TRIES)

LOG.clear()
cli = FakeClient(FakeResponse(503, ""))
res = scanner(cli, stop_on_error=True).run()
check("az atvizsgalas nem ad talalatot", not res.all_urls)
check("a naplo megmondja, miert allt le",
      any("leáll" in m for m in LOG), LOG[-1] if LOG else "")

LOG.clear()
cli = FakeClient(FakeResponse(503, ""))
scanner(cli).run()
check("pipa nelkul a naplo a folytatast irja",
      any("folytatódik" in m for m in LOG), LOG[-1] if LOG else "")

print("\n--- 14. Leallitas a probalkozasok kozben ---")
ev = threading.Event()
ev.set()
cli = FakeClient(FakeResponse(503, ""))
try:
    scanner(cli, stop_on_error=True, stop=ev)._allowed(U + "/a.pdf")
    check("a Leallitas gomb kozbeszol", False)
except Cancelled:
    check("a Leallitas gomb kozbeszol", True)
check("nem varta ki a tobbi probalkozast", len(cli.calls) == 1, str(len(cli.calls)))


print("\n--- 15. Tulsagosan nagy robots.txt ---")
hosszu = "User-agent: *\nDisallow: /elso\n" + "# toltelek sor\n" * 60000 + "Disallow: /utolso"
check("a toltelek tenyleg atlepi a hatart", len(hosszu) > letolto.ROBOTS_MAX_TEXT)
sc = scanner(FakeClient(FakeResponse(200, hosszu)))
check("a hatar elotti szabaly ervenyes", not sc._allowed(U + "/elso/x"))
check("a hataron tuli resz mar nem", sc._allowed(U + "/utolso"))

csonka = "User-agent: *\nDisallow: /jo\n" + "#" * (letolto.ROBOTS_MAX_TEXT + 10) + "\nDisallow: /nem"
sc = scanner(FakeClient(FakeResponse(200, csonka)))
check("a fel sor nem valik szabalya", sc._allowed(U + "/nem"))
check("az ep sorok viszont megmaradnak", not sc._allowed(U + "/jo"))


print("\n--- 16. Valodi kiszolgaloval, valodi HTTP-vel ---")
srv = testsrv.serve(8811)
BASE = "http://127.0.0.1:8811/"
client = letolto.make_client(4)


def valos_scan(stop_on_error=False):
    cfg = ScanConfig(root=BASE, depth=1, same_host=True, respect_robots=True,
                     stop_on_robots_error=stop_on_error)
    LOG.clear()
    return Scanner(cfg, client, lambda m: LOG.append(m), threading.Event()).run()


testsrv.H.robots_status = 200
testsrv.H.robots_body = "User-agent: *\nDisallow: /files/\nAllow: /files/a.bin\n"
res = valos_scan()
nevek = [u.rsplit("/", 1)[-1] for u in res.all_urls]
check("az Allow kivetele atjon a valos halozaton is", "a.bin" in nevek, str(sorted(nevek)))
check("a tiltott tarsai viszont nem", "b.bin" not in nevek and "d.pdf" not in nevek,
      str(sorted(nevek)))
check("a mas mappaban levo fajl megmarad", any(n.startswith("e.bin") for n in nevek),
      str(sorted(nevek)))

testsrv.H.robots_status = 503
res = valos_scan(stop_on_error=True)
check("valos 5xx + pipa -> nincs talalat", not res.all_urls, str(res.all_urls[:3]))
check("es a naplo megmondja, miert", any("leáll" in m for m in LOG), str(LOG[-1:]))

res = valos_scan(stop_on_error=False)
check("valos 5xx pipa nelkul -> folytatja", len(res.all_urls) > 5, str(len(res.all_urls)))
check("de a naplo jelzi a gondot", any("folytatódik" in m for m in LOG), str(LOG[-1:]))

testsrv.H.robots_status = 404
res = valos_scan(stop_on_error=True)
check("hianyzo robots.txt valos halozaton is rendben", len(res.all_urls) > 5)

client.close()
srv.shutdown()


print("\n=== OSSZEGZES: %d / %d teszt sikeres ===" % (sum(R), len(R)))
sys.exit(0 if all(R) else 1)
