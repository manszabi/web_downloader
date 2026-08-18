#!/usr/bin/env python3
"""A letöltő grafikus felülete (tkinter).

Miért külön modul? A mag (``letolto.py``) így nem függ a tkintertől: a
parancssori mód olyan gépen is elindul, ahol nincs telepítve, és a felület
kódja önmagában is átlátható marad. A mag nevei ``core.`` előtaggal érhetők
el, mert a beállításokat és az osztályokat futás közben ki lehet cserélni
(a tesztek élnek is ezzel), és ilyenkor a felületnek a mindenkori értéket
kell látnia, nem az importáláskor lemásoltat.
"""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from collections.abc import Iterable
from contextlib import suppress
from functools import partial
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import letolto as core

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
        self.title(f"Weboldal-letöltő {core.__version__}")
        self.scale = self._display_scale()
        self.tk.call("tk", "scaling", self.scale * 96 / 72)   # betűk a DPI-hez
        self.geometry(f"{int(1040 * self.scale)}x{int(740 * self.scale)}")
        self.minsize(int(900 * self.scale), int(620 * self.scale))
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.manager: core.DownloadManager | None = None
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
        self.v_robots.trace_add("write", self._on_robots_changed)
        self._on_robots_changed()
        self._pump_job = self.after(core.UI_TICK_MS, self._pump)
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
            saved = json.loads(core.SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for name, var in (("url", self.v_url), ("dir", self.v_dir), ("ext", self.v_ext),
                          ("depth", self.v_depth), ("threads", self.v_threads),
                          ("same_host", self.v_same), ("robots", self.v_robots),
                          ("robots5xx", self.v_robots5xx),
                          ("existing", self.v_existing), ("html", self.v_html)):
            if name in saved:
                with suppress(tk.TclError, ValueError):
                    var.set(saved[name])

    def _save_settings(self) -> None:
        data = {"url": self.v_url.get(), "dir": self.v_dir.get(), "ext": self.v_ext.get(),
                "depth": self.v_depth.get(), "threads": self.v_threads.get(),
                "same_host": self.v_same.get(), "robots": self.v_robots.get(),
                "robots5xx": self.v_robots5xx.get(),
                "existing": self.v_existing.get(), "html": self.v_html.get()}
        # Ugyanaz a minta, mint az állapotfájlnál: előbb ideiglenes fájlba írunk,
        # és csak a kész tartalom lép a régi helyére. Így áramszünet vagy
        # összeomlás esetén sem marad csonka, olvashatatlan beállításfájl.
        with suppress(OSError, TypeError):
            core.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = core.SETTINGS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            core.atomic_replace(tmp, core.SETTINGS_FILE)

    def _on_robots_changed(self, *_args: object) -> None:
        """A robots.txt kikapcsolásakor az 5xx-kapcsoló értelmét veszti.

        Ilyenkor a program a robots.txt-t le sem kéri, tehát nincs mit
        eldönteni; a szürke jelölőnégyzet ezt mutatja meg.
        """
        self.c_robots5xx.configure(state="normal" if self.v_robots.get() else "disabled")

    def _on_existing_changed(self, *_args: object) -> None:
        if self.manager is not None:
            self.manager.existing = self.v_existing.get()

    def _on_threads_changed(self, *_args: object) -> None:
        """A szálszám azonnal érvényesül, futó letöltés közben is."""
        try:
            count = self.v_threads.get()
        except tk.TclError:              # a mezőbe épp félkész érték került
            return
        if self.manager is not None and 1 <= count <= core.MAX_THREADS:
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
        if not (outdir / core.STATE_FILE).is_file():
            return
        if self.manager is not None and self.manager.outdir == outdir:
            return
        if self.manager is not None:
            self.manager.close()
        self.manager = core.DownloadManager(outdir, self.v_threads.get(), on_event=self._on_event)
        self.manager.load_state()
        unfinished = len(self.manager.pending())
        partial = sum(1 for i in self.manager.items.values()
                      if i.done and i.status != core.Status.DONE)
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
        ttk.Spinbox(box, from_=1, to=core.MAX_THREADS, width=5, textvariable=self.v_threads).grid(
            row=4, column=4, sticky="w")

        self.v_same = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="csak azonos domain", variable=self.v_same).grid(
            row=5, column=1, sticky="w", padx=4)
        self.v_robots = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="robots.txt betartása", variable=self.v_robots).grid(
            row=5, column=2, sticky="w")
        # Kipipálva: az elérhetetlen robots.txt leállítja az átvizsgálást (RFC 9309),
        # üresen hagyva a program a régi, megengedő módon folytatja.
        self.v_robots5xx = tk.BooleanVar(value=False)
        self.c_robots5xx = ttk.Checkbutton(box, text="5xx hibánál leáll",
                                           variable=self.v_robots5xx)
        self.c_robots5xx.grid(row=5, column=3, columnspan=2, sticky="w", padx=(12, 0))

        label("Meglévő fájl:", 6, 0)
        self.v_existing = tk.StringVar(value=str(core.Existing.VERIFY))
        ttk.Combobox(box, textvariable=self.v_existing, state="readonly", width=18,
                     values=[str(e) for e in core.Existing]).grid(row=6, column=1,
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
        core.note(message)                    # ugyanez a sor a naplófájlba is
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self._log_lines += 1
        if self._log_lines > core.LOG_MAX_LINES:      # a napló nem nőhet korlátlanul
            self.log.delete("1.0", f"{self._log_lines - core.LOG_MAX_LINES + 1}.0")
            self._log_lines = core.LOG_MAX_LINES
        self.log.see("end")
        self.log.configure(state="disabled")

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.v_dir.get() or str(Path.home()))
        if chosen:
            self.v_dir.set(chosen)

    def _open_dir(self) -> None:
        target = Path(self.v_dir.get())
        if target.is_dir():
            core.open_in_file_manager(target)
        else:
            messagebox.showinfo("Info", "A célkönyvtár még nem létezik.")

    def _open_settings_dir(self) -> None:
        """A beállításfájl mappájának megnyitása külön fájlkezelő ablakban."""
        try:
            core.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Hiba", f"A beállítások mappája nem érhető el:\n{exc}")
            return
        if not core.SETTINGS_FILE.is_file():
            self._save_settings()      # legyen mit megnézni az első indításkor is
        core.reveal_in_file_manager(core.SETTINGS_FILE)
        self._write_log(f"Beállítások helye: {core.SETTINGS_FILE}")
        self._write_log(f"Naplófájl: {core.LOG_FILE}")

    def _ensure_manager(self) -> core.DownloadManager | None:
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
            self.manager = core.DownloadManager(outdir, self.v_threads.get(),
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
        cfg = core.ScanConfig(root=url, depth=self.v_depth.get(),
                         same_host=self.v_same.get(),
                         respect_robots=self.v_robots.get(),
                         stop_on_robots_error=self.v_robots5xx.get())
        self.scanning = True
        self.scan_stop.clear()
        self.b_scan.configure(state="disabled")
        self.v_status.set("Átvizsgálás folyamatban…")

        def work() -> None:
            scanner = core.Scanner(cfg, manager.client,
                              lambda m: self.events.put(("log", m)), self.scan_stop)
            try:
                found = scanner.run()
            except Exception as exc:
                self.events.put(("log", f"Átvizsgálási hiba: {exc}"))
                found = core.ScanResult()
            self.events.put(("scanned", found))

        threading.Thread(target=work, daemon=True, name="scanner").start()

    def _load_state(self) -> None:
        manager = self._ensure_manager()
        if manager is None:
            return
        manager.load_state()
        self._write_log(f"{len(manager.items)} bejegyzés betöltve az állapotfájlból.")

    def _confirm_overwrites(self, manager: core.DownloadManager) -> bool:
        """Rákérdez a már meglévő, ép fájlok felülírására. Hamis = ne induljon a letöltés."""
        candidates = [i for i in manager.items.values()
                      if i.selected and i.status == core.Status.DONE and not i.force]
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
            for _ in range(core.UI_BATCH):           # egy körben korlátozott adag
                kind, payload = self.events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self._flush_rows()
        self._refresh_status()
        if not self._closing:            # bezárás után ne ütemezzünk újat
            self._pump_job = self.after(core.UI_TICK_MS, self._pump)

    def _handle_event(self, kind: str, payload: object) -> None:
        """Egy háttéresemény feldolgozása a GUI szálán."""
        match kind:
            case "log":
                self._write_log(str(payload))
            case "item":
                if isinstance(payload, core.Item):
                    self._queue_row(payload)
            case "added" | "reset":
                self._on_items_batch(payload if isinstance(payload, list) else [],
                                     reset=kind == "reset")
            case "scanned":
                if isinstance(payload, core.ScanResult):
                    self._on_scanned(payload)
            case "verified":
                if isinstance(payload, tuple):
                    self._on_verified(payload)
            case "finished":
                self._on_finished()

    def _on_items_batch(self, items: list[core.Item], *, reset: bool) -> None:
        """Elemek kötegelt megjelenítése: betöltés (reset) vagy új találatok (added)."""
        if reset:
            self.tree.delete(*self.tree.get_children())
            self._known_rows.clear()
        for item in items:
            self._queue_row(item)
        if reset and items:
            # Betöltött állapotnál is legyen mit pipálni a kiterjesztés-panelen.
            self._rebuild_panel_from_items()

    def _queue_row(self, item: core.Item) -> None:
        self._pending_rows[item.url] = (
            core.CHECKED_MARK if item.selected else core.UNCHECKED_MARK,
            item.path, core.human(item.total), core.human(item.done),
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
            ).grid(row=index // core.EXT_COLUMNS, column=index % core.EXT_COLUMNS,
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
            value = changes.get(core.item_label(item))
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
            name = core.item_label(item)
            groups.setdefault(name, []).append(item.url)
            if item.selected:
                checked.add(name)
        ordered = dict(sorted(groups.items(),
                              key=lambda kv: (kv[0] == core.NO_EXT_LABEL, kv[0])))
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
            self.v_ext.set(core.ext_filter_text(checked))
            html_var = self._ext_vars.get(core.HTML_LABEL)
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
        self._ext_filter_job = self.after(core.EXT_SYNC_MS, self._apply_ext_filter)

    def _apply_ext_filter(self) -> None:
        """Mező -> panel: a beírt kiterjesztések és a HTML kapcsoló érvényesítése."""
        self._ext_filter_job = None
        if not self._ext_vars:
            return
        try:
            want_html = self.v_html.get()
        except tk.TclError:                  # félkész érték a változóban
            return
        chosen = core.choose_labels(self._ext_vars.keys(),
                               core.parse_ext_filter(self.v_ext.get()), want_html)
        changes: dict[str, bool] = {}
        self._ext_sync = True                # a pipák állítása ne írja vissza a mezőt
        try:
            for name, var in self._ext_vars.items():
                if not core.text_representable(name):     # a mező nem tud róla, ne is nyúljon hozzá
                    continue
                if var.get() != (name in chosen):
                    var.set(name in chosen)
                    changes[name] = name in chosen
        finally:
            self._ext_sync = False
        self._apply_ext_selection(changes)

    def _on_scanned(self, result: core.ScanResult) -> None:
        self.scanning = False
        self.b_scan.configure(state="normal")
        groups = result.by_extension()
        if self.manager is None:
            return
        wanted = core.parse_ext_filter(self.v_ext.get())
        checked = core.matching_extensions(result, wanted, self.v_html.get())
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

            def record(item: core.Item, ok: bool) -> None:
                (intact if ok else broken).append(item.url)

            try:
                manager.classify_existing(items, record)
            except Exception as exc:                       # hálózati hiba stb.
                self.events.put(("log", f"Az ellenőrzés félbeszakadt: {core._brief(exc)}"))
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
            if item is not None and core.item_label(item) in wanted:
                item.selected = True        # sérült: újra kell tölteni
                item.status = core.Status.PENDING
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
                f"{core.human(totals.bytes_done)} / {core.human(totals.bytes_total)} · "
                f"{core.human(speed)}/s · hátralévő idő: {core.human_time(manager.eta())}")

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


def run_gui() -> int:                                   # pragma: no cover (GUI)
    """A grafikus felület elindítása. A hívó a kilépési kódot kapja vissza."""
    core.enable_dpi_awareness()
    App().mainloop()
    return 0
