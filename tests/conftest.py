"""A pytest indulasa: a repo gyokere kerul a sys.path-ra.

Igy a "pytest" barmelyik konyvtarbol elindithato, es a tesztek megtalaljak a
letolto.py-t es a letolto_gui.py-t telepites nelkul is.
"""
import sys
from pathlib import Path

GYOKER = Path(__file__).resolve().parent.parent
for ut in (str(GYOKER), str(GYOKER / "tests")):
    if ut not in sys.path:
        sys.path.insert(0, ut)
