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
kiterjesztéseket darabszámmal. Kipipálod, mi kell — csoportosan vagy fájlonként.

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
| `TESZTJEGYZOKONYV.md` | a fejlesztés során talált hibák, mérések, ismert korlátok |
| `testsrv.py` | a tesztekhez tartozó helyi kiszolgáló |
| `test_*.py` | tesztek (a futtatáshoz nem kellenek) |

---

## Tesztek

```bash
python test_letolto.py      python test_valogatas.py     python test_meglevo.py
python test_epseg.py        python test_szalak.py        python test_gui.py
python test_terheles.py     python test_osszeomlas.py    python test_windows.py
python test_gui_valogatas.py
```

Jelenlegi állás: **209 teszt, mind sikeres**. A GUI-tesztek valódi ablakot nyitnak.
A `TESZTJEGYZOKONYV.md` tartalmazza a mérési eredményeket és az ismert korlátokat.

---

## Felelős használat

A program alapértelmezés szerint betartja a `robots.txt` tiltásait. A szálszámot érdemes
barátságos szinten (4–8) tartani, hogy ne terheld túl a kiszolgálót. A letöltött tartalom
felhasználására a forrásoldal feltételei vonatkoznak.
