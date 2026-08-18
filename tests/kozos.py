"""Kozos segedek a tesztekhez.

Egyelore a memoriamerest tartalmazza. A /proc csak Linuxon letezik, ezert a
Windows-os es macOS-es agat is megadjuk - igy ugyanaz a teszt fut mindenhol,
es ahol egyik sem elerheto, ott az ellenorzes kimarad, nem bukik el.
"""
from __future__ import annotations

import sys


def _linux(kulcs: str) -> float | None:
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for sor in f:
                if sor.startswith(kulcs):
                    return int(sor.split()[1]) / 1024        # kB -> MB
    except OSError:
        return None
    return None


def _windows(csucs: bool) -> float | None:
    """A folyamat munkakeszlete a psapi.GetProcessMemoryInfo alapjan."""
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD),
                        ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        adat = PMC()
        adat.cb = ctypes.sizeof(PMC)
        proc = ctypes.windll.kernel32.GetCurrentProcess()          # type: ignore[attr-defined]
        if not ctypes.windll.psapi.GetProcessMemoryInfo(           # type: ignore[attr-defined]
                proc, ctypes.byref(adat), adat.cb):
            return None
        bajt = adat.PeakWorkingSetSize if csucs else adat.WorkingSetSize
        return bajt / (1024 * 1024)
    except Exception:                     # nincs psapi, vagy mas a szerkezet
        return None


def _mac() -> float | None:
    try:
        import resource  # noqa: PLC0415
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)  # bajt
    except (ImportError, OSError):
        return None


def rss_mb() -> float | None:
    """A folyamat pillanatnyi memoriahasznalata MB-ban, vagy None."""
    if sys.platform == "win32":
        return _windows(csucs=False)
    return _linux("VmRSS") if sys.platform.startswith("linux") else _mac()


def csucs_mb() -> float | None:
    """A folyamat eddigi memoriacsucsa MB-ban, vagy None."""
    if sys.platform == "win32":
        return _windows(csucs=True)
    return _linux("VmHWM") if sys.platform.startswith("linux") else _mac()


def merheto() -> bool:
    """Van-e egyaltalan memoriamerés ezen a rendszeren?"""
    return rss_mb() is not None
