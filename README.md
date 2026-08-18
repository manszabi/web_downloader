# web_downloader

Weboldal-fájlletöltő grafikus felülettel: **többszálú**, **megszakítható és folytatható**,
és átvizsgálás után kiválogathatod, mi kell.

Egyetlen függősége a [httpx](https://www.python-httpx.org/); a HTML-elemzést és a felületet
a Python szabványkönyvtára végzi.

---

## Telepítés

### Windows

Töltsd le a repót, majd kattints duplán az `inditas.bat` fájlra. Az első indításkor létrehoz
egy `.venv` mappát, telepíti a `httpx`-et, és elindítja a programot.

### Más rendszeren

```bash
pip install httpx
python letolto.py
```

Python **3.11** vagy újabb szükséges.

---

## Mit tud

**Folytatás.** A részletek `.part` fájlba készülnek, a haladást a célkönyvtárban lévő
`_letoltes_allapot.json` őrzi. A folytatás HTTP `Range` kéréssel történik, `If-Range`
validátorral: ha a fájl közben megváltozott a szerveren, a letöltés tisztán újraindul
ahelyett, hogy két verzió darabjai állnának össze. Áramszünet vagy programösszeomlás után
legfeljebb néhány másodpercnyi letöltés vész el, és a program indításkor magától felkínálja
a folytatást.

**Válogatás.** Az átvizsgálás minden találatot összegyűjt, és megmutatja a talált
kiterjesztéseket darabszámmal. Kipipálod, mi kell — csoportosan vagy fájlonként. A **Talált
kiterjesztések** panel és a **Kiterjesztések** mező mindkét irányban követi egymást: amit a
panelen kipipálsz, az beíródik a mezőbe (a html a saját kapcsolójára kerül), és amit a mezőbe
írsz, az a panelen pipálódik ki.

**Beállítások mappája.** Egy gomb a felületen új fájlkezelő-ablakot nyit a beállításfájl
helyén (Windowson `%APPDATA%\PyLetolto\`, ott mindjárt ki is jelöli a fájlt).

**Automatikus pipálás épség szerint.**

| A fájl állapota a célkönyvtárban | Mi történik |
|---|---|
| nincs meg | ki van pipálva → letöltendő |
| megvan, de sérült vagy csonka | ki van pipálva → újratöltendő |
| megvan és ép | lekerül róla a pipa, „kész" státuszt kap |

Ha egy ép fájlt kézzel mégis kipipálsz, az Indításnál rákérdez: **Igen / Nem / Összes**.

**Menet közben állítható szálszám.** A léptető átállítása azonnal hat. Csökkentéskor a fölös
szálak befejezik a folyamatban lévő fájlt, és csak utána lépnek ki — így letöltött bájt nem
vész kárba.

**Parancssori mód** ütemezett futtatáshoz:

```bash
python letolto.py https://pelda.hu/ -o ./letoltesek -e pdf,zip -d 1 -t 8
python letolto.py --no-gui -o ./letoltesek      # csak a félbemaradtak folytatása
```

Kapcsolók: `--html`, `--any-host`, `--ignore-robots`,
`--meglevo {kihagyás,méret-ellenőrzés,újratöltés}`, `-t/--threads`, `-d/--depth`.

---

## Fájlok

| Fájl | Mi ez |
|---|---|
| `letolto.py` | maga a program (GUI + parancssor) |
| `inditas.bat` | Windows-indító, függőség-ellenőrzéssel |
| `HASZNALAT.md` | rövid használati útmutató |
| `ruff.toml` | a lint rögzített beállításai (a program futtatásához nem kell) |
| `tests/` | tesztek és a hozzájuk tartozó helyi kiszolgáló (a futtatáshoz nem kellenek) |
| `tests/TESZTJEGYZOKONYV.md` | a fejlesztés során talált hibák, mérések, ismert korlátok |

---

## Tesztek

```bash
python tests/test_letolto.py       python tests/test_valogatas.py
python tests/test_epseg.py         python tests/test_meglevo.py
python tests/test_szalak.py        python tests/test_gui.py
python tests/test_terheles.py      python tests/test_osszeomlas.py
python tests/test_windows.py       python tests/test_gui_valogatas.py
python tests/test_gui_szinkron.py
```

Jelenlegi állás: **302 teszt, mind sikeres**. A GUI-tesztek valódi ablakot nyitnak.
A `tests/TESZTJEGYZOKONYV.md` tartalmazza a mérési eredményeket és az ismert korlátokat.

---

## Felelős használat

A program alapértelmezés szerint betartja a `robots.txt` tiltásait. A szálszámot érdemes
barátságos szinten (4–8) tartani, hogy ne terheld túl a kiszolgálót. A letöltött tartalom
felhasználására a forrásoldal feltételei vonatkoznak.
