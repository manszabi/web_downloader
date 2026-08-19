#!/usr/bin/env python3
"""Weboldal-fájlletöltő: többszálú, megszakítható és folytatható, GUI-val.

Fő jellemzők
------------
* Oldalátvizsgálás streamelt HTML-elemzéssel (nem épül DOM, konstans memória).
* Többszálú letöltés közös HTTP-kapcsolatkészlettel (keep-alive újrahasznosítás).
* Folytatás HTTP Range + If-Range validátorral: ha a fájl közben megváltozott a
  szerveren, a letöltés újraindul ahelyett, hogy sérült fájl állna össze.
* Az állapot minden pillanatban a lemezen van (.part fájlok + JSON napló),
  így áramszünet vagy összeomlás után is folytatható.
* GUI (tkinter) és parancssori mód (--no-gui) ugyanazzal a maggal.

Követelmény: Python 3.11+, httpx.
    pip install httpx
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from hashlib import blake2b
from html.parser import HTMLParser
from logging.handlers import RotatingFileHandler
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Final
from urllib.parse import unquote, urldefrag, urljoin, urlparse

# Futásidejű őr: a StrEnum és a match-case miatt 3.11 az alsó határ.
if sys.version_info < (3, 11):  # noqa: UP036
    raise SystemExit(f"Python 3.11 vagy újabb szükséges. Jelenlegi: {sys.version.split()[0]}")

try:
    import httpx
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("Hiányzó függőség. Telepítés:\n\n    pip install httpx\n") from exc

__version__ = "2.0"

log = logging.getLogger("letolto")

# --------------------------------------------------------------------------- #
#  Beállítások
# --------------------------------------------------------------------------- #

USER_AGENT: Final = f"PyLetolto/{__version__} (+https://example.invalid)"
FILE_BUFFER: Final = 256 * 1024        # írási puffer: mérve ez a leggyorsabb
FLUSH_SECONDS: Final = 5.0             # el összeomláskor (lásd a README mérését)
AUTOSAVE_SECONDS: Final = 3.0          # az állapotmentés alapüteme
AUTOSAVE_MAX_SECONDS: Final = 30.0     # ennél ritkábban a legnagyobb listánál sem
AUTOSAVE_PER_ITEM: Final = 0.0005      # elemenkénti ráadás (20 000 elem -> 13 mp)
STATE_FILE: Final = "_letoltes_allapot.json"
def _settings_path() -> Path:
    """Windowson a %APPDATA%, máshol a home könyvtár a szokásos hely."""
    appdata = os.environ.get("APPDATA")
    if sys.platform == "win32" and appdata:
        return Path(appdata) / "PyLetolto" / "beallitasok.json"
    return Path.home() / ".letolto_beallitasok.json"


def _log_path() -> Path:
    """A naplófájl a beállítások mellé kerül, ugyanazzal a névadási szokással."""
    appdata = os.environ.get("APPDATA")
    if sys.platform == "win32" and appdata:
        return Path(appdata) / "PyLetolto" / "naplo.log"
    return Path.home() / ".letolto_naplo.log"


SETTINGS_FILE: Final = _settings_path()
LOG_FILE: Final = _log_path()
LOG_MAX_BYTES: Final = 1024 * 1024     # ekkora naplófájlnál jön a rotálás (~10 ezer sor)
LOG_BACKUPS: Final = 3                 # naplo.log.1 .. .3 -> a napló összesen legfeljebb 4 MB
LOG_ROTATE_RETRY: Final = 60.0         # sikertelen rotálás után ennyi ideig nem próbáljuk újra
STATE_VERSION: Final = 2
MAX_RETRIES: Final = 4
MAX_RETRY_AFTER: Final = 30            # a Retry-After fejlécet ennyi másodpercre korlátozzuk
MAX_EXT_LEN: Final = 10                # ennél hosszabb utótagot nem tekintünk kiterjesztésnek
HTTP_OK: Final = 200
HTTP_PARTIAL: Final = 206
HTTP_BAD_REQUEST: Final = 400
HTTP_RANGE_NOT_SATISFIABLE: Final = 416
HTTP_SERVER_ERROR: Final = 500          # ettől fölfelé a kiszolgáló hibája
ROBOTS_TRIES: Final = 3                # a robots.txt lekérésének próbálkozásai
ROBOTS_RETRY_WAIT: Final = 1.0         # az újrapróbálkozások közti alapszünet (mp)
ROBOTS_MAX_TEXT: Final = 512 * 1024    # ennyi karakternél hosszabb robots.txt-t nem olvasunk
RETRY_STATUS: Final = frozenset({408, 425, 429, 500, 502, 503, 504})
HTML_EXTS: Final = frozenset({"", ".html", ".htm", ".xhtml", ".php", ".asp",
                              ".aspx", ".jsp", ".cgi", ".shtml"})
MAX_HTML_BYTES: Final = 8 * 1024 * 1024   # ennél nagyobb oldalt nem elemzünk
MAX_THREADS: Final = 32                # a felületen és a magban is ez a felső határ
SUPERVISOR_TICK: Final = 0.15          # ilyen sűrűn nézi a felügyelő a szálszámot
EXT_COLUMNS: Final = 6             # ennyi kiterjesztés fér egy sorba a panelen
UI_BATCH: Final = 400              # egy frissítési körben feldolgozott események száma
UI_TICK_MS: Final = 150            # a GUI frissítési üteme
EXT_SYNC_MS: Final = 350           # gépelés után ennyivel igazítjuk a panelt a szűrőhöz
UI_ITEM_THROTTLE: Final = 0.3      # egy fájl sorát legfeljebb ilyen sűrűn frissítjük
SPEED_WINDOW: Final = 0.5          # sebességmérés legrövidebb ablaka
LOG_MAX_LINES: Final = 500

# Windows-on foglalt eszköznevek (Microsoft: Naming Files, Paths, and Namespaces)
# A DOS-korból örökölt eszköznevek. A Microsoft dokumentációja a COM0/LPT0 nevet is
# idesorolja, a ¹²³ jeleket pedig számjegynek veszi a Windows; a CONIN$/CONOUT$ a
# konzol két félkész eszköze (a CPython ntpath.isreserved is számol velük).
_WIN_RESERVED: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{d}" for d in "0123456789¹²³"}
    | {f"LPT{d}" for d in "0123456789¹²³"}
)
_ILLEGAL_CHARS: Final = re.compile(r'[<>:"|?*\\/\x00-\x1f]')
_MAX_COMPONENT: Final = 100            # egy útvonalelem max hossza
MAX_ABS_PATH: Final = 240              # a teljes út korlátja: a Windows MAX_PATH (260) alatt
MAX_REL_PATH: Final = 240              # a célkönyvtáron belüli út ennél hosszabban lapul


class Status(StrEnum):
    PENDING = "várakozik"
    CHECK = "ellenőrzendő"        # a fájl megvan, de a mérete még nincs igazolva
    RUNNING = "letöltés"
    PAUSED = "szünetel"
    DONE = "kész"
    STOPPED = "megszakítva"
    ERROR = "hiba"


class Existing(StrEnum):
    """Mi történjék, ha a fájl már ott van a célkönyvtárban."""

    SKIP = "kihagyás"             # meglévő fájl = kész, kérdés nélkül
    VERIFY = "méret-ellenőrzés"   # HEAD kéréssel összeveti a szerveri mérettel
    REDOWNLOAD = "újratöltés"     # mindig letölti újra


class Cancelled(Exception):
    """A felhasználó leállította a munkát."""


class RobotsUnavailable(Exception):
    """A robots.txt nem érhető el, és a beállítás szerint ez teljes tiltás."""


class Retryable(Exception):
    """Átmeneti hiba, újrapróbálható."""


class DownloadError(Exception):
    """Végleges hiba egy adott fájlnál."""


def _brief(exc: BaseException) -> str:
    """Rövid, felületre való hibaszöveg (a httpx üzenetei többsorosak)."""
    text = str(exc).strip().splitlines()
    return (text[0] if text else type(exc).__name__)[:120]


# --------------------------------------------------------------------------- #
#  Naplófájl
# --------------------------------------------------------------------------- #

# Két naplózó dolgozik együtt. A "letolto" a program hangja: a figyelmeztetései
# parancssorban a képernyőre is kimennek. A "letolto.naplo" csak a fájlba ír
# (propagate=False), így oda részletesebben lehet naplózni, mint amennyi a
# képernyőn hasznos lenne. A rotáló kezelő mindkettőre felkerül, tehát a
# figyelmeztetések és a hibák sem maradnak ki a naplófájlból.
file_log = logging.getLogger("letolto.naplo")
file_log.propagate = False


class RotatingLog(RotatingFileHandler):
    """Rotáló naplófájl, amely a névcserén nem bukik el.

    Windowson a fájlt fogva tarthatja a víruskereső, egy megnyitott szerkesztő
    vagy a program egy másik példánya, és ilyenkor az átnevezés ``PermissionError``-ral
    elszáll. A rotálás nem ér annyit, hogy emiatt üzenetek vesszenek el vagy
    elszálljon a program: a hibát elnyeljük, a napló tovább nő a régi fájlban, és
    egy percig nem próbálkozunk újra (különben minden egyes sornál próbálnánk).
    """

    def __init__(self, filename: Path, maxBytes: int = 0, backupCount: int = 0,
                 encoding: str = "utf-8", delay: bool = True) -> None:
        super().__init__(filename, maxBytes=maxBytes, backupCount=backupCount,
                         encoding=encoding, delay=delay)
        self._retry_after = 0.0

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if time.monotonic() < self._retry_after:
            return False
        return bool(super().shouldRollover(record))

    def rotate(self, source: str, dest: str) -> None:
        atomic_replace(Path(source), Path(dest))   # Windowson újrapróbálkozik

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError:
            self._retry_after = time.monotonic() + LOG_ROTATE_RETRY
            if self.stream is None:                # a szülő lezárta, de nem nyitotta újra
                with suppress(OSError):
                    self.stream = self._open()


def setup_file_log(path: Path = LOG_FILE) -> Path | None:
    """A rotáló naplófájl bekapcsolása; a visszatérési érték a fájl helye.

    Napló nélkül is működnie kell a programnak: ha a mappa nem írható (csak
    olvasható profil, teli lemez), a hibát megjegyezzük, és megyünk tovább.
    """
    if file_log.handlers:                          # kétszer ne akasszunk rá kezelőt
        return Path(file_log.handlers[0].baseFilename)   # type: ignore[attr-defined]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingLog(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
                              encoding="utf-8", delay=True)
    except OSError as exc:
        log.warning("A naplófájl nem hozható létre (%s): %s", path, exc)
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s  [%(threadName)s]  %(message)s",
                                           datefmt="%Y-%m-%d %H:%M:%S"))
    for logger in (file_log, log):
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return path


def close_file_log() -> None:
    """A naplófájl lezárása és leakasztása. Kétszer hívva sem hibázik."""
    handlers = set()
    for logger in (file_log, log):
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handlers.add(handler)
    for handler in handlers:          # ugyanaz a kezelő mindkettőn rajta van
        handler.close()


def note(message: str) -> None:
    """Egy sor a naplófájlba. Naplózás nélkül (tesztek) csendben elszáll."""
    file_log.info("%s", message)


# --------------------------------------------------------------------------- #
#  Segédfüggvények
# --------------------------------------------------------------------------- #

def human(n: float | None) -> str:
    """Bájtszám emberi formában."""
    if not n:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:  # noqa: PLR2004
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def human_time(sec: float | None) -> str:
    """Másodperc -> ó:pp:mm."""
    if not sec or sec < 0 or sec > 86_400 * 7:
        return "-"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _stem(name: str) -> str:
    """A név eszköznév-része. A "CON .txt" is a CON eszközt jelenti Windowson,
    ezért a pont előtti szóközöket le kell vágni."""
    return name.partition(".")[0].rstrip(" ").upper()


def safe_component(raw: str) -> str:
    """Egy útvonalelem biztonságossá tétele minden platformon."""
    name = _ILLEGAL_CHARS.sub("_", unquote(raw))
    name = name.rstrip(". ")                      # Windows: nem végződhet ponttal
    if not name or name in {".", ".."}:
        return "_"
    if _stem(name) in _WIN_RESERVED:              # CON.txt is foglalt!
        name = "_" + name
    if len(name) > _MAX_COMPONENT:                # hosszú név rövidítése, kiterjesztés megtartva
        root, dot, ext = name.rpartition(".")
        ext = f".{ext}" if dot and len(ext) <= MAX_EXT_LEN else ""
        keep = _MAX_COMPONENT - len(ext) - 9
        name = f"{(root or name)[:keep]}_{_short_hash(name)}{ext}"
    return name


def _short_hash(text: str) -> str:
    """Rövid, de a gyakorlatban ütközésmentes megkülönböztető jel."""
    return blake2b(text.encode(), digest_size=4).hexdigest()


def url_to_relpath(url: str) -> Path:
    """URL -> a célkönyvtáron belüli relatív útvonal, a szerver szerkezetét tükrözve."""
    parts = urlparse(url)
    segments = [safe_component(s) for s in PurePosixPath(parts.path).parts if s != "/"]
    if not segments:
        segments = ["index.html"]
    if parts.query:                               # ?id=1 -> külön fájl
        root, dot, ext = segments[-1].rpartition(".")
        tag = _short_hash(parts.query)
        segments[-1] = f"{root}_{tag}.{ext}" if dot else f"{segments[-1]}_{tag}"
    rel = Path(safe_component(parts.netloc), *segments)
    if len(str(rel)) > MAX_REL_PATH:              # túl hosszú út lapítása
        rel = Path(safe_component(parts.netloc),
                   f"{_short_hash(url)}_{segments[-1][-60:]}")
    return rel


def url_ext(url: str) -> str:
    return PurePosixPath(urlparse(url).path).suffix.lower()


def parse_content_range_total(value: str | None) -> int | None:
    """'bytes 100-199/1234' vagy 'bytes */1234' -> 1234."""
    if not value:
        return None
    _, _, total = value.partition("/")
    return int(total) if total.isdigit() else None


@dataclass(frozen=True, slots=True)
class WritePlan:
    """Hogyan írjuk a fájlt a kapott válasz alapján."""

    mode: str            # "ab" (folytatás) vagy "wb" (elölről)
    offset: int          # ennyi bájt van már a lemezen
    total: int           # a fájl teljes mérete, 0 ha ismeretlen
    resumable: bool      # folytatható-e egy következő próbálkozásnál
    restart: bool        # a szerver eldobta a folytatási kérést


def plan_write(resp: httpx.Response, done: int) -> WritePlan:
    """A válasz fejlécei alapján eldönti, hogyan folytatódjon az írás.

    Ha a szerver tömörítve küld, a Content-Length a tömörített méretre
    vonatkozik, a lemezen levő bájtszám pedig a kicsomagoltra: ilyenkor a
    bájteltolásos folytatás elvileg sem működhet, ezért letiltjuk.
    """
    encoding = resp.headers.get("content-encoding", "identity").lower()
    encoded = encoding not in ("", "identity")
    no_ranges = resp.headers.get("accept-ranges", "").lower() == "none"
    length = resp.headers.get("content-length", "")
    declared = int(length) if length.isdigit() else 0

    if resp.status_code == HTTP_PARTIAL:
        total = parse_content_range_total(resp.headers.get("content-range"))
        return WritePlan(mode="ab", offset=done,
                         total=total if total is not None else done + declared,
                         resumable=not (encoded or no_ranges), restart=False)
    return WritePlan(mode="wb", offset=0, total=0 if encoded else declared,
                     resumable=not (encoded or no_ranges), restart=True)


def atomic_replace(src: Path, dst: Path, attempts: int = 5) -> None:
    """Fájl végleges helyre mozgatása.

    Windowson a víruskereső, a keresőindexelő vagy egy előnézeti panel
    átmenetileg fogva tarthatja a fájlt, és a csere ``PermissionError``-ral
    elszáll. Ilyenkor rövid várakozás után újrapróbáljuk; Linuxon és macOS-en
    ez az ág gyakorlatilag sosem fut le.
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.15 * 2 ** attempt)


def fit_path(outdir: Path, rel: Path, limit: int = MAX_ABS_PATH) -> Path:
    """A teljes útvonalat a Windows 260 karakteres korlátja alá szorítja.

    Szándékosan minden rendszeren ugyanígy viselkedik, hogy a Linuxon vagy
    macOS-en letöltött mappa Windowson is használható maradjon.
    """
    if len(str(outdir / rel)) <= limit:
        return rel
    ext = rel.suffix if len(rel.suffix) <= MAX_EXT_LEN else ""
    flat = Path(f"{rel.stem[:40]}_{_short_hash(rel.as_posix())}{ext}")
    if len(str(outdir / flat)) <= limit:
        return flat
    return Path(f"{_short_hash(rel.as_posix())}{ext}")     # végső esetben csak a jel


_spawned: list[subprocess.Popen[bytes]] = []


def spawn_detached(command: str | list[str]) -> None:
    """Külső program indítása úgy, hogy a felület ne várjon rá.

    A korábban indítottak közül a már befejezetteket begyűjtjük: enélkül
    POSIX-on minden egyes megnyitás után zombi folyamat maradna a program
    végéig. A saját munkamenet és az elnyelt kimenet azt szolgálja, hogy a
    fájlkezelő ne írjon a konzolunkra, és ne kapja meg a mi Ctrl+C-nket.
    """
    _spawned[:] = [proc for proc in _spawned if proc.poll() is None]
    _spawned.append(subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True))        # Windowson a subprocess figyelmen kívül hagyja


def open_in_file_manager(path: Path) -> None:
    """Célmappa megnyitása az operációs rendszer fájlkezelőjében."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            spawn_detached(["open", str(path)])
        else:
            spawn_detached(["xdg-open", str(path)])
    except OSError as exc:
        log.warning("A mappa megnyitása nem sikerült: %s", exc)


def explorer_select_command(path: Path) -> str:
    """Az Intéző parancssora, amellyel a fájlra állva nyílik meg egy új ablak.

    Két buktatót kerül meg:

    * Az idézőjel csakis a `/select,` *után* jó. A listás Popen-alak a szóközös
      úton (pl. ``C:\\Users\\Kis Béla\\...``) az egész paramétert idézőjelbe tenné
      (``explorer "/select,C:\\..."``), amitől az Intéző nem a kért fájlt jelöli
      ki, hanem a Dokumentumok mappát nyitja meg.
    * Az explorer.exe teljes úttal indul: név szerint hívva a CreateProcess a
      *futó folyamat aktuális könyvtárát* is végignézi, így egy letöltött,
      odakerült explorer.exe indulhatna el helyette.
    """
    explorer = PureWindowsPath(os.environ.get("WINDIR", "C:\\Windows"), "explorer.exe")
    return f'"{explorer}" /select,"{path}"'


def reveal_in_file_manager(path: Path) -> None:
    """Egy fájl megmutatása a fájlkezelőben, lehetőleg kijelölve.

    Ha a fájl még nincs meg, vagy a rendszer nem tud kijelölni, a mappáját nyitjuk meg.
    """
    if path.is_file():
        try:
            if sys.platform == "win32":
                # Egyetlen sztringként adjuk át: a CreateProcess pontosan ezt kapja,
                # nem fut se cmd.exe, se shell, ami újraértelmezné az idézőjeleket.
                spawn_detached(explorer_select_command(path))
                return
            if sys.platform == "darwin":
                spawn_detached(["open", "-R", str(path)])
                return
        except OSError as exc:
            log.warning("A fájl megmutatása nem sikerült: %s", exc)
    open_in_file_manager(path.parent if path.parent.is_dir() else path)


def make_client(threads: int, timeout: float = 30.0) -> httpx.Client:
    """Szálak közt megosztott HTTP-kliens, a szálszámhoz igazított kapcsolatkészlettel."""
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,          # httpx alapból NEM követi az átirányítást
        timeout=httpx.Timeout(timeout, connect=15.0),
        # A felső határhoz méretezzük, hogy a szálszám menet közbeni növelése
        # ne akadjon el a kapcsolatkészleten. Ezek felső korlátok, nem előfoglalás.
        limits=httpx.Limits(max_connections=max(threads, MAX_THREADS) + 2,
                            max_keepalive_connections=max(threads, MAX_THREADS)),
    )


# --------------------------------------------------------------------------- #
#  Streamelt hivatkozás-kigyűjtés (nem épít DOM-ot)
# --------------------------------------------------------------------------- #

class LinkExtractor(HTMLParser):
    """Csak a hivatkozásokat gyűjti ki, a dokumentumot nem tartja meg.

    Mérésünk szerint a bs4+html.parser párosnál ~7x gyorsabb, és nem használ
    mérhető többletmemóriát, mert nem épül fából álló objektumszerkezet.
    """

    _ATTR_OF: Final = {"a": "href", "area": "href", "link": "href",
                       "img": "src", "source": "src", "video": "src",
                       "audio": "src", "embed": "src", "iframe": "src"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.base: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "base":
            for key, val in attrs:
                if key == "href" and val:
                    self.base = val
            return
        wanted = self._ATTR_OF.get(tag)
        if wanted is None:
            return
        for key, val in attrs:
            if key == wanted and val:
                self.links.append(val)
                return

    def handle_data(self, data: str) -> None:      # nem tároljuk a szöveget
        return


# --------------------------------------------------------------------------- #
#  Átvizsgálás
# --------------------------------------------------------------------------- #

CHECKED_MARK: Final = "☑"
UNCHECKED_MARK: Final = "☐"
NO_EXT_LABEL: Final = "(nincs kiterjesztés)"
HTML_LABEL: Final = "html"
# Az üres mező "minden kiterjesztést" jelent, ezért az "egyetlen sem" állapotnak
# saját jelölés kell. Egyetlen valódi címkére sem illik, így a szűrés üresen tér vissza.
NONE_TOKEN: Final = "(egyik sem)"


def ext_label(url: str, is_page: bool = False) -> str:
    """A felületen megjelenő kiterjesztés-címke. A HTML-oldalak mindig 'html'."""
    if is_page:
        return HTML_LABEL
    ext = url_ext(url).lstrip(".")
    return ext or NO_EXT_LABEL


def item_label(item: Item) -> str:
    """Egy elem kiterjesztés-címkéje. Régi állapotfájlban még nem volt eltárolva,
    ilyenkor a címből képezzük, hogy a szűrés akkor is működjön."""
    return item.label or ext_label(item.url)


@dataclass(slots=True)
class ScanResult:
    """Az átvizsgálás nyers eredménye: minden megtalált cím, szűrés nélkül."""

    files: list[str] = dataclass_field(default_factory=list)   # nem HTML tartalmak
    pages: list[str] = dataclass_field(default_factory=list)   # bejárt HTML oldalak

    @property
    def all_urls(self) -> list[str]:
        return [*self.files, *self.pages]

    def by_extension(self) -> dict[str, list[str]]:
        """Kiterjesztés -> a hozzá tartozó címek, ábécésorrendben."""
        groups: dict[str, list[str]] = {}
        for url in self.files:
            groups.setdefault(ext_label(url), []).append(url)
        for url in self.pages:
            groups.setdefault(HTML_LABEL, []).append(url)
        return dict(sorted(groups.items(), key=lambda kv: (kv[0] == NO_EXT_LABEL, kv[0])))


def parse_ext_filter(text: str) -> set[str]:
    """A "Kiterjesztések" mező szövegéből a keresett címkék halmaza."""
    return {e.strip().lstrip(".").lower() for e in text.split(",") if e.strip()}


def choose_labels(labels: Iterable[str], wanted: set[str], want_html: bool) -> set[str]:
    """A megadott (kézzel beírt) kérésnek megfelelő kiterjesztés-címkék.

    Üres kérés esetén minden címke kell, a HTML kivételével - azt külön
    kapcsoló szabályozza, mert a legtöbb esetben nem a lapok kellenek.
    """
    known = set(labels)
    chosen = {lab for lab in known if lab.lower() in wanted} if wanted else set(known)
    if want_html:
        chosen.add(HTML_LABEL)
    else:
        chosen.discard(HTML_LABEL)
    return chosen & known


def text_representable(label: str) -> bool:
    """Kifejezhető-e a címke a vesszővel tagolt mezőben?

    A vesszőt tartalmazó utótag (pl. a "fajl.a,b" címéből képzett "a,b") nem az:
    a mező két külön kiterjesztésnek olvasná. Az ilyen ritka címkét ezért csak a
    saját jelölőnégyzete vezérli, a mező nem ír bele és nem is veszi el.
    """
    return "," not in label


def ext_filter_text(labels: Iterable[str]) -> str:
    """A kipipált címkékből a mezőbe illő szöveg - mintha kézzel írták volna be.

    A HTML-t külön jelölőnégyzet kezeli, ezért kimarad. Ha semmi sincs
    kipipálva, jelezni kell: az üres mező ugyanis "minden kiterjesztést" jelent.
    """
    picked = [lab for lab in labels if lab != HTML_LABEL and text_representable(lab)]
    return ", ".join(picked) if picked else NONE_TOKEN


def matching_extensions(result: ScanResult, wanted: set[str], want_html: bool) -> set[str]:
    """A talált címkék közül a kért kiterjesztésekhez tartozók."""
    return choose_labels(result.by_extension(), wanted, want_html)


# --------------------------------------------------------------------------- #
#  robots.txt (RFC 9309)
# --------------------------------------------------------------------------- #

# A saját terméknevünk; a robots.txt csoportfejlécei ehhez illeszkednek.
ROBOTS_TOKEN: Final = USER_AGENT.split("/")[0]


@dataclass(frozen=True, slots=True)
class RobotsRule:
    """Egy Allow/Disallow sor: illeszkedési minta és a specifikusság mértéke."""

    pattern: re.Pattern[str]
    length: int          # a nyers minta hossza - ez dönt az ütköző szabályok közt
    allow: bool


class RobotsRules:
    """Egy kiszolgáló robots.txt-jének ránk vonatkozó szabályai (RFC 9309).

    A szabvány két ponton tér el az egyszerű, prefixes olvasattól:

    * a mintában a ``*`` tetszőleges karaktersort, a záró ``$`` a cím végét
      jelenti (§2.2.2);
    * nem az első illeszkedő sor dönt, hanem a leghosszabb minta, és azonos
      hossz esetén az ``Allow`` nyer (§2.2.2). Enélkül az ``Allow`` sosem tudná
      felülírni a nála tágabb ``Disallow``-ot, azaz értelmét vesztené.
    """

    def __init__(self, rules: Iterable[RobotsRule] = ()) -> None:
        self._rules = tuple(rules)

    # -- feldolgozás ------------------------------------------------------
    @classmethod
    def parse(cls, text: str, token: str = ROBOTS_TOKEN) -> RobotsRules:
        """A fájlból a ránk vonatkozó csoport szabályai.

        A csoportokat a ``User-agent`` sorok nyitják; egy szabálysor után
        következő ``User-agent`` már új csoportot kezd. Az azonos nevű
        csoportok összeolvadnak, az ismeretlen kulcsok (Sitemap, Crawl-delay)
        kimaradnak.
        """
        groups: dict[str, list[RobotsRule]] = {}
        agents: list[str] = []      # az épp nyitott csoport fejlécei
        in_header = True            # még a csoport fejlécénél tartunk?

        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key, value = key.strip().lower(), value.strip()

            if key == "user-agent":
                if not in_header:   # az előző csoport lezárult
                    agents, in_header = [], True
                if value:
                    agents.append(value.lower())
                    groups.setdefault(value.lower(), [])
                continue

            if key not in {"allow", "disallow"}:
                continue
            in_header = False
            rule = cls._rule(value, allow=key == "allow")
            if rule is None:        # üres vagy értelmezhetetlen minta
                continue
            for agent in agents:    # csoportfej nélküli szabály sehová nem tartozik
                groups[agent].append(rule)

        return cls(groups.get(cls._best_group(groups, token), ()))

    @staticmethod
    def _best_group(groups: dict[str, list[RobotsRule]], token: str) -> str:
        """A terméknevünkre illeszkedő leghosszabb csoportnév, egyébként a "*"."""
        token = token.lower()
        best = ""
        for agent in groups:
            if agent != "*" and token.startswith(agent) and len(agent) > len(best):
                best = agent
        return best or "*"

    @staticmethod
    def _rule(value: str, *, allow: bool) -> RobotsRule | None:
        """Egy szabálysor értékéből minta, vagy None, ha a sor nem szabály.

        Az üres érték nem tiltás és nem is engedés, a "/" vagy "*" jellel nem
        kezdődő útvonal pedig érvénytelen - mindkettő figyelmen kívül marad.
        """
        if not value or not value.startswith(("/", "*")):
            return None
        body, tail = (value[:-1], r"\Z") if value.endswith("$") else (value, "")
        # A "*" mentén darabolunk, és csak a darabokat oldjuk fel: így a %2A
        # nem válik joker jellé.
        regex = ".*".join(re.escape(unquote(part)) for part in body.split("*"))
        return RobotsRule(re.compile(regex + tail), len(value), allow)

    # -- döntés -----------------------------------------------------------
    def allowed(self, url: str) -> bool:
        """Bejárható-e a cím? Ha egyik szabály sem illeszkedik, igen."""
        parts = urlparse(url)
        path = unquote(parts.path or "/")
        if parts.query:
            path += "?" + unquote(parts.query)
        best: RobotsRule | None = None
        for rule in self._rules:
            if rule.pattern.match(path) and (best is None or rule.length > best.length
                                             or (rule.length == best.length and rule.allow)):
                best = rule
        return best is None or best.allow


@dataclass(slots=True)
class ScanConfig:
    root: str
    depth: int = 0
    same_host: bool = True
    respect_robots: bool = True
    stop_on_robots_error: bool = False   # elérhetetlen robots.txt: leállás vagy folytatás
    max_pages: int = 500


class Scanner:
    """Szélességi bejárás; a nem HTML válaszok testét el sem olvassa."""

    def __init__(self, cfg: ScanConfig, client: httpx.Client,
                 on_log: Callable[[str], None], stop: threading.Event) -> None:
        self.cfg = cfg
        self.client = client
        self.on_log = on_log
        self.stop = stop
        self._robots: dict[str, RobotsRules] = {}

    # -- robots.txt ------------------------------------------------------
    def _fetch_robots(self, origin: str) -> RobotsRules:
        """A kiszolgáló szabályai; elérhetetlen fájl esetén a beállítás dönt.

        Az RFC 9309 §2.3.1.4 különbséget tesz a kétféle hiba között: a 4xx azt
        jelenti, hogy *nincs* szabály (minden bejárható), az 5xx viszont azt,
        hogy *nem tudjuk*, mi a szabály - ez teljes tiltást kíván. Egy villanásnyi
        szerverhiba ne döntsön ekkorát, ezért előbb néhányszor újrapróbáljuk; a
        hálózati hibát (időtúllépés, megszakadt kapcsolat) ugyanígy kezeljük,
        mert az is "nem tudjuk".
        """
        reason = ""
        for attempt in range(1, ROBOTS_TRIES + 1):
            try:
                with self.client.stream("GET", f"{origin}/robots.txt", timeout=10.0) as resp:
                    if resp.status_code == HTTP_OK:
                        return RobotsRules.parse(self._robots_text(resp))
                    if resp.status_code < HTTP_SERVER_ERROR:
                        return RobotsRules()   # nincs robots.txt -> nincs tiltás
                    reason = f"HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                reason = type(exc).__name__
            if attempt < ROBOTS_TRIES:
                self.on_log(f"A robots.txt nem érhető el ({reason}) - {attempt}. kísérlet, "
                            f"újrapróbálom: {origin}")
                if self.stop.wait(ROBOTS_RETRY_WAIT * attempt):
                    raise Cancelled
        if self.cfg.stop_on_robots_error:
            raise RobotsUnavailable(
                f"A(z) {origin} robots.txt-je {ROBOTS_TRIES} próbálkozásra sem érhető el "
                f"({reason}), az átvizsgálás leáll.")
        self.on_log(f"A(z) {origin} robots.txt-je nem érhető el ({reason}), "
                    "az átvizsgálás folytatódik.")
        return RobotsRules()

    @staticmethod
    def _robots_text(resp: httpx.Response) -> str:
        """A robots.txt szövege, korlátos mérettel.

        Az RFC 9309 §2.5 legalább 500 KiB feldolgozását várja el; ennél többet
        jóindulatú kiszolgáló nem küld, egy rosszindulatútól viszont ugyanúgy
        nem akarjuk a memóriát félteni, mint a túl nagy HTML lapoknál. A levágott
        utolsó sor fél szabály lehetne, ezért azt eldobjuk.
        """
        parts: list[str] = []
        size = 0
        for chunk in resp.iter_text(64 * 1024):
            parts.append(chunk)
            size += len(chunk)
            if size >= ROBOTS_MAX_TEXT:
                text = "".join(parts)
                return text[:text.rfind("\n") + 1]
        return "".join(parts)

    def _allowed(self, url: str) -> bool:
        if not self.cfg.respect_robots:
            return True
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = self._fetch_robots(origin)
        return self._robots[origin].allowed(url)

    @staticmethod
    def _looks_like_page(url: str) -> bool:
        """Bejárható lapnak látszik-e. A kiterjesztés nélküli címeket a
        Content-Type dönti el, ezért azok is idekerülnek."""
        return url_ext(url) in HTML_EXTS

    # -- oldal beolvasása -------------------------------------------------
    def _read_page(self, url: str) -> tuple[str, list[str]] | None:
        """(végleges URL, hivatkozások) vagy None, ha nem HTML.

        A választ streamelve olvassuk: ha a Content-Type nem HTML, egyetlen
        bájtot sem töltünk le a törzsből.
        """
        try:
            with self.client.stream("GET", url, timeout=25.0) as resp:
                if resp.status_code >= HTTP_BAD_REQUEST:
                    self.on_log(f"HTTP {resp.status_code}: {url}")
                    return None
                ctype = resp.headers.get("content-type", "").split(";")[0].strip()
                if "html" not in ctype and "xml" not in ctype:
                    return None
                parser = LinkExtractor()
                size = 0
                for chunk in resp.iter_text(64 * 1024):
                    if self.stop.is_set():
                        raise Cancelled
                    size += len(chunk)
                    parser.feed(chunk)
                    if size > MAX_HTML_BYTES:
                        self.on_log(f"Túl nagy oldal, félbehagyva: {url}")
                        break
                parser.close()
                base = urljoin(str(resp.url), parser.base) if parser.base else str(resp.url)
                return base, parser.links
        except Cancelled:
            raise
        except httpx.HTTPError as exc:
            self.on_log(f"Hiba az oldal olvasásakor ({url}): {exc!s}")
            return None

    # -- bejárás ----------------------------------------------------------
    def _absolute_links(self, links: Iterable[str], base: str, root_host: str) -> Iterator[str]:
        """A nyers hivatkozásokból abszolút, szűrt http(s) címeket ad vissza."""
        for raw in links:
            if raw.startswith(("javascript:", "mailto:", "data:", "#", "tel:")):
                continue
            full = urldefrag(urljoin(base, raw)).url
            parts = urlparse(full)
            if parts.scheme not in ("http", "https"):
                continue
            if self.cfg.same_host and parts.netloc != root_host:
                continue
            yield full

    @dataclass(slots=True)
    class _Crawl:
        """A bejárás munkaállapota egy helyen."""

        result: ScanResult
        visited: set[str]
        seen_files: set[str]
        todo: deque[tuple[str, int]]
        root_host: str

    def run(self) -> ScanResult:
        """A bejárás fő ciklusa: minden megtalált címet visszaad, szűrés nélkül.

        A szűrés tudatosan a felhasználóra marad, hogy az átvizsgálás után a
        talált kiterjesztések közül lehessen válogatni.
        """
        note(f"Átvizsgálás indul: {self.cfg.root} (mélység {self.cfg.depth}, "
             f"{'csak azonos domain' if self.cfg.same_host else 'bármely domain'}, "
             f"robots.txt: {'betartva' if self.cfg.respect_robots else 'figyelmen kívül'})")
        crawl = self._Crawl(result=ScanResult(), visited=set(), seen_files=set(),
                            todo=deque([(self.cfg.root, 0)]),
                            root_host=urlparse(self.cfg.root).netloc)
        result, visited, seen_files, todo = (crawl.result, crawl.visited,
                                             crawl.seen_files, crawl.todo)

        while todo and not self.stop.is_set() and len(visited) < self.cfg.max_pages:
            url, depth = todo.popleft()
            url = urldefrag(url).url
            if url in visited:
                continue
            visited.add(url)
            try:
                if not self._allowed(url):
                    self.on_log(f"robots.txt tiltja: {url}")
                    continue

                page = self._read_page(url)
                if page is None:                   # nem HTML -> maga is fájl
                    if url not in seen_files:
                        seen_files.add(url)
                        result.files.append(url)
                    continue

                result.pages.append(url)           # ez egy letölthető HTML lap
                base, links = page
                self.on_log(f"Átvizsgálás (mélység {depth}): {url} - {len(links)} hivatkozás")
                self._sort_links(links, base, depth, crawl)
            except RobotsUnavailable as exc:
                self.on_log(str(exc))
                break
            except Cancelled:
                break

        if len(visited) >= self.cfg.max_pages:
            self.on_log(f"Elértük az oldalkorlátot ({self.cfg.max_pages}).")
        note(f"Átvizsgálás vége: {len(result.pages)} lap, {len(result.files)} fájl, "
             f"{len(visited)} megnyitott cím")
        return result

    def _sort_links(self, links: list[str], base: str, depth: int,
                    crawl: Scanner._Crawl) -> None:
        """A hivatkozásokat vagy bejárandó lapnak, vagy letölthető fájlnak sorolja be."""
        for full in self._absolute_links(links, base, crawl.root_host):
            if not self._allowed(full):
                continue
            # Lapnak látszik és van még mélység -> bejárjuk. A mélységhatáron
            # túl viszont fájlként vesszük fel, hogy ne vesszen el a találat.
            if self._looks_like_page(full) and depth < self.cfg.depth:
                if full not in crawl.visited:
                    crawl.todo.append((full, depth + 1))
            elif full not in crawl.seen_files:
                crawl.seen_files.add(full)
                crawl.result.files.append(full)


# --------------------------------------------------------------------------- #
#  Letöltendő elem és állapottár
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Item:
    """Egy letöltendő fájl. A slots miatt tízezer elemnél is kicsi a memóriaigény."""

    url: str
    path: str                       # célkönyvtárhoz képesti relatív út
    total: int = 0
    done: int = 0
    status: str = Status.PENDING
    validator: str | None = None    # ETag vagy Last-Modified az If-Range-hez
    error: str = ""
    resumable: bool = True          # hamis, ha a szerver nem folytatható választ ad
    selected: bool = True           # a felületen kipipálva -> letöltendő
    label: str = ""                 # kiterjesztés-címke a szűréshez
    force: bool = False             # a felhasználó kérte a felülírást (nem mentjük el)

    @property
    def percent(self) -> float:
        return self.done * 100 / self.total if self.total else 0.0

    def as_dict(self) -> dict[str, object]:
        return {"url": self.url, "path": self.path, "total": self.total,
                "done": self.done, "status": str(self.status),
                "validator": self.validator, "error": self.error,
                "resumable": self.resumable, "selected": self.selected,
                "label": self.label}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Item:      # a JSON-ból bármi jöhet
        # A régi, Windowson mentett állapotfájlban "\\" az elválasztó. Máshol ez
        # nem útvonal, hanem egyetlen, visszaperjeles fájlnév lenne, tehát a
        # meglévő fájlok "eltűnnének", és a program létre is hozná a furcsa nevű
        # másolatukat. A fájlnevekben a "\\" amúgy is tiltott (lásd safe_component).
        return cls(url=raw["url"], path=str(raw["path"]).replace("\\", "/"),
                   total=int(raw.get("total", 0)),
                   done=int(raw.get("done", 0)),
                   status=raw.get("status", Status.PENDING),
                   validator=raw.get("validator"), error=raw.get("error", ""),
                   resumable=bool(raw.get("resumable", True)),
                   selected=bool(raw.get("selected", True)),
                   label=raw.get("label", ""))


class StateStore:
    """Az állapot lemezre mentése atomi módon, késleltetve (csak ha változott)."""

    def __init__(self, outdir: Path) -> None:
        self.outdir = outdir
        self.path = outdir / STATE_FILE
        self._dirty = False
        self._lock = threading.Lock()

    def mark_dirty(self) -> None:
        self._dirty = True

    def load(self) -> dict[str, Item]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        entries = raw.get("items", raw) if isinstance(raw, dict) else {}
        items: dict[str, Item] = {}
        for key, value in entries.items():
            try:
                value.setdefault("url", key)
                items[value["url"]] = Item.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
        return items

    def save(self, items: dict[str, Item], force: bool = False) -> None:
        if not (self._dirty or force):
            return
        # A pillanatkép egyetlen C-szintű művelet, tehát oszthatatlan: enélkül egy
        # párhuzamos bővítés (átvizsgálás letöltés közben) "dictionary changed size
        # during iteration" hibával szakítaná félbe a mentést - és mivel ez a mentő
        # szálon történik, onnantól *egyáltalán* nem készülne mentés.
        pillanatkep = list(items.items())
        payload = {"version": STATE_VERSION,
                   "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "items": {url: item.as_dict() for url, item in pillanatkep}}
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                self.outdir.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(data, encoding="utf-8")
                atomic_replace(tmp, self.path)      # atomi csere, Windowson újrapróbálva
                self._dirty = False
            except OSError as exc:
                log.warning("Állapot mentése sikertelen: %s", exc)


# --------------------------------------------------------------------------- #
#  Letöltéskezelő
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Totals:
    """Folyamatosan karbantartott összesítő - nem kell végigjárni a listát."""

    files: int = 0
    done_files: int = 0
    bytes_total: int = 0
    bytes_done: int = 0


class DownloadManager:
    """Többszálú, folytatható letöltés. GUI-független, parancssorból is használható."""

    def __init__(self, outdir: Path, threads: int = 4,
                 on_event: Callable[[str, object], None] | None = None,
                 client: httpx.Client | None = None,
                 existing: str = Existing.VERIFY) -> None:
        self.outdir = Path(outdir)
        self.threads = max(1, min(threads, MAX_THREADS))
        self.existing = existing
        self.on_event = on_event or (lambda kind, payload: None)
        self.items: dict[str, Item] = {}
        self.totals = Totals()
        self.store = StateStore(self.outdir)
        self._client = client
        self._owns_client = client is None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self._worker: threading.Thread | None = None
        self._todo: deque[Item] = deque()
        self._pool: list[threading.Thread] = []
        self._active = 0                  # épp élő munkásszálak száma
        self._used_paths: set[str] = set()
        self._speed = 0.0                 # bájt/mp, exponenciálisan simítva
        self._speed_mark = (time.monotonic(), 0)
        note(f"Célkönyvtár: {self.outdir} ({self.threads} szál, "
             f"meglévő fájl: {self.existing})")

    # -- életciklus -------------------------------------------------------
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client(self.threads)
        return self._client

    def close(self) -> None:
        self.stop()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=10)
        self.store.save(self.items, force=True)
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    # -- állapot ----------------------------------------------------------
    def load_state(self) -> None:
        """Korábbi állapot betöltése; a lemez tartalma a mérvadó."""
        self.items = self.store.load()
        self._used_paths = {i.path.casefold() for i in self.items.values()}
        for item in self.items.values():
            dest = self.outdir / item.path
            part = dest.with_name(dest.name + ".part")
            if dest.exists():
                item.done = dest.stat().st_size
                # Csak akkor kész, ha a méret igazoltan egyezik a szerverivel.
                # Egyébként ellenőrzésre vár - így a máshonnan odakerült csonka
                # fájl nem csúszik át késznek.
                if item.total == item.done and item.total:
                    item.status = Status.DONE
                    # A pipa is lekerül róla: amit már letöltöttünk, azt nem
                    # akarjuk véletlenül felülírni a következő indításnál.
                    item.selected = False
                else:
                    item.status = Status.CHECK
            elif part.exists():
                item.done = part.stat().st_size
                item.status = Status.PAUSED
            else:
                item.done, item.status = 0, Status.PENDING
        self._recount()
        self.on_event("reset", list(self.items.values()))

    def _recount(self) -> None:
        """Az összesítő csak a kipipált elemekre vonatkozik."""
        totals = Totals()
        for item in self.items.values():
            if not item.selected:
                continue
            totals.files += 1
            totals.bytes_total += item.total
            totals.bytes_done += item.done
            totals.done_files += item.status == Status.DONE
        self.totals = totals

    def _unique_path(self, url: str) -> str:
        """Ütköző fájlnevek feloldása (két URL ugyanarra az útra képződne).

        Az ütközést kis- és nagybetűre érzéketlenül vizsgáljuk, mert a Windows
        fájlrendszere sem tesz köztük különbséget: az ``A.PDF`` és az ``a.pdf``
        ott ugyanaz a fájl lenne.
        """
        rel = fit_path(self.outdir, url_to_relpath(url))
        # Mindig "/" az elválasztó, Windowson is: így a célmappa a benne lévő
        # állapotfájllal együtt átvihető másik rendszerre. A Windows a "/"-t
        # ugyanúgy elfogadja útvonal-elválasztónak, mint a visszaperjelet.
        key = rel.as_posix()
        if key.casefold() not in self._used_paths:
            self._used_paths.add(key.casefold())
            return key
        alt = fit_path(self.outdir,
                       rel.with_name(f"{rel.stem}_{_short_hash(url)}{rel.suffix}")).as_posix()
        self._used_paths.add(alt.casefold())
        return alt

    def add_urls(self, urls: Iterable[str], *, labels: dict[str, str] | None = None,
                 selected: bool = True) -> int:
        """Új címek felvétele. A labels a kiterjesztés-címkéket adja meg URL-enként."""
        added: list[Item] = []
        with self._lock:
            for url in urls:
                if url in self.items:
                    continue
                item = Item(url=url, path=self._unique_path(url), selected=selected,
                            label=(labels or {}).get(url) or ext_label(url))
                self.items[url] = item
                added.append(item)
        if added:
            self._recount()
            self.store.mark_dirty()
            self.store.save(self.items)
            self.on_event("added", added)
        return len(added)

    def pending(self) -> list[Item]:
        """A letöltésre váró, kipipált elemek."""
        return [i for i in self.items.values() if i.selected and i.status != Status.DONE]

    def set_selected(self, urls: Iterable[str], value: bool) -> None:
        """Elemek ki- vagy bepipálása; az összesítő azonnal követi."""
        wanted = set(urls)
        for url in wanted:
            item = self.items.get(url)
            if item is not None:
                item.selected = value
        self._recount()
        self.store.mark_dirty()

    # -- vezérlés ---------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    def start(self) -> bool:
        if self.running:
            self._resume.set()
            return False
        self._stop.clear()
        self._resume.set()
        self._worker = threading.Thread(target=self._run, name="downloader", daemon=True)
        self._worker.start()
        return True

    def pause(self, value: bool = True) -> None:
        self._resume.clear() if value else self._resume.set()

    def stop(self) -> None:
        self._stop.set()
        self._resume.set()          # a szüneten várakozó szálakat is elengedjük

    def _gate(self) -> None:
        """Szünet/leállítás ellenőrzése - letöltési cikluson belül hívjuk."""
        if self._stop.is_set():
            raise Cancelled
        while not self._resume.wait(0.2):
            if self._stop.is_set():
                raise Cancelled
        if self._stop.is_set():
            raise Cancelled

    def _sleep(self, seconds: float) -> None:
        """Megszakítható várakozás (újrapróbálkozás előtt)."""
        if self._stop.wait(seconds):
            raise Cancelled

    # -- fő ciklus --------------------------------------------------------
    def set_threads(self, count: int) -> None:
        """A párhuzamos szálak száma menet közben is állítható.

        Növeléskor azonnal indulnak az új munkásszálak; csökkentéskor a fölös
        szálak a *folyamatban lévő* fájljukat még befejezik, és csak utána
        vonulnak vissza, hogy egyetlen letöltött bájt se vesszen kárba.
        """
        count = max(1, min(count, MAX_THREADS))
        if count == self.threads:
            return
        previous, self.threads = self.threads, count
        if self.running:
            direction = "növelve" if count > previous else "csökkentve"
            self.on_event("log", f"Szálak száma {direction}: {previous} -> {count}")

    def _scale_workers(self) -> list[threading.Thread]:
        """A hiányzó munkásszálak létrehozása; a zárat nem tartjuk indítás közben."""
        fresh: list[threading.Thread] = []
        with self._lock:
            self._pool = [t for t in self._pool if t.is_alive()]
            missing = min(self.threads - self._active, len(self._todo))
            for _ in range(max(0, missing)):
                self._active += 1
                worker = threading.Thread(target=self._worker_loop, name="dl", daemon=True)
                self._pool.append(worker)
                fresh.append(worker)
        for worker in fresh:
            worker.start()
        return fresh

    def _worker_loop(self) -> None:
        """Egy munkásszál: sorra veszi a hátralévő fájlokat, amíg van dolga."""
        while True:
            with self._lock:
                surplus = self._active > self.threads      # időközben csökkent a szálszám
                item = (self._todo.popleft()
                        if not surplus and not self._stop.is_set() and self._todo else None)
                if item is None:
                    self._active -= 1                      # visszavonulás
                    return
            self._download(item)

    def _run(self) -> None:
        with self._lock:
            self._todo = deque(self.pending())
            self._pool = []
            self._active = 0
            pending = len(self._todo)
        if not pending:
            self.on_event("log", "Nincs letöltendő fájl.")
            self.on_event("finished", None)
            return
        self.on_event("log", f"{pending} fájl letöltése {self.threads} szálon...")
        self._speed_mark = (time.monotonic(), self.totals.bytes_done)
        threading.Thread(target=self._autosave, daemon=True).start()
        try:
            while not self._stop.is_set():
                self._scale_workers()
                with self._lock:
                    finished = not self._todo and self._active == 0
                if finished or self._stop.wait(SUPERVISOR_TICK):
                    break
            for worker in list(self._pool):        # a futó fájlok befejezése/leállása
                worker.join(timeout=30)
        finally:
            self.store.save(self.items, force=True)
            self.on_event("finished", None)

    def _autosave_interval(self) -> float:
        """A mentés üteme a lista méretéhez igazodik.

        Húszezer elemnél egy mentés ~200 ms processzoridő és ~4,5 MB írás; ezt
        három másodpercenként megismételni pazarlás. Kevesebb mentéssel sem vész
        el letöltött bájt: a haladást a ``.part`` fájlok hordozzák, a méretüket a
        következő indulás úgyis a lemezről olvassa vissza.
        """
        return min(AUTOSAVE_MAX_SECONDS,
                   AUTOSAVE_SECONDS + len(self.items) * AUTOSAVE_PER_ITEM)

    def _autosave(self) -> None:
        while self.running:
            if self._stop.wait(self._autosave_interval()):
                break
            try:
                self.store.save(self.items)
            except Exception as exc:      # a mentő szál soha ne haljon el némán
                log.warning("Automatikus állapotmentés sikertelen: %s", exc)

    # -- egy fájl ---------------------------------------------------------
    def _remote_size(self, url: str) -> int | None:
        """A fájl mérete a szerveren, HEAD kéréssel. None, ha nem deríthető ki."""
        try:
            resp = self.client.head(url, headers={"Accept-Encoding": "identity"}, timeout=15.0)
        except httpx.HTTPError:
            return None
        if resp.status_code >= HTTP_BAD_REQUEST:
            return None
        if resp.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
            return None                       # tömörített válasz: a hossz nem összevethető
        length = resp.headers.get("content-length", "")
        return int(length) if length.isdigit() else None

    def is_intact(self, item: Item) -> bool:
        """Megvan-e már a fájl épen. Csak akkor kérdezi meg a szervert, ha muszáj.

        Nem módosít státuszt: a hívó dönti el, mit kezd az eredménnyel.
        """
        dest = self.outdir / item.path
        if not dest.exists():
            return False
        size = dest.stat().st_size
        if item.total and size == item.total:
            return True                       # a hossz már igazolt
        remote = self._remote_size(item.url)
        if remote is None:                    # nem ellenőrizhető -> épnek vesszük
            self._set_total(item, item.total or size)
            return True
        if remote == size:
            self._set_total(item, remote)
            return True
        return False

    def classify_existing(self, items: list[Item],
                          on_each: Callable[[Item, bool], None] | None = None) -> int:
        """A meglévő fájlok épségének párhuzamos ellenőrzése. Az épek számát adja vissza.

        Csak azokat nézi, amelyek tényleg ott vannak a lemezen, így a hálózatot
        egyáltalán nem terheli, ha még semmi sincs letöltve.
        """
        present = [i for i in items if (self.outdir / i.path).exists()]
        if not present:
            return 0
        workers = min(self.threads, len(present))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="check") as pool:
            verdicts = list(pool.map(self.is_intact, present))
        for item, verdict in zip(present, verdicts, strict=True):
            if on_each is not None:
                on_each(item, verdict)
        return sum(verdicts)

    def mark_intact(self, item: Item) -> None:
        """Ép, meglévő fájl: késznek jelöljük és levesszük a pipát."""
        dest = self.outdir / item.path
        item.status = Status.DONE
        item.selected = False
        item.done = dest.stat().st_size if dest.exists() else item.done
        item.total = item.total or item.done
        item.force = False
        self.store.mark_dirty()

    def mark_for_redownload(self, item: Item) -> None:
        """A felhasználó a felülírást kérte: a fájl újratöltendő."""
        item.force = True
        item.status = Status.PENDING
        item.selected = True
        item.done = 0
        self._recount()
        self.store.mark_dirty()

    def _accept_existing(self, item: Item, dest: Path) -> bool:
        """Igaz, ha a már meglévő fájl elfogadható, és nem kell újratölteni."""
        size = dest.stat().st_size
        if item.force or self.existing == Existing.REDOWNLOAD:
            return False
        if item.total and size == item.total:
            return True                       # mi töltöttük le, a hossz akkor egyezett
        if self.existing == Existing.SKIP:
            return True
        remote = self._remote_size(item.url)
        if remote is None:                    # nem ellenőrizhető -> nem piszkáljuk
            self._set_total(item, item.total or size)
            return True
        if remote == size:
            self._set_total(item, remote)
            return True
        self.on_event("log", f"Eltérő méret ({human(size)} a lemezen, {human(remote)} a "
                             f"szerveren), újratöltés: {item.path}")
        return False

    def _download(self, item: Item) -> None:
        dest = self.outdir / item.path
        part = dest.with_name(dest.name + ".part")
        note(f"Letöltés: {item.url} -> {dest}")
        try:
            self._gate()
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                if self._accept_existing(item, dest):
                    self._finish(item, dest)
                    return
                part.unlink(missing_ok=True)  # a régi fájl a helyén marad, amíg az új el nem készül
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    self._attempt(item, dest, part)
                    return
                except Retryable as exc:
                    if attempt == MAX_RETRIES:
                        raise
                    delay = min(2 ** attempt, 15)
                    self.on_event("log", f"Újrapróbálkozás {attempt}/{MAX_RETRIES - 1} "
                                         f"{delay}s múlva ({item.path}): {exc}")
                    self._sleep(delay)
        except Cancelled:
            item.status = Status.PAUSED if self.paused else Status.STOPPED
            self._emit(item)
        except Exception as exc:          # szándékosan széles: a hibát a felületen mutatjuk
            item.status = Status.ERROR
            item.error = _brief(exc) if isinstance(exc, (DownloadError, Retryable)) \
                else f"{type(exc).__name__}: {_brief(exc)}"
            self._emit(item)
            self.on_event("log", f"Hiba ({item.path}): {item.error}")
        finally:
            self.store.mark_dirty()

    def _check_status(self, resp: httpx.Response, item: Item,
                      dest: Path, part: Path, done: int) -> bool:
        """Igaz, ha a fájl már ezzel a válasszal elkészült; egyébként hibát dob vagy folytat."""
        if resp.status_code == HTTP_RANGE_NOT_SATISFIABLE:   # nincs ilyen tartomány
            total = parse_content_range_total(resp.headers.get("content-range"))
            if total is not None and done == total:          # a .part valójában teljes
                atomic_replace(part, dest)
                self._finish(item, dest)
                return True
            part.unlink(missing_ok=True)
            raise Retryable("a szerveren megváltozott a fájl mérete")
        if resp.status_code in RETRY_STATUS:
            wait = resp.headers.get("retry-after")
            if wait and wait.isdigit():
                self._sleep(min(int(wait), MAX_RETRY_AFTER))
            raise Retryable(f"HTTP {resp.status_code}")
        if resp.status_code >= HTTP_BAD_REQUEST:
            raise DownloadError(f"HTTP {resp.status_code} {resp.reason_phrase}")
        return False

    def _write_stream(self, resp: httpx.Response, item: Item, part: Path, mode: str) -> None:
        """A válasz testének kiírása, közben szünet/leállítás figyelése."""
        last_ui = last_flush = 0.0
        try:
            with part.open(mode, buffering=FILE_BUFFER) as fh:
                # amint érkezik: így megszakításkor sem vész el a haladás
                for chunk in resp.iter_bytes():
                    self._gate()
                    fh.write(chunk)
                    self._set_done(item, item.done + len(chunk))
                    now = time.monotonic()
                    if now - last_flush > FLUSH_SECONDS:
                        last_flush = now
                        fh.flush()          # a Python pufferéből az operációs rendszerhez
                        os.fsync(fh.fileno())   # onnan a lemezre: áramszünet is túlélhető
                    if now - last_ui > UI_ITEM_THROTTLE:
                        last_ui = now
                        self._emit(item)
        except httpx.HTTPError as exc:      # félbeszakadt kapcsolat
            raise Retryable(_brief(exc)) from exc

    def _attempt(self, item: Item, dest: Path, part: Path) -> None:
        if not item.resumable:            # korábban kiderült: nem folytatható
            part.unlink(missing_ok=True)
        done = part.stat().st_size if part.exists() else 0
        # Az "identity" kérése nélkül a szerver tömörítve küldene: ilyenkor a
        # letöltött bájtok száma nem egyezik a fájlmérettel, és a folytatás
        # bájteltolása értelmét vesztené.
        headers = {"Accept-Encoding": "identity"}
        if done:
            headers["Range"] = f"bytes={done}-"
            if item.validator:
                headers["If-Range"] = item.validator  # változott fájl -> teljes újratöltés

        try:
            stream = self.client.stream("GET", item.url, headers=headers)
        except httpx.HTTPError as exc:
            raise Retryable(_brief(exc)) from exc

        with stream as resp:
            if self._check_status(resp, item, dest, part, done):
                return

            plan = plan_write(resp, done)
            if plan.restart and done:
                self.on_event("log", "A szerver nem támogatja a folytatást, "
                                     f"újratöltés: {item.path}")
            item.resumable = plan.resumable
            item.validator = resp.headers.get("etag") or resp.headers.get("last-modified")
            self._set_total(item, plan.total)
            self._set_done(item, plan.offset)
            item.status = Status.RUNNING
            item.error = ""
            self._emit(item)

            self._write_stream(resp, item, part, plan.mode)
            total = plan.total

            if total and item.done != total:
                raise Retryable(f"csonka válasz ({item.done}/{total} bájt)")

        atomic_replace(part, dest)
        self._finish(item, dest)

    def _finish(self, item: Item, dest: Path) -> None:
        size = dest.stat().st_size
        self._set_done(item, size)
        self._set_total(item, item.total or size)
        if item.status != Status.DONE:
            item.status = Status.DONE
            with self._lock:
                self.totals.done_files += 1
        item.force = False
        item.error = ""
        self.store.mark_dirty()
        self._emit(item)
        self.on_event("log", f"Kész: {item.path} ({human(size)})")

    # -- könyvelés --------------------------------------------------------
    def _set_total(self, item: Item, total: int) -> None:
        if total and total != item.total:
            with self._lock:
                self.totals.bytes_total += total - item.total
            item.total = total

    def _set_done(self, item: Item, value: int) -> None:
        """Az összesítő mindig az elemek különbségéből épül, így nem csúszhat el."""
        with self._lock:
            self.totals.bytes_done += value - item.done
        item.done = value

    def speed(self) -> float:
        """Pillanatnyi sebesség (bájt/mp), exponenciális simítással."""
        now = time.monotonic()
        prev_t, prev_b = self._speed_mark
        elapsed = now - prev_t
        if elapsed < SPEED_WINDOW:
            return self._speed
        sample = max(0, self.totals.bytes_done - prev_b) / elapsed
        self._speed = sample if self._speed == 0 else self._speed * 0.7 + sample * 0.3
        self._speed_mark = (now, self.totals.bytes_done)
        return self._speed

    def eta(self) -> float | None:
        speed = self._speed
        remaining = self.totals.bytes_total - self.totals.bytes_done
        return remaining / speed if speed > 1 and remaining > 0 else None

    def _emit(self, item: Item) -> None:
        self.on_event("item", item)


# --------------------------------------------------------------------------- #
#  Grafikus felület
# --------------------------------------------------------------------------- #

def enable_dpi_awareness() -> None:
    """Windows 10/11 alatt élessé teszi a felületet a nagyított kijelzőkön.

    A Tk alapból nem DPI-tudatos, ezért a Windows nagyítja fel a képet, ami
    elmosódott szöveget ad. A hívásnak a Tk() létrehozása ELŐTT kell megtörténnie.
    """
    if sys.platform != "win32":
        return
    import ctypes  # noqa: PLC0415 - csak Windowson kell
    with suppress(Exception):                     # régi Windows: nincs shcore
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # PROCESS_SYSTEM_DPI_AWARE


def run_gui() -> int:                                   # pragma: no cover (GUI)
    """A grafikus felület indítása a külön modulból.

    A tkinter importja szándékosan itt történik: parancssori módban a program
    tkinter nélküli rendszeren (például csupasz szerveren) is elfut.
    """
    try:
        from letolto_gui import run_gui as start_gui  # noqa: PLC0415 - lusta import
    except ModuleNotFoundError as exc:                # hiányzó tkinter vagy letolto_gui.py
        raise SystemExit(
            "A grafikus felület nem indítható: hiányzik a tkinter vagy a letolto_gui.py.\n"
            f"Részletek: {exc}\n"
            "Parancssori mód: python letolto.py --no-gui -o <mappa> <URL>"
        ) from exc
    return start_gui()




# --------------------------------------------------------------------------- #
#  Parancssori mód
# --------------------------------------------------------------------------- #

def run_cli(args: argparse.Namespace) -> int:
    # Windowson a konzol kódlapja cp852/cp1250; fájlba irányított kimenetnél az
    # ékezetes vagy tipográfiai karakterek UnicodeEncodeError-t okoznának.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    outdir = Path(args.out).expanduser()
    wanted = parse_ext_filter(args.ext or "")
    client = make_client(args.threads)
    manager = DownloadManager(outdir, args.threads, on_event=_cli_event, client=client,
                              existing=args.meglevo)
    manager.load_state()
    if args.url:
        cfg = ScanConfig(root=args.url, depth=args.depth,
                         same_host=not args.any_host, respect_robots=not args.ignore_robots,
                         stop_on_robots_error=args.robots_5xx_stop)
        found = Scanner(cfg, client, log.info, threading.Event()).run()
        groups = found.by_extension()
        chosen = matching_extensions(found, wanted, args.html)
        log.info("Talált kiterjesztések: %s", ", ".join(
            f"{name} ({len(urls)}){'' if name in chosen else ' [kihagyva]'}"
            for name, urls in groups.items()))
        labels = {url: name for name, urls in groups.items() for url in urls}
        picked = [url for name in chosen for url in groups[name]]
        manager.add_urls(picked, labels=labels)
        log.info("Letöltendő fájlok: %d", len(picked))
    manager.start()
    try:
        while manager.running:
            time.sleep(1)
            totals = manager.totals
            print(f"\r{totals.done_files}/{totals.files} kész | "
                  f"{human(totals.bytes_done)}/{human(totals.bytes_total)} | "
                  f"{human(manager.speed())}/s   ", end="", flush=True)
    except KeyboardInterrupt:
        print("\nMegszakítás... (később folytatható)")
        manager.stop()
    finally:
        manager.close()
        client.close()
    print()
    errors = [i for i in manager.items.values() if i.status == Status.ERROR]
    for item in errors:
        log.error("HIBA %s - %s", item.path, item.error)
    return 1 if errors else 0


def _cli_event(kind: str, payload: object) -> None:
    if kind == "log":
        note(str(payload))                   # ugyanez a sor a naplófájlba is
        print("\r" + str(payload).ljust(78))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="letolto", description="Weboldal-fájlletöltő (folytatható, többszálú).")
    parser.add_argument("url", nargs="?", help="a kiindulási oldal címe")
    parser.add_argument("-o", "--out", default=str(Path.home() / "letoltesek"),
                        help="célkönyvtár")
    parser.add_argument("-t", "--threads", type=int, default=4,
                        help=f"párhuzamos szálak száma (1-{MAX_THREADS})")
    parser.add_argument("-d", "--depth", type=int, default=0, help="bejárási mélység")
    parser.add_argument("-e", "--ext", default="",
                        help="kiterjesztések vesszővel, pl. pdf,zip (üresen: mind)")
    parser.add_argument("--html", action="store_true", help="a HTML lapok is töltődjenek le")
    parser.add_argument("--any-host", action="store_true", help="más domainek is")
    parser.add_argument("--ignore-robots", action="store_true", help="robots.txt figyelmen kívül")
    parser.add_argument("--robots-5xx-stop", action="store_true",
                        help="ha a robots.txt 5xx miatt elérhetetlen, álljon le az átvizsgálás")
    parser.add_argument("--meglevo", choices=[str(e) for e in Existing],
                        default=str(Existing.VERIFY),
                        help="mi történjék a már meglévő fájlokkal")
    parser.add_argument("--no-gui", action="store_true", help="parancssori futtatás")
    parser.add_argument("-V", "--version", action="version", version=f"letolto {__version__}")
    args = parser.parse_args(argv)
    cli = bool(args.no_gui or args.url)
    setup_file_log()
    note("-" * 70)
    note(f"PyLetolto {__version__} indul ({'parancssor' if cli else 'grafikus felület'}), "
         f"Python {sys.version.split()[0]}, {sys.platform}")
    try:
        return run_cli(args) if cli else run_gui()
    finally:
        note(f"PyLetolto {__version__} kilép")


if __name__ == "__main__":
    # A "python letolto.py" hívásnál ez a fájl __main__ néven fut. A felület
    # modulja viszont "letolto" néven importálja - enélkül a Python másodszor is
    # betöltené ezt a fájlt, és a felület más osztályokat és más beállításokat
    # látna, mint a mag. Ez a sor a kettőt ugyanarra a modulra köti.
    sys.modules.setdefault("letolto", sys.modules[__name__])
    raise SystemExit(main())
