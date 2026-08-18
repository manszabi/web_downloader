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
import queue
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
from functools import partial
from dataclasses import field as dataclass_field
from enum import StrEnum
from hashlib import blake2b
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Final
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
NET_CHUNK: Final = 256 * 1024          # írási puffer alapmérete
FILE_BUFFER: Final = NET_CHUNK         # mérve ez a leggyorsabb és kevés adat vész
FLUSH_SECONDS: Final = 5.0             # el összeomláskor (lásd a README mérését)
STATE_FILE: Final = "_letoltes_allapot.json"
def _settings_path() -> Path:
    """Windowson a %APPDATA%, máshol a home könyvtár a szokásos hely."""
    appdata = os.environ.get("APPDATA")
    if sys.platform == "win32" and appdata:
        return Path(appdata) / "PyLetolto" / "beallitasok.json"
    return Path.home() / ".letolto_beallitasok.json"


SETTINGS_FILE: Final = _settings_path()
STATE_VERSION: Final = 2
MAX_RETRIES: Final = 4
MAX_RETRY_AFTER: Final = 30            # a Retry-After fejlécet ennyi másodpercre korlátozzuk
MAX_EXT_LEN: Final = 10                # ennél hosszabb utótagot nem tekintünk kiterjesztésnek
HTTP_OK: Final = 200
HTTP_PARTIAL: Final = 206
HTTP_BAD_REQUEST: Final = 400
HTTP_RANGE_NOT_SATISFIABLE: Final = 416
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
_WIN_RESERVED: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{d}" for d in "123456789¹²³"}
    | {f"LPT{d}" for d in "123456789¹²³"}
)
_ILLEGAL_CHARS: Final = re.compile(r'[<>:"|?*\\/\x00-\x1f]')
_MAX_COMPONENT: Final = 100            # egy útvonalelem max hossza
MAX_ABS_PATH: Final = 240              # a Windows MAX_PATH (260) alatt maradunk
_MAX_PATH: Final = 240                 # visszafelé kompatibilis név


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


class Retryable(Exception):
    """Átmeneti hiba, újrapróbálható."""


class DownloadError(Exception):
    """Végleges hiba egy adott fájlnál."""


def _brief(exc: BaseException) -> str:
    """Rövid, felületre való hibaszöveg (a httpx üzenetei többsorosak)."""
    text = str(exc).strip().splitlines()
    return (text[0] if text else type(exc).__name__)[:120]


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
    return name.partition(".")[0].upper()


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
    if len(str(rel)) > _MAX_PATH:                 # túl hosszú út lapítása
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
    def _allowed(self, url: str) -> bool:
        if not self.cfg.respect_robots:
            return True
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            rules = RobotsRules()          # hiányzó vagy hibás robots.txt: nincs tiltás
            try:
                resp = self.client.get(f"{origin}/robots.txt", timeout=10.0)
                if resp.status_code == HTTP_OK:
                    rules = RobotsRules.parse(resp.text)
            except httpx.HTTPError:
                pass
            self._robots[origin] = rules
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
            if not self._allowed(url):
                self.on_log(f"robots.txt tiltja: {url}")
                continue

            try:
                page = self._read_page(url)
            except Cancelled:
                break
            if page is None:                       # nem HTML -> maga is fájl
                if url not in seen_files:
                    seen_files.add(url)
                    result.files.append(url)
                continue

            result.pages.append(url)               # ez egy letölthető HTML lap
            base, links = page
            self.on_log(f"Átvizsgálás (mélység {depth}): {url} - {len(links)} hivatkozás")
            self._sort_links(links, base, depth, crawl)

        if len(visited) >= self.cfg.max_pages:
            self.on_log(f"Elértük az oldalkorlátot ({self.cfg.max_pages}).")
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
    def from_dict(cls, raw: dict) -> Item:
        return cls(url=raw["url"], path=raw["path"], total=int(raw.get("total", 0)),
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
        payload = {"version": STATE_VERSION,
                   "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "items": {url: item.as_dict() for url, item in items.items()}}
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                self.outdir.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(data, encoding="utf-8")
                os.replace(tmp, self.path)          # atomi csere
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
        key = str(rel)
        if key.casefold() not in self._used_paths:
            self._used_paths.add(key.casefold())
            return key
        alt = fit_path(self.outdir,
                       rel.with_name(f"{rel.stem}_{_short_hash(url)}{rel.suffix}"))
        self._used_paths.add(str(alt).casefold())
        return str(alt)

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
                if finished:
                    break
                time.sleep(SUPERVISOR_TICK)
            for worker in list(self._pool):        # a futó fájlok befejezése/leállása
                worker.join(timeout=30)
        finally:
            self.store.save(self.items, force=True)
            self.on_event("finished", None)

    def _autosave(self) -> None:
        while self.running:
            if self._stop.wait(3.0):
                break
            self.store.save(self.items)

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
                        fh.flush()          # áramszünetkor is csak pár másodperc vész el
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
    import tkinter as tk  # noqa: PLC0415 - csak a GUI módnak kell
    from tkinter import filedialog, messagebox, ttk  # noqa: PLC0415

    COLUMNS = (("pipa", "✓", 34), ("fajl", "Fájl", 400), ("meret", "Méret", 90),
               ("letoltve", "Letöltve", 90), ("szazalek", "%", 60),
               ("allapot", "Állapot", 150))

    class OverwriteDialog(tk.Toplevel):
        """Rákérdezés egy meglévő, ép fájl felülírására: Igen / Nem / Összes."""

        def __init__(self, parent: tk.Tk, path: str, remaining: int) -> None:
            super().__init__(parent)
            self.result = "nem"                  # az ablak bezárása = ne írjuk felül
            self.title("Felülírás?")
            self.resizable(False, False)
            self.transient(parent)

            body = ttk.Frame(self, padding=14)
            body.pack(fill="both", expand=True)
            ttk.Label(body, text="Ez a fájl már megvan, és épnek tűnik:").pack(anchor="w")
            ttk.Label(body, text=path, font=("TkDefaultFont", 9, "bold"),
                      wraplength=460).pack(anchor="w", pady=(4, 8))
            ttk.Label(body, text="Felülírjam?").pack(anchor="w")
            if remaining > 1:
                ttk.Label(body, text=f"(még {remaining - 1} ilyen fájl van kijelölve - "
                                     f"az Összes ezekre is igent mond)",
                          foreground="#555").pack(anchor="w", pady=(4, 0))

            row = ttk.Frame(body)
            row.pack(fill="x", pady=(12, 0))
            for text, value in (("Igen", "igen"), ("Nem", "nem"), ("Összes", "osszes")):
                ttk.Button(row, text=text, width=10,
                           command=partial(self._choose, value)).pack(side="right", padx=4)

            self.bind("<Escape>", lambda _e: self._choose("nem"))
            self.bind("<Return>", lambda _e: self._choose("igen"))
            self.protocol("WM_DELETE_WINDOW", lambda: self._choose("nem"))
            self.update_idletasks()
            self.geometry(f"+{parent.winfo_rootx() + 80}+{parent.winfo_rooty() + 120}")
            with suppress(tk.TclError):
                self.grab_set()                  # modális: amíg nincs válasz, nincs tovább
            parent.wait_window(self)

        def _choose(self, value: str) -> None:
            self.result = value
            with suppress(tk.TclError):
                self.grab_release()
            self.destroy()

    class App(tk.Tk):
        def __init__(self) -> None:
            super().__init__()
            self.title(f"Weboldal-letöltő {__version__}")
            self.scale = self._display_scale()
            self.tk.call("tk", "scaling", self.scale * 96 / 72)   # betűk a DPI-hez
            self.geometry(f"{int(1040 * self.scale)}x{int(740 * self.scale)}")
            self.minsize(int(900 * self.scale), int(620 * self.scale))
            self.events: queue.Queue[tuple[str, object]] = queue.Queue()
            self.manager: DownloadManager | None = None
            self.scan_stop = threading.Event()
            self.scanning = False
            self._pending_rows: dict[str, tuple] = {}
            self._known_rows: set[str] = set()
            self._ext_vars: dict[str, tk.BooleanVar] = {}
            self._ext_sync = False              # a két irány ne írja egymást körbe-körbe
            self._ext_filter_job: str | None = None
            # Külön attribútum, hogy teszteléskor kicserélhető legyen.
            self.overwrite_dialog = OverwriteDialog
            self._log_lines = 0
            self._autoload_job: str | None = None
            self._pump_job: str | None = None
            self._closing = False
            self._build()
            self._restore_settings()
            self.v_dir.trace_add("write", self._on_dir_changed)
            self.v_threads.trace_add("write", self._on_threads_changed)
            self.v_existing.trace_add("write", self._on_existing_changed)
            self.v_ext.trace_add("write", self._on_ext_filter_changed)
            self.v_html.trace_add("write", self._on_ext_filter_changed)
            self._pump_job = self.after(UI_TICK_MS, self._pump)
            self.after(200, self._autoload_state)     # összeomlás utáni folytatás felkínálása
            self.protocol("WM_DELETE_WINDOW", self._on_close)

        # -- beállítások megjegyzése -------------------------------------
        def _display_scale(self) -> float:
            """A kijelző nagyítása (1.0 = 96 DPI). Windows 11-en gyakori az 1.25-1.5."""
            try:
                dpi = float(self.winfo_fpixels("1i"))
            except (tk.TclError, ValueError):
                return 1.0
            return min(max(dpi / 96.0, 1.0), 3.0)          # épeszű határok közé szorítva

        def _restore_settings(self) -> None:
            """Az előző futás beállításai, hogy újraindítás után ne kelljen újra begépelni."""
            try:
                saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            for name, var in (("url", self.v_url), ("dir", self.v_dir), ("ext", self.v_ext),
                              ("depth", self.v_depth), ("threads", self.v_threads),
                              ("same_host", self.v_same), ("robots", self.v_robots),
                              ("existing", self.v_existing), ("html", self.v_html)):
                if name in saved:
                    with suppress(tk.TclError, ValueError):
                        var.set(saved[name])

        def _save_settings(self) -> None:
            data = {"url": self.v_url.get(), "dir": self.v_dir.get(), "ext": self.v_ext.get(),
                    "depth": self.v_depth.get(), "threads": self.v_threads.get(),
                    "same_host": self.v_same.get(), "robots": self.v_robots.get(),
                    "existing": self.v_existing.get(), "html": self.v_html.get()}
            # Ugyanaz a minta, mint az állapotfájlnál: előbb ideiglenes fájlba írunk,
            # és csak a kész tartalom lép a régi helyére. Így áramszünet vagy
            # összeomlás esetén sem marad csonka, olvashatatlan beállításfájl.
            with suppress(OSError, TypeError):
                SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
                tmp = SETTINGS_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                atomic_replace(tmp, SETTINGS_FILE)

        def _on_existing_changed(self, *_args: object) -> None:
            if self.manager is not None:
                self.manager.existing = self.v_existing.get()

        def _on_threads_changed(self, *_args: object) -> None:
            """A szálszám azonnal érvényesül, futó letöltés közben is."""
            try:
                count = self.v_threads.get()
            except tk.TclError:              # a mezőbe épp félkész érték került
                return
            if self.manager is not None and 1 <= count <= MAX_THREADS:
                self.manager.set_threads(count)

        # -- félbemaradt munka automatikus felismerése --------------------
        def _on_dir_changed(self, *_args: object) -> None:
            """Gépelés közben ne fusson minden leütésre; kis késleltetéssel nézzük meg."""
            if self._autoload_job is not None:
                with suppress(tk.TclError):
                    self.after_cancel(self._autoload_job)
            self._autoload_job = self.after(800, self._autoload_state)

        def _autoload_state(self) -> None:
            """Ha a célkönyvtárban van állapotfájl, magától betölti és jelzi a folytathatót."""
            self._autoload_job = None
            raw = self.v_dir.get().strip()
            if not raw:
                return
            outdir = Path(raw).expanduser()
            # Szándékosan nem hozunk létre könyvtárat: csak meglévő munkát olvasunk be.
            if not (outdir / STATE_FILE).is_file():
                return
            if self.manager is not None and self.manager.outdir == outdir:
                return
            if self.manager is not None:
                self.manager.close()
            self.manager = DownloadManager(outdir, self.v_threads.get(), on_event=self._on_event)
            self.manager.load_state()
            unfinished = len(self.manager.pending())
            partial = sum(1 for i in self.manager.items.values()
                          if i.done and i.status != Status.DONE)
            if unfinished:
                self._write_log(f"Korábbi munka található: {unfinished} befejezetlen fájl "
                                f"(ebből {partial} részben letöltve). Az Indítás onnan folytatja.")
                self.v_status.set(f"{unfinished} befejezetlen letöltés folytatható - "
                                  f"nyomd meg az Indítás gombot.")
            else:
                self._write_log(f"A célkönyvtárban {len(self.manager.items)} korábbi "
                                f"letöltés található, mind kész.")

        # -- felépítés ---------------------------------------------------
        def _build(self) -> None:
            self._build_options()
            self._build_toolbar()
            self._build_table()

        def _build_options(self) -> None:
            box = ttk.LabelFrame(self, text="Beállítások")
            box.pack(fill="x", padx=6, pady=4)
            def label(text: str, row: int, col: int) -> None:
                ttk.Label(box, text=text).grid(row=row, column=col, sticky="e", padx=4, pady=3)

            label("URL:", 0, 0)
            self.v_url = tk.StringVar(value="https://")
            entry = ttk.Entry(box, textvariable=self.v_url)
            entry.grid(row=0, column=1, columnspan=5, sticky="we", padx=4, pady=3)
            entry.bind("<Return>", lambda _e: self._scan())

            label("Célkönyvtár:", 1, 0)
            self.v_dir = tk.StringVar(value=str(Path.home() / "letoltesek"))
            ttk.Entry(box, textvariable=self.v_dir).grid(
                row=1, column=1, columnspan=4, sticky="we", padx=4, pady=3)
            ttk.Button(box, text="Tallózás…", command=self._browse).grid(row=1, column=5, padx=4)

            label("Talált kiterjesztések:", 2, 0)
            self._build_extension_area(box)

            label("Kiterjesztések:", 3, 0)
            self.v_ext = tk.StringVar(value="")
            ttk.Entry(box, textvariable=self.v_ext, width=30).grid(
                row=3, column=1, sticky="we", padx=4, pady=3)
            self.v_html = tk.BooleanVar(value=False)
            ttk.Checkbutton(box, text="HTML letöltése", variable=self.v_html).grid(
                row=3, column=2, sticky="w")
            ttk.Label(box, text="(üresen: minden kiterjesztés)").grid(
                row=3, column=3, columnspan=2, sticky="w")

            label("Mélység:", 4, 0)
            self.v_depth = tk.IntVar(value=0)
            ttk.Spinbox(box, from_=0, to=8, width=5, textvariable=self.v_depth).grid(
                row=4, column=1, sticky="w", padx=4)

            label("Szálak:", 4, 3)
            self.v_threads = tk.IntVar(value=4)
            ttk.Spinbox(box, from_=1, to=MAX_THREADS, width=5, textvariable=self.v_threads).grid(
                row=4, column=4, sticky="w")

            self.v_same = tk.BooleanVar(value=True)
            ttk.Checkbutton(box, text="csak azonos domain", variable=self.v_same).grid(
                row=5, column=1, sticky="w", padx=4)
            self.v_robots = tk.BooleanVar(value=True)
            ttk.Checkbutton(box, text="robots.txt betartása", variable=self.v_robots).grid(
                row=5, column=2, sticky="w")

            label("Meglévő fájl:", 6, 0)
            self.v_existing = tk.StringVar(value=str(Existing.VERIFY))
            ttk.Combobox(box, textvariable=self.v_existing, state="readonly", width=18,
                         values=[str(e) for e in Existing]).grid(row=6, column=1,
                                                                 sticky="w", padx=4, pady=3)
            ttk.Label(box, text="(a méret-ellenőrzés kiszűri a csonka fájlokat)").grid(
                row=6, column=2, columnspan=3, sticky="w")
            box.columnconfigure(1, weight=1)

        def _build_extension_area(self, box: ttk.LabelFrame) -> None:
            """Görgethető panel a talált kiterjesztések jelölőnégyzeteinek."""
            wrap = ttk.Frame(box)
            wrap.grid(row=2, column=1, columnspan=4, sticky="we", padx=4, pady=3)
            self.ext_canvas = tk.Canvas(wrap, height=58, highlightthickness=0,
                                        borderwidth=1, relief="sunken")
            ext_scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.ext_canvas.yview)
            self.ext_canvas.configure(yscrollcommand=ext_scroll.set)
            self.ext_canvas.pack(side="left", fill="both", expand=True)
            ext_scroll.pack(side="right", fill="y")
            self.ext_frame = ttk.Frame(self.ext_canvas)
            self.ext_window = self.ext_canvas.create_window((0, 0), window=self.ext_frame,
                                                            anchor="nw")
            self.ext_frame.bind("<Configure>", lambda _e: self.ext_canvas.configure(
                scrollregion=self.ext_canvas.bbox("all")))
            self.ext_canvas.bind("<Configure>", lambda e: self.ext_canvas.itemconfigure(
                self.ext_window, width=e.width))
            self.ext_hint = ttk.Label(self.ext_frame,
                                      text="Az átvizsgálás után itt jelennek meg a talált "
                                           "kiterjesztések.")
            self.ext_hint.grid(row=0, column=0, sticky="w", padx=4, pady=2)

            ext_buttons = ttk.Frame(box)
            ext_buttons.grid(row=2, column=5, sticky="n", padx=4)
            ttk.Button(ext_buttons, text="Összes", width=9,
                       command=lambda: self._set_all_extensions(True)).pack(pady=1)
            ttk.Button(ext_buttons, text="Egyik sem", width=9,
                       command=lambda: self._set_all_extensions(False)).pack(pady=1)

        def _build_toolbar(self) -> None:
            bar = ttk.Frame(self)
            bar.pack(fill="x", padx=6, pady=4)
            self.b_scan = ttk.Button(bar, text="Átvizsgálás", command=self._scan)
            self.b_load = ttk.Button(bar, text="Korábbi állapot", command=self._load_state)
            self.b_start = ttk.Button(bar, text="Indítás / Folytatás", command=self._start)
            self.b_pause = ttk.Button(bar, text="Szünet", command=self._pause, state="disabled")
            self.b_stop = ttk.Button(bar, text="Leállítás", command=self._stop, state="disabled")
            self.b_open = ttk.Button(bar, text="Mappa megnyitása", command=self._open_dir)
            self.b_settings = ttk.Button(bar, text="Beállítások mappája",
                                         command=self._open_settings_dir)
            for button in (self.b_scan, self.b_load, self.b_start, self.b_pause,
                           self.b_stop, self.b_open, self.b_settings):
                button.pack(side="left", padx=3)

        def _build_table(self) -> None:
            head = ttk.Frame(self)
            head.pack(fill="x", padx=6)
            ttk.Label(head, text="Fájlok:").pack(side="left")
            ttk.Button(head, text="Összes kijelölése",
                       command=lambda: self._set_all_files(True)).pack(side="left", padx=3)
            ttk.Button(head, text="Kijelölés törlése",
                       command=lambda: self._set_all_files(False)).pack(side="left", padx=3)
            self.v_count = tk.StringVar(value="")
            ttk.Label(head, textvariable=self.v_count).pack(side="left", padx=8)

            frame = ttk.Frame(self)
            frame.pack(fill="both", expand=True, padx=6)
            self.tree = ttk.Treeview(frame, columns=[c[0] for c in COLUMNS],
                                     show="headings", height=14)
            for key, title, width in COLUMNS:
                self.tree.heading(key, text=title)
                self.tree.column(key, width=int(width * self.scale),
                                 anchor="w" if key == "fajl" else "center")
            scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=scroll.set)
            self.tree.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")
            self.tree.bind("<Button-1>", self._on_tree_click)
            self.tree.bind("<space>", self._on_tree_space)

            bottom = ttk.Frame(self)
            bottom.pack(fill="x", padx=6, pady=4)
            self.progress = ttk.Progressbar(bottom, maximum=1000)
            self.progress.pack(fill="x")
            self.v_status = tk.StringVar(value="Készen áll.")
            ttk.Label(bottom, textvariable=self.v_status).pack(anchor="w", pady=2)

            self.log = tk.Text(self, height=7, wrap="none", state="disabled")
            self.log.pack(fill="both", padx=6, pady=(0, 6))

        # -- segédek ------------------------------------------------------
        def _write_log(self, message: str) -> None:
            self.log.configure(state="normal")
            self.log.insert("end", message + "\n")
            self._log_lines += 1
            if self._log_lines > LOG_MAX_LINES:      # a napló nem nőhet korlátlanul
                self.log.delete("1.0", f"{self._log_lines - LOG_MAX_LINES + 1}.0")
                self._log_lines = LOG_MAX_LINES
            self.log.see("end")
            self.log.configure(state="disabled")

        def _browse(self) -> None:
            chosen = filedialog.askdirectory(initialdir=self.v_dir.get() or str(Path.home()))
            if chosen:
                self.v_dir.set(chosen)

        def _open_dir(self) -> None:
            target = Path(self.v_dir.get())
            if target.is_dir():
                open_in_file_manager(target)
            else:
                messagebox.showinfo("Info", "A célkönyvtár még nem létezik.")

        def _open_settings_dir(self) -> None:
            """A beállításfájl mappájának megnyitása külön fájlkezelő ablakban."""
            try:
                SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("Hiba", f"A beállítások mappája nem érhető el:\n{exc}")
                return
            if not SETTINGS_FILE.is_file():
                self._save_settings()      # legyen mit megnézni az első indításkor is
            reveal_in_file_manager(SETTINGS_FILE)
            self._write_log(f"Beállítások helye: {SETTINGS_FILE}")

        def _ensure_manager(self) -> DownloadManager | None:
            raw = self.v_dir.get().strip()
            if not raw:
                messagebox.showerror("Hiba", "Adj meg egy célkönyvtárat!")
                return None
            outdir = Path(raw).expanduser()
            try:
                outdir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("Hiba", f"A könyvtár nem hozható létre:\n{exc}")
                return None
            if self.manager is None or self.manager.outdir != outdir:
                if self.manager is not None:
                    self.manager.close()
                self.manager = DownloadManager(outdir, self.v_threads.get(),
                                               on_event=self._on_event)
                self.manager.load_state()
            self.manager.set_threads(self.v_threads.get())
            return self.manager

        def _on_event(self, kind: str, payload: object) -> None:
            """Háttérszálakból hívódik - csak sorba tesszük, a GUI-t nem érintjük."""
            self.events.put((kind, payload))

        # -- műveletek ----------------------------------------------------
        def _scan(self) -> None:
            manager = self._ensure_manager()
            if manager is None or self.scanning:
                return
            url = self.v_url.get().strip()
            if not url.startswith(("http://", "https://")):
                messagebox.showerror("Hiba", "Érvényes http(s) URL szükséges.")
                return
            cfg = ScanConfig(root=url, depth=self.v_depth.get(),
                             same_host=self.v_same.get(),
                             respect_robots=self.v_robots.get())
            self.scanning = True
            self.scan_stop.clear()
            self.b_scan.configure(state="disabled")
            self.v_status.set("Átvizsgálás folyamatban…")

            def work() -> None:
                scanner = Scanner(cfg, manager.client,
                                  lambda m: self.events.put(("log", m)), self.scan_stop)
                try:
                    found = scanner.run()
                except Exception as exc:
                    self.events.put(("log", f"Átvizsgálási hiba: {exc}"))
                    found = ScanResult()
                self.events.put(("scanned", found))

            threading.Thread(target=work, daemon=True, name="scanner").start()

        def _load_state(self) -> None:
            manager = self._ensure_manager()
            if manager is None:
                return
            manager.load_state()
            self._write_log(f"{len(manager.items)} bejegyzés betöltve az állapotfájlból.")

        def _confirm_overwrites(self, manager: DownloadManager) -> bool:
            """Rákérdez a már meglévő, ép fájlok felülírására. Hamis = ne induljon a letöltés."""
            candidates = [i for i in manager.items.values()
                          if i.selected and i.status == Status.DONE and not i.force]
            if not candidates:
                return True
            answer = ""
            for index, item in enumerate(candidates):
                if answer != "osszes":
                    answer = self.overwrite_dialog(self, item.path,
                                                   len(candidates) - index).result
                if answer in ("igen", "osszes"):
                    manager.mark_for_redownload(item)
                else:                            # nem: marad a meglévő fájl
                    manager.set_selected([item.url], False)
                self._queue_row(item)
            self._flush_rows()
            self._update_count()
            return True

        def _start(self) -> None:
            manager = self._ensure_manager()
            if manager is None:
                return
            if manager.paused:
                manager.pause(False)
                self.b_pause.configure(text="Szünet")
                self.v_status.set("Letöltés folytatva.")
                return
            if not self._confirm_overwrites(manager):
                return
            if not manager.pending():
                if manager.items:
                    messagebox.showinfo("Info", "Nincs kipipálva letöltendő fájl. Jelöld ki, "
                                                "mi kell: a kiterjesztéseknél vagy a listában.")
                else:
                    messagebox.showinfo("Info", "Nincs letöltendő fájl. Előbb vizsgáld át az "
                                                "oldalt, vagy tölts be korábbi állapotot.")
                return
            manager.start()
            self.b_start.configure(state="disabled")
            self.b_pause.configure(state="normal")
            self.b_stop.configure(state="normal")

        def _pause(self) -> None:
            if self.manager is None:
                return
            paused = not self.manager.paused
            self.manager.pause(paused)
            self.b_pause.configure(text="Folytatás" if paused else "Szünet")
            self.v_status.set("Szüneteltetve - bármikor folytatható."
                              if paused else "Letöltés folytatva.")

        def _stop(self) -> None:
            self.scan_stop.set()
            if self.manager is not None:
                self.manager.stop()
                self.v_status.set("Leállítás…")

        # -- eseményhurok --------------------------------------------------
        def _pump(self) -> None:
            """Egyetlen GUI-szálon futó frissítés; a sorból kötegelve dolgozunk."""
            try:
                for _ in range(UI_BATCH):           # egy körben korlátozott adag
                    kind, payload = self.events.get_nowait()
                    self._handle_event(kind, payload)
            except queue.Empty:
                pass
            self._flush_rows()
            self._refresh_status()
            if not self._closing:            # bezárás után ne ütemezzünk újat
                self._pump_job = self.after(UI_TICK_MS, self._pump)

        def _handle_event(self, kind: str, payload: object) -> None:
            """Egy háttéresemény feldolgozása a GUI szálán."""
            match kind:
                case "log":
                    self._write_log(str(payload))
                case "item":
                    if isinstance(payload, Item):
                        self._queue_row(payload)
                case "added" | "reset":
                    self._on_items_batch(payload if isinstance(payload, list) else [],
                                         reset=kind == "reset")
                case "scanned":
                    if isinstance(payload, ScanResult):
                        self._on_scanned(payload)
                case "verified":
                    if isinstance(payload, tuple):
                        self._on_verified(payload)
                case "finished":
                    self._on_finished()

        def _on_items_batch(self, items: list[Item], *, reset: bool) -> None:
            """Elemek kötegelt megjelenítése: betöltés (reset) vagy új találatok (added)."""
            if reset:
                self.tree.delete(*self.tree.get_children())
                self._known_rows.clear()
            for item in items:
                self._queue_row(item)
            if reset and items:
                # Betöltött állapotnál is legyen mit pipálni a kiterjesztés-panelen.
                self._rebuild_panel_from_items()

        def _queue_row(self, item: Item) -> None:
            self._pending_rows[item.url] = (
                CHECKED_MARK if item.selected else UNCHECKED_MARK,
                item.path, human(item.total), human(item.done),
                f"{item.percent:.0f}%" if item.total else "-",
                item.error or str(item.status),
            )

        def _flush_rows(self) -> None:
            if not self._pending_rows:
                return
            tree = self.tree
            for url, values in self._pending_rows.items():
                if url in self._known_rows:
                    tree.item(url, values=values)
                else:
                    tree.insert("", "end", iid=url, values=values)
                    self._known_rows.add(url)
            self._pending_rows.clear()
            self._update_count()

        # -- kijelölés a fájllistában -------------------------------------
        def _on_tree_click(self, event: tk.Event) -> str | None:
            """A pipa oszlopra kattintva vált a fájl kijelölése."""
            if self.tree.identify_region(event.x, event.y) != "cell":
                return None
            if self.tree.identify_column(event.x) != "#1":
                return None
            url = self.tree.identify_row(event.y)
            if url:
                self._toggle_files([url])
                return "break"                # ne induljon sorkijelölés is
            return None

        def _on_tree_space(self, _event: tk.Event) -> None:
            """Szóközzel a kijelölt sorok pipája vált."""
            self._toggle_files(self.tree.selection())

        def _toggle_files(self, urls: Iterable[str]) -> None:
            if self.manager is None:
                return
            for url in urls:
                item = self.manager.items.get(url)
                if item is not None:
                    self.manager.set_selected([url], not item.selected)
                    self._queue_row(item)
            self._flush_rows()

        def _set_all_files(self, value: bool) -> None:
            if self.manager is None:
                return
            self.manager.set_selected(list(self.manager.items), value)
            for item in self.manager.items.values():
                self._queue_row(item)
            self._flush_rows()

        def _update_count(self) -> None:
            if self.manager is None:
                return
            total = len(self.manager.items)
            chosen = sum(1 for i in self.manager.items.values() if i.selected)
            self.v_count.set(f"{chosen} / {total} kijelölve")

        # -- kiterjesztés-panel -------------------------------------------
        def _rebuild_extension_panel(self, groups: dict[str, list[str]],
                                     checked: set[str]) -> None:
            """A talált kiterjesztések kirajzolása jelölőnégyzetekként."""
            for child in self.ext_frame.winfo_children():
                child.destroy()
            self._ext_vars.clear()
            if not groups:
                ttk.Label(self.ext_frame, text="Nem található letölthető fájl.").grid(
                    row=0, column=0, sticky="w", padx=4, pady=2)
                return
            for index, (name, urls) in enumerate(groups.items()):
                var = tk.BooleanVar(value=name in checked)
                self._ext_vars[name] = var
                ttk.Checkbutton(
                    self.ext_frame, text=f"{name} ({len(urls)})", variable=var,
                    command=partial(self._on_extension_toggled, name),
                ).grid(row=index // EXT_COLUMNS, column=index % EXT_COLUMNS,
                       sticky="w", padx=6, pady=1)

        def _apply_ext_selection(self, changes: dict[str, bool]) -> None:
            """Több kiterjesztés kijelölésének érvényesítése egyetlen végigjárással.

            A listát csak egyszer nézzük végig, és csak a ténylegesen változó
            sorokat rajzoljuk újra - húszezer elemnél ez a különbség érezhető.
            """
            manager = self.manager
            if manager is None or not changes:
                return
            buckets: dict[bool, list[str]] = {True: [], False: []}
            for item in manager.items.values():
                value = changes.get(item_label(item))
                if value is not None and item.selected != value:
                    buckets[value].append(item.url)
            for value, urls in buckets.items():
                if not urls:
                    continue
                manager.set_selected(urls, value)
                for url in urls:
                    self._queue_row(manager.items[url])
            self._flush_rows()               # üres sorlistánál magától visszatér

        def _rebuild_panel_from_items(self) -> None:
            """A kiterjesztés-panel feltöltése a már ismert elemekből.

            Korábbi állapot betöltése után nincs átvizsgálás, a panel mégse
            maradjon üres: a címkék az elemekből is kiolvashatók.
            """
            if self.manager is None:
                return
            groups: dict[str, list[str]] = {}
            checked: set[str] = set()
            for item in self.manager.items.values():
                name = item_label(item)
                groups.setdefault(name, []).append(item.url)
                if item.selected:
                    checked.add(name)
            ordered = dict(sorted(groups.items(),
                                  key=lambda kv: (kv[0] == NO_EXT_LABEL, kv[0])))
            self._rebuild_extension_panel(ordered, checked)

        def _on_extension_toggled(self, name: str) -> None:
            """Egy kiterjesztés ki/be pipálása az összes hozzá tartozó fájlra hat."""
            self._sync_ext_text()
            self._apply_ext_selection({name: self._ext_vars[name].get()})

        def _set_all_extensions(self, value: bool) -> None:
            if not self._ext_vars:
                self._write_log("Előbb vizsgáld át az oldalt.")
                return
            self._ext_sync = True                # egyszerre állítunk mindent, utána írjuk a mezőt
            try:
                for var in self._ext_vars.values():
                    var.set(value)
            finally:
                self._ext_sync = False
            self._sync_ext_text()
            self._apply_ext_selection(dict.fromkeys(self._ext_vars, value))

        # -- a panel és a Kiterjesztések mező összehangolása ---------------
        def _sync_ext_text(self) -> None:
            """Panel -> mező: a kipipált címkék úgy jelennek meg, mintha beírták volna."""
            if self._ext_sync or not self._ext_vars:
                return
            checked = [name for name, var in self._ext_vars.items() if var.get()]
            self._ext_sync = True
            try:
                self.v_ext.set(ext_filter_text(checked))
                html_var = self._ext_vars.get(HTML_LABEL)
                if html_var is not None:
                    self.v_html.set(html_var.get())
            finally:
                self._ext_sync = False

        def _on_ext_filter_changed(self, *_args: object) -> None:
            """Gépelés közben ne fusson minden leütésre; kis késleltetéssel igazítunk."""
            if self._ext_sync or self._closing:
                return
            if self._ext_filter_job is not None:
                with suppress(tk.TclError):
                    self.after_cancel(self._ext_filter_job)
            self._ext_filter_job = self.after(EXT_SYNC_MS, self._apply_ext_filter)

        def _apply_ext_filter(self) -> None:
            """Mező -> panel: a beírt kiterjesztések és a HTML kapcsoló érvényesítése."""
            self._ext_filter_job = None
            if not self._ext_vars:
                return
            try:
                want_html = self.v_html.get()
            except tk.TclError:                  # félkész érték a változóban
                return
            chosen = choose_labels(self._ext_vars.keys(),
                                   parse_ext_filter(self.v_ext.get()), want_html)
            changes: dict[str, bool] = {}
            self._ext_sync = True                # a pipák állítása ne írja vissza a mezőt
            try:
                for name, var in self._ext_vars.items():
                    if not text_representable(name):     # a mező nem tud róla, ne is nyúljon hozzá
                        continue
                    if var.get() != (name in chosen):
                        var.set(name in chosen)
                        changes[name] = name in chosen
            finally:
                self._ext_sync = False
            self._apply_ext_selection(changes)

        def _on_scanned(self, result: ScanResult) -> None:
            self.scanning = False
            self.b_scan.configure(state="normal")
            groups = result.by_extension()
            if self.manager is None:
                return
            wanted = parse_ext_filter(self.v_ext.get())
            checked = matching_extensions(result, wanted, self.v_html.get())
            labels = {url: name for name, urls in groups.items() for url in urls}
            # Minden találat bekerül a listába; a pipa dönti el, mi töltődik le.
            # A már ismert elemek kijelölését is frissítjük, hogy egy újabb
            # átvizsgálás (pl. bekapcsolt HTML mellett) érvényre jusson.
            for name, urls in groups.items():
                self.manager.add_urls(urls, labels=labels, selected=name in checked)
                self.manager.set_selected(urls, name in checked)
            for item in self.manager.items.values():
                self._queue_row(item)
            self._flush_rows()
            self._rebuild_extension_panel(groups, checked)
            total = sum(len(u) for u in groups.values())
            self._write_log(f"Átvizsgálás kész: {total} fájl, {len(groups)} kiterjesztés "
                            f"({', '.join(groups)}).")
            self.v_status.set(f"{total} fájl található - a meglévők ellenőrzése…")
            self._update_count()
            self._start_verification()

        # -- meglévő fájlok automatikus szűrése ---------------------------
        def _start_verification(self) -> None:
            """Háttérben ellenőrzi, mi van már meg épen, és leveszi róla a pipát."""
            manager = self.manager
            if manager is None:
                return
            items = list(manager.items.values())

            def work() -> None:
                intact: list[str] = []
                broken: list[str] = []

                def record(item: Item, ok: bool) -> None:
                    (intact if ok else broken).append(item.url)

                try:
                    manager.classify_existing(items, record)
                except Exception as exc:                       # hálózati hiba stb.
                    self.events.put(("log", f"Az ellenőrzés félbeszakadt: {_brief(exc)}"))
                self.events.put(("verified", (intact, broken)))

            threading.Thread(target=work, daemon=True, name="verify").start()

        def _on_verified(self, payload: tuple[list[str], list[str]]) -> None:
            """Ép fájl -> pipa le; hiányzó vagy sérült -> pipa fel."""
            manager = self.manager
            if manager is None:
                return
            intact, broken = payload
            for url in intact:
                item = manager.items.get(url)
                if item is not None:
                    manager.mark_intact(item)
            wanted = self._checked_labels()      # egyszer, ne körönként újra
            for url in broken:
                item = manager.items.get(url)
                if item is not None and item_label(item) in wanted:
                    item.selected = True        # sérült: újra kell tölteni
                    item.status = Status.PENDING
                    item.done = 0
            manager._recount()                  # az összesítő a pipákhoz igazodik
            for url in (*intact, *broken):      # csak az érintett sorok rajzolódnak újra
                item = manager.items.get(url)
                if item is not None:
                    self._queue_row(item)
            self._flush_rows()
            waiting = len(manager.pending())
            if intact or broken:
                self._write_log(f"Ellenőrzés kész: {len(intact)} fájl már megvan épen "
                                f"(pipa levéve), {len(broken)} sérült vagy hiányos.")
            self.v_status.set(f"{waiting} fájl letöltésre kijelölve.")
            self._update_count()

        def _checked_labels(self) -> set[str]:
            return {name for name, var in self._ext_vars.items() if var.get()}

        def _on_finished(self) -> None:
            self.b_start.configure(state="normal")
            self.b_pause.configure(state="disabled", text="Szünet")
            self.b_stop.configure(state="disabled")
            self.v_status.set("Befejezve vagy leállítva - a félkész fájlok folytathatók.")

        def _refresh_status(self) -> None:
            manager = self.manager
            if manager is None:
                return
            totals = manager.totals
            ratio = (totals.bytes_done / totals.bytes_total) if totals.bytes_total else 0
            self.progress["value"] = min(1000, ratio * 1000)
            if manager.running and not manager.paused:
                speed = manager.speed()
                self.v_status.set(
                    f"{totals.done_files}/{totals.files} kész · "
                    f"{human(totals.bytes_done)} / {human(totals.bytes_total)} · "
                    f"{human(speed)}/s · hátralévő idő: {human_time(manager.eta())}")

        def _on_close(self) -> None:
            self._closing = True
            for job in (self._pump_job, self._autoload_job,   # függő időzítők leállítása
                        self._ext_filter_job):
                if job is not None:
                    with suppress(tk.TclError):
                        self.after_cancel(job)
            self._pump_job = self._autoload_job = self._ext_filter_job = None
            self._save_settings()
            self.scan_stop.set()
            if self.manager is not None:
                self.manager.close()
            self.destroy()

    enable_dpi_awareness()
    App().mainloop()
    return 0


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
                         same_host=not args.any_host, respect_robots=not args.ignore_robots)
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
    parser.add_argument("--meglevo", choices=[str(e) for e in Existing],
                        default=str(Existing.VERIFY),
                        help="mi történjék a már meglévő fájlokkal")
    parser.add_argument("--no-gui", action="store_true", help="parancssori futtatás")
    parser.add_argument("-V", "--version", action="version", version=f"letolto {__version__}")
    args = parser.parse_args(argv)
    if args.no_gui or args.url:
        return run_cli(args)
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
