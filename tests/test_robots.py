"""A robots.txt-szabalyok tesztje az RFC 9309 szerint."""
import sys
import threading
from pathlib import Path

# A letolto.py lehet a teszt mellett vagy egy szinttel feljebb (tests/ mappa).
_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent)]
from letolto import ScanConfig, Scanner, RobotsRules

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


class FakeClient:
    """Csak a robots.txt lekereset szolgalja ki; szamolja a hivasokat."""

    def __init__(self, status_code=200, text=""):
        self.resp = FakeResponse(status_code, text)
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        return self.resp


def scanner(client, respect=True):
    cfg = ScanConfig(root=U + "/", depth=1, same_host=True, respect_robots=respect)
    return Scanner(cfg, client, lambda _m: None, threading.Event())

print("\n--- 11. A Scanner hasznalata ---")
cli = FakeClient(200, "User-agent: *\nDisallow: /admin/\nAllow: /admin/nyilvanos")
sc = scanner(cli)
check("a Scanner tiltja a tiltottat", not sc._allowed(U + "/admin/titok"))
check("a Scanner atengedi az Allow kivetelt", sc._allowed(U + "/admin/nyilvanos/a.pdf"))
check("a robots.txt kiszolgalonkent egyszer tolt le", len(cli.calls) == 1, str(cli.calls))

cli404 = FakeClient(404, "")
check("hianyzo robots.txt -> minden szabad", scanner(cli404)._allowed(U + "/admin/titok"))

cli_off = FakeClient(200, "User-agent: *\nDisallow: /")
check("kikapcsolt robots -> nincs letoltes sem",
      scanner(cli_off, respect=False)._allowed(U + "/admin/titok") and not cli_off.calls)

print("\n=== OSSZEGZES: %d / %d teszt sikeres ===" % (sum(R), len(R)))
sys.exit(0 if all(R) else 1)
