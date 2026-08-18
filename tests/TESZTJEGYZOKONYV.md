# Felülvizsgálat, hibajavítás és mérési jegyzőkönyv

Környezet: Python 3.12.3, httpx 0.28.1, Ubuntu 24.04. Minden mérés saját teszt-HTTP-szerverrel
(`testsrv.py`) készült, amely Range, ETag, gzip, „Range-t nem támogató" és lassítható végpontokat
kínál, hogy a hálózati hibák szimulálhatók legyenek.

---

## 1. Az eredeti verzióban talált hibák

| # | Hiba | Következmény | Bizonyíték |
|---|------|--------------|-----------|
| 1 | A scanner teljes válaszokat olvasott a memóriába (`r.text`), a Content-Type ellenőrzése előtt | Egy 5 MB-os, kiterjesztés nélküli hivatkozás 10,2 MB memóriacsúcsot okozott; nagy fájloknál gigabájtokat is jelenthet | `tracemalloc` mérés: csúcs 10,2 MB |
| 2 | `bs4 + html.parser` a hivatkozások kigyűjtésére | A leglassabb és legpazarlóbb elérhető kombináció | lásd 2. pont |
| 3 | Nem kérte az `Accept-Encoding: identity`-t | Tömörítve küldő szervernél a lemezen lévő bájtszám (kicsomagolt) és a Range-eltolás (tömörített) eltér → **néma fájlsérülés** folytatáskor | elvi; a tesztszerveren azért nem jött elő, mert az eldobta a Range-t |
| 4 | Hiányzott az `If-Range` validátor | Ha a fájl közben megváltozik a szerveren, két különböző verzió darabjai állnak össze | MDN Range-requests ajánlás |
| 5 | Windows-on foglalt fájlnevek (`CON.txt`, `NUL`) | A fájl Windowson **nem hozható létre** – a Microsoft dokumentációja szerint kiterjesztéssel együtt is foglalt | teszt: `['127.0.0.1_8765/files/CON.txt']` |
| 6 | Szünet közben a státusz „letöltés" maradt | Félrevezető felület | teszt C) |
| 7 | Nem volt újrapróbálkozás | Egyetlen átmeneti hálózati hiba véglegesen elbukta a fájlt | – |
| 8 | `_update_total` 150 ms-enként végigjárta az egész listát | 20 000 elemnél másodpercenként ~130 000 fölösleges művelet | kódelemzés |
| 9 | Szálanként külön `requests.Session` | Nincs kapcsolat-újrahasznosítás a szálak között | kódelemzés |
| 10 | Korlátlanul növő naplóablak | Hosszú futásnál memóriaszivárgás a GUI-ban | kódelemzés |
| 11 | Leállításkor minden hátralévő munka elindult, csak hogy azonnal kivételt dobjon | Lassú leállás, fölösleges eseményáradat | kódelemzés |
| 17 | Újraindítás után a program a **saját, hibátlanul letöltött fájljaira** is rákérdezett volna a felülírásnál, mert a kész elemekről nem került le a pipa | Az Indítás után modális kérdések sorozata olyan fájlokra, amelyeket senki nem akart újratölteni; a GUI-teszt emiatt végtelenül várt | a repó `tests/` szerkezetében futtatva derült ki |
| 16 | Az átvizsgálás nem vette figyelembe, mi van már meg a lemezen: a felhasználónak kellett kézzel kipipálgatnia a már meglévőket | Ismételt futtatásnál könnyű volt fölöslegesen újratölteni, vagy épp kihagyni egy sérült fájlt | tervezési hiányosság |
| 15 | Az átvizsgálás a kézi kiterjesztés-szűrő alapján **eldobta** a nem kért találatokat | A felhasználó nem is látta, mi van még az oldalon; szűrőváltáshoz újra kellett vizsgálni | tervezési hiányosság |
| 19 | A DPI-javítás felénél a beillesztés nem illeszkedett, de a másik fele igen: `self.scale` definiálatlanul maradt volna | A GUI azonnal összeomlott volna induláskor | a saját ellenőrző futás fogta meg, mielőtt kiadtam volna |
| 18 | `os.replace` újrapróbálkozás nélkül | Windowson a víruskereső / keresőindexelő pillanatnyi zárolása `PermissionError`-ral eldobta a kész fájlt | dokumentált Windows-jelenség, teszttel szimulálva |
| 17 | Az útvonalhossz-korlát csak a *relatív* útra vonatkozott | Hosszú célkönyvtárnál (pl. `C:\Users\...\Documents\...`) a teljes út átléphette a Windows 260 karakteres MAX_PATH korlátját | mérés: 12 szintű URL + hosszú célmappa |
| 16 | A névütközést kis-nagybetű-érzékenyen vizsgálta | Windowson a `Jelentes.PDF` és a `jelentes.pdf` **ugyanaz a fájl** – az egyik felülírta volna a másikat | teszt: két URL, egy fájl |
| 15 | A parancssori kimenetben `·` és `…` szerepelt | Magyar Windows-konzolon (cp852) fájlba irányítva `UnicodeEncodeError` | kódlap-ellenőrzés minden üzenetre |
| 14 | A célkönyvtárban már meglévő fájlt pusztán a *létezése* alapján fogadta el késznek | Egy máshonnan odakerült **csonka fájl** véglegesen késznek látszott, és soha nem töltődött le rendesen | teszt: 100 000 bájtos csonka fájl „kész" státuszt kapott a 300 000 helyett |
| 13 | A szálszám a `ThreadPoolExecutor` létrehozásakor rögzült; a léptető átállítása futás közben nem csinált semmit | A felhasználó azt hihette, gyorsított, közben nem történt semmi | mérés: 2→8 állítás után is 2 munkásszál |
| 12 | Összeomlás után a GUI üresen indult: az állapot megvolt a lemezen, de csak külön gombnyomásra jelent meg | A felhasználó azt hihette, elveszett a munkája, és elölről kezdte | teszt: indulás után 0 sor a táblázatban |

## 2. HTML-elemzők mérése (1,17 MB HTML, 10 000 hivatkozás)

Valós RSS-csúcs, alfolyamatonként mérve:

| Megoldás | Idő | Többletmemória |
|---|---|---|
| **stdlib `HTMLParser` (a választott)** | **182 ms** | **+0,0 MB** |
| `bs4` + `html.parser` (az eredeti) | 1364 ms | +29,4 MB |
| `bs4` + `lxml` | 500 ms | +30,0 MB |
| `bs4` + `lxml` + `SoupStrainer` | ~1278 ms* | +9,3 MB* |
| `lxml.html.iterlinks` | 119 ms | +13,6 MB |
| `selectolax` (lexbor) | 51 ms | +14,8 MB |

\*tracemalloc-kal mérve. A `selectolax` a leggyorsabb, de C-fordítású külső csomagot igényel;
a szabványkönyvtári elemző 7,5-szer gyorsabb az eredetinél, **nem épít DOM-ot**, és nulla
függőséget igényel. Ezért lett ez az alapértelmezett.

## 3. Írási puffer mérése (200 MB kiírása)

| Puffer | Sebesség |
|---|---|
| `buffering=0` | 137 MB/s |
| **256 KB (a választott)** | **547 MB/s** |
| 1 MB (az első verzióban) | 428 MB/s |
| 8 MB | 453 MB/s |

A 256 KB egyszerre gyorsabb *és* biztonságosabb: `kill -9` esetén kevesebb adat vész el.
Mellé 5 másodpercenként explicit `flush()` került.

## 4. Hálózati adagolás

A `iter_bytes(256 KB)` a mérés szerint 256 KB-ot visszatart, mielőtt átadná: lassú vonalon
minden megszakításkor ennyi haladás veszne el. Paraméter nélküli `iter_bytes()` esetén az adat
azonnal, ~16 KB-os adagokban érkezik – a rendszerhívások számát az írási puffer fogja össze.
Ez a 12-szeres megszakításos teszten mérhető: a részfájl körönként nő
(114 KB → 229 KB → … → 1,36 MB), az első verzió beállításával viszont végig 0 bájt maradt.

## 5. Végleges tesztek

```
Funkcionális teszt (test_letolto.py)            30 / 30
Épség és felülírás (test_epseg.py, Xvfb)       28 / 28
Válogatás, kiterjesztések (test_valogatas.py)  25 / 25
GUI-válogatás (test_gui_valogatas.py, Xvfb)    19 / 19
GUI-szinkron, beállítások (test_gui_szinkron)  33 / 33
Élő szálszám-változtatás (test_szalak.py)      18 / 18
Meglévő fájlok (test_meglevo.py)               16 / 16
GUI végponttól végpontig (test_gui.py, Xvfb)   15 / 15
Terhelés és összeomlás (test_terheles.py)      14 / 14
Összeomlás utáni folytatás (test_osszeomlas)   10 / 10
Windows-specifikus ellenőrzések (test_windows)  34 / 34
-------------------------------------------------------
Összesen                                      242 / 242
ruff (E,F,W,B,UP,SIM,C4,RUF,PL)        All checks passed
mypy                                   Success: no issues found
```

Kiemelt esetek:

* **12 egymást követő megszakítás** ugyanazon az 5 MB-os fájlon → a végeredmény bitre azonos
  (MD5-egyezés), a részfájl minden körben nőtt.
* **`kill -9` letöltés közben** → 1 065 KB megmaradt a lemezen, az új példány pontosan onnan
  folytatta, a végeredmény ép.
* **`kill -9` után a GUI újraindítása** → gombnyomás nélkül megjelenik mind a 3 fájl, az
  állapotsor jelzi a folytathatót, az Indítás pedig a részfájltól viszi tovább; a végeredmény
  mindhárom fájlnál bitre azonos.
* **Sérült állapotfájl** (`{ ez nem json ]]`) → nem omlik össze, üres állapottal indul.
* **20 000 elem**: +14,1 MB memória (~1 KB/elem), felvétel 0,58 s, mentés 0,11 s, visszatöltés 0,42 s.
* **16 szál** párhuzamosan: +3,0 MB memória, minden fájl kész.
* **Szálszám-kapcsolgatás**: 6 gyors váltás (1→6→1→12→3→16→2) 48 fájl letöltése közben –
  minden fájl elkészült, nem maradt elárvult szál (`_active == 0`) és nem maradt `.part`.
* **GUI 2000 sorral**: felvétel 83 ms, kirajzolás 0,48 s, ezután 20 frissítési kör 1 ms.
* **Átvitel**: 60 MB egy szálon 0,75 s alatt (80 MB/s, helyi hálózaton).

## 5.b Windows 11 specifikus ellenőrzések

Minden ellenőrzés a `test_windows.py`-ban fut, Linuxon is, a Windows-szabályok szimulálásával.

| Terület | Mit csinál a program |
|---|---|
| Foglalt eszköznevek | `CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9` – kiterjesztéssel együtt is (`CON.txt` -> `_CON.txt`). A Python 3.13+ `os.path.isreserved()` ellenőrzésével is egyeztetve. |
| Tiltott karakterek | `< > : " / \ | ? *` és a vezérlőkarakterek cseréje; záró pont és szóköz levágása (a Windows némán levágná, és eltérő nevet kapnál) |
| MAX_PATH (260) | A **teljes** út (célkönyvtár + relatív út) 240 karakter alatt marad; szükség esetén rövidít és rövid jelet fűz a névhez, a kiterjesztést megtartva. Ugyanígy viselkedik Linuxon is, hogy a mappa átvihető legyen. |
| Kis-/nagybetű | A névütközést kis-nagybetűre érzéketlenül vizsgálja, mert az NTFS sem tesz köztük különbséget |
| Alternatív adatfolyam (ADS) | A kettőspont cseréje megakadályozza, hogy `fajl.txt:stream` alakú név jöjjön létre |
| Fájlzárolás | `atomic_replace()` rövid, növekvő várakozással ötször újrapróbálja a cserét, ha víruskereső vagy indexelő fogja a fájlt |
| Nagy DPI | `SetProcessDpiAwareness(1)` a `Tk()` előtt, majd `tk scaling` és az ablak/oszlopszélességek arányos igazítása – Windows 11-en a 125-150%-os nagyítás az alapértelmezett új gépeken |
| Konzol kódlap | A parancssori kimenet minden karaktere ábrázolható cp852 és cp1250 kódlapon; ezen felül a kimenet UTF-8-ra van állítva `errors="replace"`-szel |
| Beállítások helye | Windowson `%APPDATA%\PyLetolto\beallitasok.json`, máshol a home könyvtár |
| Indító `.bat` | Tiszta ASCII, CRLF sorvégek, Python 3.11+ ellenőrzés, `.venv`, és jelzi, ha a rendszeren nincs bekapcsolva a hosszú útvonalak támogatása |

Végponttól végpontig futó ellenőrzés valós letöltéssel, Windows-ellenes nevekből:

```
127.0.0.1_8802/w/_CON.txt          <- CON.txt
127.0.0.1_8802/w/_nul.pdf          <- nul.pdf
127.0.0.1_8802/w/pont/a.txt        <- "pont./a.txt"
127.0.0.1_8802/w/szokoz/b.txt      <- "szokoz /b.txt"
127.0.0.1_8802/w/Nagy.PDF          <- Nagy.PDF
127.0.0.1_8802/w/nagy_66b0e987.pdf <- nagy.pdf (ütközne Windowson)
127.0.0.1_8802/w/hosszu...hos_a2324068.dat  <- 150 karakteres név
PROBLEMAK: nincs
```

## 6. Mit tud most, amit korábban nem

* `If-Range` + ETag/Last-Modified: megváltozott fájl esetén tiszta újratöltés, nem sérült keverék.
* `Accept-Encoding: identity`, és ha a szerver mégis tömörít, a fájl automatikusan
  „nem folytatható" jelölést kap → a következő próbálkozás elölről kezdi ahelyett, hogy sérülne.
* 416-os válasz kezelése: ha a részfájl valójában teljes, egyszerűen véglegesíti.
* Újrapróbálkozás exponenciális várakozással (4 kísérlet), `Retry-After` figyelembevételével.
* Windows-biztos fájlnevek: foglalt eszköznevek, tiltott karakterek, záró pont/szóköz,
  komponens- és teljes úthossz korlátozása, névütközések feloldása.
* Sebesség- és hátralévőidő-kijelzés, „Mappa megnyitása" gomb, korlátozott naplóablak.
* Parancssori mód (`--no-gui`), így szerveren, ütemezve is használható.
* **Épség szerinti automatikus pipálás**: az átvizsgálás után a program háttérben, a beállított
  szálszámmal párhuzamosan megnézi, mi van már meg a célkönyvtárban. Ami ép, arról lekerül a
  pipa (és „kész" státuszt kap); ami hiányzik vagy sérült, az bepipálva marad. Hálózati kérés
  csak a ténylegesen létező fájloknál indul, és ott is csak akkor, ha a méret még nincs igazolva.
* **Felülírás megerősítése**: ha egy már meglévő, ép fájlt kézzel kipipálsz, az Indításkor
  modális ablak kérdez rá - **Igen / Nem / Összes**. Az „Összes" a többi kijelölt fájlra is
  igent mond, és nem kérdez újra; a „Nem" leveszi a pipát, a meglévő fájl marad. Az ablak
  bezárása és az Escape is „nem"-nek számít. A felülírás nem törli előre a régi fájlt: az új
  példány `.part` fájlba készül, és csak a végén cserélődik.
* **Válogatás átvizsgálás után**: a bejárás mostantól *minden* találatot összegyűjt, szűrés
  nélkül, és a felület mutatja a talált kiterjesztéseket darabszámmal (`pdf (12)`, `bin (5)`,
  `(nincs kiterjesztés) (1)`, `html (3)`). Kipipálható, mi kell; az „Összes" / „Egyik sem"
  gombok az egészre hatnak. A fájllistában soronként is van pipa (kattintás a ✓ oszlopra vagy
  szóköz a kijelölt sorokon), fölötte „Összes kijelölése" / „Kijelölés törlése" gombokkal.
  A kijelölés az állapotfájlba kerül, tehát újraindítás után megmarad.
* **HTML-lapok külön kapcsolón**: a kézi kiterjesztés-mező üresen hagyva minden kiterjesztést
  jelent, a HTML-lapokat viszont csak akkor tölti le, ha a „HTML letöltése" be van pipálva -
  így nem árasztják el a listát a bejárt oldalak, de egy kattintással kérhetők.
* **A kiterjesztés-panel és a kézi mező összehangolása**: a „Talált kiterjesztések" pipái és
  az alattuk lévő „Kiterjesztések" mező mindkét irányban követik egymást. Panelen pipálva a
  mezőbe magától beíródik a kipipált címkék listája (`pdf, png`), a html pedig a „HTML
  letöltése" kapcsolóra kerül; a mezőbe gépelve (350 ms szünet után, hogy ne fusson minden
  leütésre) a panel pipái és velük a fájllista kijelölése igazodik. A körkörös felülírást
  közös őrjelző akadályozza meg. Az üres mező továbbra is „minden kiterjesztést" jelent,
  ezért az „egyik sem" állapotnak saját jelölése van: `(egyik sem)`.
* **Beállítások mappája gomb**: külön fájlkezelő-ablakot nyit a beállításfájl helyén, és
  Windowson az Intéző `/select` kapcsolójával rögtön ki is jelöli a fájlt. Ha a fájl még nem
  létezik, a gomb kiírja az aktuális beállításokat, hogy legyen mit megnézni.
* **Meglévő fájlok házirendje** (`Meglévő fájl:` legördülő, illetve `--meglevo` kapcsoló):
  * *kihagyás* – ami ott van, az kész (a régi viselkedés),
  * *méret-ellenőrzés* (alapértelmezett) – HEAD kéréssel összeveti a szerveri mérettel, és csak
    eltérés esetén tölt újra; ha mi töltöttük le és a hossz ismert, hálózati kérés sem kell,
  * *újratöltés* – mindig letölti.
  A régi fájl mindvégig a helyén marad, az új példány `.part` fájlba készül, és csak a végén
  cserélődik – így megszakadó újratöltés esetén sem marad üres kézzel a felhasználó.
  Mérés: 20 meglévő fájl ellenőrzése 0,17 s (letöltés nélkül).
* **Menet közben állítható szálszám**: a fix `ThreadPoolExecutor` helyére saját, dinamikusan
  méretezhető munkásszál-készlet került. A léptető átállítása 0,15 másodpercen belül hat:
  méréssel 2 -> 8 szál 0,4 s alatt, GUI-ból vezérelve 2 -> 10 párhuzamos fájl. Csökkentéskor a
  fölös szálak a folyamatban lévő fájlt még befejezik, így letöltött bájt nem vész kárba.
  A HTTP-kapcsolatkészlet a felső határhoz (32) van méretezve, hogy a növelés ne akadjon el.
* **Automatikus folytatás-felismerés**: induláskor (és a célkönyvtár megváltoztatásakor)
  magától beolvassa a korábbi állapotot, ha talál `_letoltes_allapot.json` fájlt, és kiírja,
  hány fájl folytatható. A beállításokat a `~/.letolto_beallitasok.json` őrzi, így újraindítás
  után a célkönyvtár és az URL is a helyén van.

## 7. Használati sorrend a felületen

1. **URL** és **célkönyvtár** megadása (az utóbbit a program megjegyzi).
2. **Átvizsgálás** - minden találatot összegyűjt, majd ellenőrzi a meglévőket.
3. A **talált kiterjesztések** panelen (vagy a vele szinkronban lévő **Kiterjesztések**
   mezőben) és a **fájllistában** a pipák igazítása.
4. **Indítás** - a már meglévő, mégis kipipált fájloknál rákérdez a felülírásra.

## 8. Ismert korlátok

* A `Content-Disposition` fejlécben megadott fájlnevet nem használja (az URL-ből képzi a nevet).
* A meglévő fájlok ellenőrzése **méret alapján** történik, nem tartalom (ellenőrzőösszeg) alapján:
  az azonos méretű, de eltérő tartalmú fájlt elfogadja. Tartalomellenőrzéshez a szervernek
  ellenőrzőösszeget kellene közölnie, amit a legtöbb nem tesz meg.
* Csak HTTP/1.1 (a HTTP/2 többszálú, streamelt letöltésnél a httpx-nél ismert kockázat).
* JavaScripttel betöltött tartalmat nem lát – ehhez böngészőmotor kellene.
* A robots.txt `Crawl-delay` direktíváját nem veszi figyelembe, csak a tiltásokat.
