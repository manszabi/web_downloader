# Weboldal-letöltő – rövid használat

## Telepítés és indítás (Windows)

Tedd az `inditas.bat` és a `letolto.py` fájlt egy mappába, és kattints duplán a `.bat`-ra.
Az első indításkor létrehoz egy `.venv` mappát, és telepíti az egyetlen függőséget (`httpx`).
Python 3.11 vagy újabb szükséges.

Más rendszeren: `pip install httpx`, majd `python letolto.py`.

## A felület sorrendje

1. **URL** – a kiindulási oldal címe.
2. **Célkönyvtár** – a program megjegyzi, és következő indításkor magától visszatölti az itt
   félbemaradt munkát.
3. **Átvizsgálás** gomb. Ez most már *minden* találatot összegyűjt, majd háttérben ellenőrzi,
   mi van már meg a lemezen.
4. **Talált kiterjesztések** panel – kipipálod, mi kell. Az „Összes" / „Egyik sem" gomb az
   egész csoportra hat. A kiterjesztés nélküli fájlok külön csoportba kerülnek. A pipák és az
   alatta lévő **Kiterjesztések** mező mindkét irányban követik egymást.
5. **Fájllista** – soronként is pipálható: kattints a ✓ oszlopra, vagy nyomj szóközt a
   kijelölt sorokon. Fölötte „Összes kijelölése" / „Kijelölés törlése".
6. **Indítás / Folytatás** – letölti a kipipált fájlokat.

### Automatikus pipálás

| A fájl állapota a célkönyvtárban | Mi történik |
|---|---|
| nincs meg | ki van pipálva → letöltendő |
| megvan, de sérült vagy csonka | ki van pipálva → újratöltendő |
| megvan és ép | **lekerül róla a pipa**, „kész" státuszt kap |

Ha egy ép fájlt kézzel mégis kipipálsz, az Indításnál rákérdez: **Igen / Nem / Összes**.
Az „Összes" a többi ilyen fájlra is igent mond, és nem kérdez újra.

### Kiterjesztések mező és HTML

A kézi mező üresen hagyva minden kiterjesztést jelent. A HTML-lapokat csak akkor tölti le,
ha a **HTML letöltése** be van pipálva – így a bejárt oldalak nem árasztják el a listát.

A **Talált kiterjesztések** panel és a **Kiterjesztések** mező mindig egyben mozog:

* ha a panelen pipálsz, a mezőbe magától beíródik a kipipált kiterjesztések listája
  (pl. `pdf, png`), mintha kézzel gépelted volna be – a html nem a mezőbe kerül, hanem a
  **HTML letöltése** kapcsolóra;
* ha a mezőbe írsz vagy a HTML-kapcsolót állítod, a panelen igazodnak a pipák (és velük a
  fájllista kijelölése is) – a gépelés után kis szünettel, hogy ne kapkodjon minden leütésre;
* ha semmi sincs kipipálva, a mezőben `(egyik sem)` látszik, mert az üres mező „minden
  kiterjesztést" jelentene; a mezőt üresre törölve rögtön vissza is kapod a „mindent";
* a kiterjesztés nélküli csoport neve `(nincs kiterjesztés)`, ez így is írható a mezőbe.

### Beállítások mappája

A **Beállítások mappája** gomb új fájlkezelő-ablakot nyit a beállításfájl helyén (Windowson
`%APPDATA%\PyLetolto\beallitasok.json`, máshol a home könyvtárban `.letolto_beallitasok.json`),
és Windowson rögtön ki is jelöli a fájlt. Ha még nem lenne meg, a gomb létrehozza.
Ebben a fájlban őrződik az URL, a célkönyvtár, a kiterjesztés-szűrő, a mélység, a szálszám
és a többi kapcsoló.

### Egyéb beállítások

* **Mélység** – 0: csak a megadott oldal linkjei; 1 vagy több: al-oldalak bejárása is.
* **Szálak** – letöltés közben is állítható, azonnal hat. Csökkentéskor a futó fájlok
  befejeződnek, csak utána lép ki a fölös szál.
* **Meglévő fájl** – *kihagyás* / *méret-ellenőrzés* (alapértelmezett) / *újratöltés*.
* **robots.txt betartása** – ajánlott bekapcsolva hagyni. A szabályokat az RFC 9309
  szerint értelmezi: a `*` és a záró `$` joker is érvényes, ütközéskor a leghosszabb minta
  dönt, azonos hossznál pedig az `Allow` – így az `Allow` felül tudja írni a tágabb
  `Disallow`-ot.
* **5xx hibánál leáll** – mi legyen, ha maga a `robots.txt` nem érhető el (a kiszolgáló 5xx-et
  ad, vagy elszáll a kapcsolat). A program ilyenkor háromszor próbálkozik, egyre hosszabb
  szünettel; ha egyik sem sikerül, **kipipálva** leállítja az átvizsgálást (nem tudjuk, mit
  tiltott volna az oldal – ez az RFC 9309 szigorú olvasata), **pipa nélkül** pedig
  naplóüzenettel folytatja. A hiányzó `robots.txt` (404) nem tartozik ide: az azt jelenti,
  hogy nincs tiltás.

## Naplófájl

A futás eseményei a beállítások mellé, rotáló naplófájlba kerülnek: Windowson
`%APPDATA%\PyLetolto\naplo.log`, máshol `~/.letolto_naplo.log`. 1 MB-onként fordul, 3 mentést
tart meg (`naplo.log.1` … `naplo.log.3`), tehát legfeljebb ~4 MB-ot foglal. Benne van az
indulás és a kilépés, a célkönyvtár, az átvizsgálás paraméterei és eredménye, fájlonként a
forrás cím és a célfájl, továbbá minden hiba – az újrapróbálkozások és a `robots.txt` gondjai
is. A pontos helyét a *Beállítások mappája* gomb írja ki.

## Megszakítás és folytatás

A *Szünet* és a *Leállítás* is folytatható állapotot hagy maga után; a részfájlok `.part`
kiterjesztéssel készülnek, a haladást a célkönyvtárban lévő `_letoltes_allapot.json` őrzi.
Áramszünet vagy programösszeomlás után legfeljebb néhány másodpercnyi letöltés vész el,
a folytatás onnan indul.

## Parancssori mód

```
python letolto.py https://pelda.hu/ -o C:\letoltesek -e pdf,zip -d 1 -t 8
python letolto.py --no-gui -o C:\letoltesek          # csak a félbemaradtak folytatása
```

Kapcsolók: `--html`, `--any-host`, `--ignore-robots`, `--robots-5xx-stop`,
`--meglevo {kihagyás,méret-ellenőrzés,újratöltés}`, `-t/--threads`, `-d/--depth`.

## Tesztek

```
python test_letolto.py      python test_valogatas.py    python test_meglevo.py
python test_epseg.py        python test_szalak.py       python test_gui.py
python test_terheles.py     python test_osszeomlas.py   python test_windows.py
python test_gui_valogatas.py  python test_gui_szinkron.py  python test_robots.py
python test_naplo.py        python test_gui_robots.py
```

A GUI-tesztek valódi ablakot nyitnak. A `testsrv.py` a tesztekhez tartozó helyi
kiszolgáló – a program működéséhez nem kell.
