# web_downloader

[![ellenőrzés](https://github.com/manszabi/web_downloader/actions/workflows/ellenorzes.yml/badge.svg)](https://github.com/manszabi/web_downloader/actions/workflows/ellenorzes.yml)

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

### Telepítés parancsként (nem kötelező)

Ha nem a repóból akarod indítani, telepíthető is – ekkor a `letolto` parancs bárhonnan
elérhető lesz (a `letolto-gui` Windowson konzolablak nélkül indul):

```bash
pipx install .          # vagy: pip install .
letolto --no-gui -o ./letoltesek https://pelda.hu
```

Fejlesztéshez a lint, a típusellenőrzés és a tesztek egy lépésben:

```bash
pip install -e ".[fejlesztes]"
```

---

## Mit tud

**Folytatás.** A részletek `.part` fájlba készülnek, a haladást a célkönyvtárban lévő
`_letoltes_allapot.json` őrzi. A folytatás HTTP `Range` kéréssel történik, `If-Range`
validátorral: ha a fájl közben megváltozott a szerveren, a letöltés tisztán újraindul
ahelyett, hogy két verzió darabjai állnának össze. Áramszünet vagy programösszeomlás után
legfeljebb néhány másodpercnyi letöltés vész el, és a program indításkor magától felkínálja
a folytatást. A részfájl ötmásodpercenként `fsync`-cel a lemezre is kikerül, nem csak az
operációs rendszer gyorsítótárába – így nem csak a program összeomlását, hanem az áramszünetet
is túléli.

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

Kapcsolók: `--html`, `--any-host`, `--ignore-robots`, `--robots-5xx-stop`,
`--meglevo {kihagyás,méret-ellenőrzés,újratöltés}`, `-t/--threads`, `-d/--depth`.

---

## Fájlok

| Fájl | Mi ez |
|---|---|
| `letolto.py` | a program magja: átvizsgálás, letöltés, parancssori mód – ez az indítandó fájl |
| `letolto_gui.py` | a grafikus felület (tkinter); a mag lustán importálja, csak GUI módban |
| `inditas.bat` | Windows-indító, függőség-ellenőrzéssel |
| `HASZNALAT.md` | rövid használati útmutató |
| `pyproject.toml` | csomagleírás: ebből lesz a telepíthető `letolto` parancs |
| `ruff.toml` | a lint rögzített beállításai (a program futtatásához nem kell) |
| `tests/` | tesztek és a hozzájuk tartozó helyi kiszolgáló (a futtatáshoz nem kellenek) |
| `tests/TESZTJEGYZOKONYV.md` | a fejlesztés során talált hibák, mérések, ismert korlátok |

---

## Tesztek

A teljes csomag egy paranccsal, pytesttel:

```bash
pip install pytest
pytest                 # minden teszt
pytest -k robots       # csak a robots.txt tesztjei
pytest -k "not gui"    # ablak nélkül (kiszolgálón)
```

A szkriptek külön-külön is futtathatók, pytest nélkül:

```bash
python tests/test_letolto.py       python tests/test_valogatas.py
python tests/test_epseg.py         python tests/test_meglevo.py
python tests/test_szalak.py        python tests/test_gui.py
python tests/test_terheles.py      python tests/test_osszeomlas.py
python tests/test_windows.py       python tests/test_gui_valogatas.py
python tests/test_gui_szinkron.py  python tests/test_robots.py
python tests/test_naplo.py         python tests/test_gui_robots.py
python tests/test_hatekonysag.py
```

Minden feltöltésnél GitHub Actions is lefuttatja őket, **Linuxon és Windowson**, Python 3.11
és 3.13 alatt, a `ruff` és a szigorú `mypy` mellé (`.github/workflows/ellenorzes.yml`).

Jelenlegi állás: **431 teszt, mind sikeres** – Linuxon és Windowson egyaránt (a Windows-ág
első futásai három valódi, Linuxot feltételező tesztbeli hibát hoztak felszínre, lásd a
jegyzőkönyv 10. pontját). A GUI-tesztek valódi ablakot nyitnak.
A `tests/TESZTJEGYZOKONYV.md` tartalmazza a mérési eredményeket és az ismert korlátokat.

---

## Naplófájl

A program a beállítások mellé rotáló naplót ír, tehát nem nő korlátlanul:

| Rendszer | Hely |
|---|---|
| Windows | `%APPDATA%\PyLetolto\naplo.log` |
| Linux, macOS | `~/.letolto_naplo.log` |

A fájl 1 MB-onként fordul, és 3 mentést tart meg (`naplo.log.1` … `naplo.log.3`), vagyis a napló
összesen legfeljebb ~4 MB. UTF-8 kódolású, és minden sor tartalmazza az időt és a szál nevét:

```
2026-08-18 19:27:13  [dl]  Letöltés: http://pelda.hu/a.bin -> C:\letoltesek\pelda.hu\a.bin
2026-08-18 19:27:13  [dl]  Kész: pelda.hu/a.bin (293.0 KB)
```

Bekerül a program indulása és kilépése, a célkönyvtár és a szálszám, az átvizsgálás
paraméterei és eredménye, fájlonként a forrás cím és a célfájl, a kész letöltések, valamint
minden hiba és figyelmeztetés – az újrapróbálkozások, a méreteltérés miatti újratöltés és a
`robots.txt` gondjai (5xx, tiltás) is. A GUI *Beállítások mappája* gombja a napló helyét is
kiírja. Ha a fájl épp zárolt (Windowson víruskereső, megnyitott szerkesztő vagy egy másik
példány), a rotálás kimarad, de a naplózás nem áll le és a program sem hibázik el tőle.

---

## Felelős használat

A program alapértelmezés szerint betartja a `robots.txt` tiltásait, az RFC 9309
(Robots Exclusion Protocol) szabályai szerint: a `*` és a záró `$` joker is érvényes, és
ütköző sorok közül a leghosszabb minta dönt – azonos hossznál az `Allow` nyer. A fájlból
legfeljebb 512 KiB-ot olvasunk (az RFC 500 KiB-ot kér), így egy végtelen `robots.txt` sem viszi
el a memóriát.

Ha a `robots.txt` **nem érhető el** (5xx vagy hálózati hiba), a program háromszor újrapróbálja,
majd a beállítás dönt: a *5xx hibánál leáll* pipa (parancssorban `--robots-5xx-stop`) az RFC
szerinti szigorú olvasat, vagyis inkább nem jár be semmit; pipa nélkül – ez az alapértelmezés –
naplóüzenettel folytatja. A 4xx (nincs ilyen fájl) egyik esetben sem hiba: az azt jelenti, hogy
az oldal nem tiltott semmit. A szálszámot érdemes
barátságos szinten (4–8) tartani, hogy ne terheld túl a kiszolgálót. A letöltött tartalom
felhasználására a forrásoldal feltételei vonatkoznak.
