"""A tesztszkriptek futtatasa pytest alol.

A csomag tesztjei onallo szkriptek: sajat kiszolgalot inditanak, valodi
ablakot nyitnak, es a vegen kilepesi koddal jeleznek. Ez a fajl mindegyiket
kulon alfolyamatban futtatja, es a kilepesi kodot valtja assert-re. Igy egyben
megmarad a szkriptek kozvetlen futtathatosaga ("python tests/test_robots.py"),
es megjon a pytest kenyelme is:

    pytest                      # minden
    pytest -k robots            # csak a robots.txt tesztjei
    pytest -k "not gui"         # ablak nelkul
    pytest -x -q                # az elso hibanal all

A pytest csak ezt a fajlt gyujti be (lasd pyproject.toml, python_files).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

TESZTEK = Path(__file__).resolve().parent
IDOKORLAT = 900          # masodperc: a terhelesi teszt a leglassabb

# Ezek valodi ablakot nyitnak, tehat kepernyo (vagy Xvfb) kell hozzajuk.
ABLAKOS = {"test_gui.py", "test_gui_robots.py", "test_gui_szinkron.py",
           "test_gui_valogatas.py", "test_epseg.py", "test_szalak.py",
           "test_osszeomlas.py"}


def szkriptek() -> list[str]:
    return sorted(p.name for p in TESZTEK.glob("test_*.py"))


def van_kepernyo() -> bool:
    """Elindithato-e ablakos teszt ezen a gepen?"""
    try:
        import tkinter  # noqa: F401, PLC0415 - csak a meglete szamit
    except ImportError:
        return False
    return sys.platform in ("win32", "darwin") or bool(os.environ.get("DISPLAY"))


@pytest.mark.parametrize("szkript", szkriptek())
def test_szkript(szkript: str) -> None:
    """Egy tesztszkript lefuttatasa; a nem nulla kilepesi kod a hiba."""
    if szkript in ABLAKOS and not van_kepernyo():
        pytest.skip("ablakos teszt: nincs kepernyo (tkinter vagy DISPLAY hianyzik)")
    kesz = subprocess.run(
        [sys.executable, str(TESZTEK / szkript)],
        capture_output=True, timeout=IDOKORLAT, cwd=TESZTEK.parent, check=False,
        # A szkriptek ekezetes magyar szoveget irnak ki. A gyermek oldalan a
        # PYTHONIOENCODING allitja a kimenet kodolasat, itt, a szulo oldalan
        # pedig az encoding= - enelkul a Windows a cp1252-t hasznalna, es a
        # dekodolas mar az elso "ő" betun elszallna.
        text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if kesz.returncode != 0:
        hibak = [sor for sor in kesz.stdout.splitlines() if "[HIBA]" in sor]
        pytest.fail(f"{szkript} elbukott (kilepesi kod {kesz.returncode})\n"
                    + "\n".join(hibak or kesz.stdout.splitlines()[-25:])
                    + (f"\n--- stderr ---\n{kesz.stderr[-2000:]}" if kesz.stderr else ""))
