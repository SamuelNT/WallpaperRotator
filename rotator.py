#!/usr/bin/env python3
"""Rotate the Windows desktop wallpaper, one image per invocation.

Run-once by design: Task Scheduler starts this script via pythonw.exe and it
performs a single rotation step, then exits. All design rationale, configuration
details and operating procedures live in README.md.

Public entry point: main(argv) -> int.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import logging
import logging.handlers
import os
import random
import sys
import time
import winreg
from pathlib import Path

WALLPAPER_DIR = Path(r"C:\Wallpapers")

# Parsed by install-task.ps1 to build the scheduled-task trigger.
ROTATION_INTERVAL_MINUTES = 60

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})

APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "WallpaperRotator"
STATE_PATH = APP_DIR / "state.json"
LOG_PATH = APP_DIR / "rotator.log"

LOG_MAX_BYTES = 256 * 1024
LOG_BACKUP_COUNT = 2

MUTEX_NAME = "Local\\WallpaperRotator.SingleInstance"

STATE_VERSION = 1

SPI_SETDESKWALLPAPER = 20
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02

# "10" is Fill; "22" (Span) would stretch one image across all monitors.
WALLPAPER_STYLE_FILL = "10"
TILE_WALLPAPER_NO = "0"

ERROR_ALREADY_EXISTS = 183

CLSCTX_ALL = 0x17
COINIT_APARTMENTTHREADED = 0x2

log = logging.getLogger("wallpaperrotator")


def configure_logging() -> None:
    """Log to a rotating file; the scheduled task has no console."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    log.setLevel(logging.INFO)
    for existing in log.handlers:
        existing.close()
    log.handlers.clear()
    log.addHandler(handler)
    log.propagate = False


def say(message: str = "") -> None:
    """Write to stdout when one exists (interactive CLI commands only)."""
    if sys.stdout is None:
        return
    try:
        print(message, flush=True)
    except (OSError, ValueError):
        pass


def scan_folder(folder: Path | None = None) -> list[Path]:
    """Return sorted absolute paths of supported images, non-recursively."""
    folder = folder or WALLPAPER_DIR
    if not folder.is_dir():
        log.error("Wallpaper folder does not exist: %s", folder)
        return []

    images: list[Path] = []
    try:
        entries = list(folder.iterdir())
    except OSError as exc:
        log.error("Cannot list %s: %s", folder, exc)
        return []

    for entry in entries:
        try:
            if entry.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            if not entry.is_file():
                continue
            images.append(entry.resolve())
        except OSError as exc:
            log.warning("Skipping %s: %s", entry, exc)

    images.sort(key=lambda p: p.name.lower())
    return images


def _empty_state() -> dict:
    return {"version": STATE_VERSION, "cycle": 0, "last": None, "queue": [], "updated_at": None}


def _basename(value: str) -> str:
    """Reduce a stored value to a file name (state never stores absolute paths)."""
    try:
        return Path(value).name
    except (TypeError, ValueError):
        return ""


def load_state(path: Path | None = None) -> tuple[dict, bool]:
    """Load state, returning (state, was_rebuilt).

    Missing state is normal on first run; unreadable or malformed state is
    logged and replaced with an empty one so rotation can continue.
    """
    path = path or STATE_PATH
    if not path.exists():
        return _empty_state(), False

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("State file unreadable (%s); rebuilding from scratch. File: %s", exc, path)
        return _empty_state(), True

    if not isinstance(raw, dict):
        log.warning("State file has wrong top-level type %s; rebuilding.", type(raw).__name__)
        return _empty_state(), True

    state = _empty_state()
    try:
        state["cycle"] = int(raw.get("cycle") or 0)
    except (TypeError, ValueError):
        state["cycle"] = 0

    state["last"] = _basename(raw.get("last") or "") or None

    queue = raw.get("queue")
    if isinstance(queue, list):
        names = [_basename(item) for item in queue if isinstance(item, str)]
        state["queue"] = [name for name in names if name]
    else:
        if queue is not None:
            log.warning("State 'queue' is not a list; ignoring it.")
        state["queue"] = []

    return state, False


def save_state(state: dict, path: Path | None = None) -> None:
    """Write state atomically via a temporary file and os.replace()."""
    path = path or STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    state = dict(state, version=STATE_VERSION, updated_at=time.strftime("%Y-%m-%dT%H:%M:%S"))

    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def pick_next(files: list[Path], state: dict, rng: random.Random) -> tuple[Path, list[str], int, bool]:
    """Select the next image.

    Returns (path, remaining_queue, cycle, reshuffled). Missing queue entries
    are dropped; an empty queue starts a new cycle whose first image differs
    from the previously shown one when possible.
    """
    by_key = {p.name.lower(): p for p in files}

    queue: list[str] = []
    seen: set[str] = set()
    for name in state.get("queue", []):
        key = name.lower()
        if key in by_key and key not in seen:
            queue.append(by_key[key].name)
            seen.add(key)

    dropped = len(state.get("queue", [])) - len(queue)
    if dropped > 0:
        log.info("Dropped %d queued image(s) that no longer exist on disk.", dropped)

    reshuffled = False
    cycle = int(state.get("cycle") or 0)

    if not queue:
        names = [p.name for p in files]
        rng.shuffle(names)
        last = state.get("last")
        if len(names) > 1 and last and names[0].lower() == str(last).lower():
            swap_at = rng.randrange(1, len(names))
            names[0], names[swap_at] = names[swap_at], names[0]
        queue = names
        cycle += 1
        reshuffled = True

    chosen_name = queue[0]
    return by_key[chosen_name.lower()], queue[1:], cycle, reshuffled


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SystemParametersInfoW.argtypes = [
    wintypes.UINT,
    wintypes.UINT,
    wintypes.LPWSTR,
    wintypes.UINT,
]
_user32.SystemParametersInfoW.restype = wintypes.BOOL


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


CLSID_DesktopWallpaper = _GUID(
    0xC2CF3110, 0x460E, 0x4FC1,
    (ctypes.c_ubyte * 8)(0xB9, 0xD0, 0x8A, 0x1C, 0x0C, 0x9C, 0xC4, 0xBD),
)
IID_IDesktopWallpaper = _GUID(
    0xB92B56A9, 0x8B55, 0x4E14,
    (ctypes.c_ubyte * 8)(0x9A, 0x89, 0x01, 0x99, 0xBB, 0xB6, 0xF9, 0x3B),
)


class _DesktopWallpaper:
    """ctypes binding for IDesktopWallpaper (Windows 8+).

    Methods are addressed by vtable slot, after the three IUnknown slots:
    3 SetWallpaper, 4 GetWallpaper, 5 GetMonitorDevicePathAt,
    6 GetMonitorDevicePathCount.
    """

    def __init__(self) -> None:
        self._ptr = ctypes.c_void_p()
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)

        hr_init = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        self._needs_uninit = hr_init in (0, 1)

        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_DesktopWallpaper),
            None,
            CLSCTX_ALL,
            ctypes.byref(IID_IDesktopWallpaper),
            ctypes.byref(self._ptr),
        )
        if hr < 0 or not self._ptr:
            if self._needs_uninit:
                ole32.CoUninitialize()
            raise OSError(f"CoCreateInstance(IDesktopWallpaper) failed: 0x{hr & 0xFFFFFFFF:08X}")

        self._vtbl = ctypes.cast(
            self._ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        self._ole32 = ole32
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None

    def _call(self, index: int, argtypes: list, *args) -> int:
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
        func = proto(self._vtbl[index])
        return func(self._ptr, *args)

    def _free(self, ptr) -> None:
        self._ole32.CoTaskMemFree(ctypes.cast(ptr, ctypes.c_void_p))

    def _take_string(self, ptr: ctypes.c_wchar_p) -> str:
        value = ptr.value or ""
        self._free(ptr)
        return value

    def set_all(self, wallpaper: str) -> None:
        """Set the wallpaper for every monitor (NULL monitor id)."""
        hr = self._call(3, [wintypes.LPCWSTR, wintypes.LPCWSTR], None, wallpaper)
        if hr < 0:
            raise OSError(f"IDesktopWallpaper::SetWallpaper failed: 0x{hr & 0xFFFFFFFF:08X}")

    def monitor_wallpapers(self) -> list[str]:
        """Return the wallpaper path currently reported for each monitor."""
        count = wintypes.UINT(0)
        hr = self._call(6, [ctypes.POINTER(wintypes.UINT)], ctypes.byref(count))
        if hr < 0:
            raise OSError(f"GetMonitorDevicePathCount failed: 0x{hr & 0xFFFFFFFF:08X}")

        result: list[str] = []
        for index in range(count.value):
            monitor_id = ctypes.c_wchar_p()
            hr = self._call(5, [wintypes.UINT, ctypes.POINTER(ctypes.c_wchar_p)],
                            index, ctypes.byref(monitor_id))
            if hr < 0:
                continue
            path = ctypes.c_wchar_p()
            hr = self._call(4, [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_wchar_p)],
                            monitor_id.value, ctypes.byref(path))
            if hr >= 0:
                result.append(self._take_string(path))
            self._free(monitor_id)
        return result

    def close(self) -> None:
        if self._ptr:
            try:
                release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(self._vtbl[2])
                release(self._ptr)
            except Exception:
                pass
            self._ptr = ctypes.c_void_p()
        if self._needs_uninit:
            try:
                self._ole32.CoUninitialize()
            except Exception:
                pass

    def __enter__(self) -> "_DesktopWallpaper":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _apply_registry_style() -> None:
    """Set Fill + no tiling before applying the image."""
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, "WallpaperStyle", 0, winreg.REG_SZ, WALLPAPER_STYLE_FILL)
        winreg.SetValueEx(key, "TileWallpaper", 0, winreg.REG_SZ, TILE_WALLPAPER_NO)


def _verify_monitors(target: Path, attempts: int = 6) -> tuple[bool, list[str]]:
    """Check what each monitor reports; retries briefly after the set.

    Returns (all_match, paths). When the COM query is unavailable the result is
    (True, []) so verification cannot block an otherwise successful set.
    """
    wanted = os.path.normcase(str(target))
    try:
        with _DesktopWallpaper() as desktop:
            for attempt in range(attempts):
                current = [os.path.normcase(p) for p in desktop.monitor_wallpapers() if p]
                if current and all(p == wanted for p in current):
                    return True, current
                if attempt + 1 < attempts:
                    time.sleep(0.05)
            return (not current), current
    except Exception as exc:
        log.debug("Per-monitor verification unavailable: %s", exc)
        return True, []


def set_wallpaper(image_path: Path) -> str:
    """Apply one image to all monitors; returns "spi" or "com".

    Falls back to IDesktopWallpaper when SPI succeeds but the monitors report
    different images. Raises on failure so the caller can retry later.
    """
    path = Path(image_path).resolve(strict=True)
    if not path.is_file():
        raise OSError(f"Not a file: {path}")

    _apply_registry_style()

    ok = _user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER,
        0,
        str(path),
        SPIF_UPDATEINIFILE | SPIF_SENDCHANGE,
    )
    if not ok:
        raise OSError(
            ctypes.get_last_error(),
            f"SystemParametersInfoW(SPI_SETDESKWALLPAPER) failed for {path.name}",
        )

    matched, current = _verify_monitors(path)
    if matched:
        return "spi"

    log.warning(
        "SPI reported success but monitors disagree (%s); retrying via IDesktopWallpaper.",
        ", ".join(os.path.basename(p) or "<empty>" for p in current) or "unknown",
    )
    with _DesktopWallpaper() as desktop:
        desktop.set_all(str(path))

    matched, current = _verify_monitors(path)
    if not matched:
        raise RuntimeError(
            "Wallpaper set via COM but monitors still disagree: "
            + (", ".join(current) or "no monitors reported")
        )
    return "com"


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


def acquire_single_instance() -> tuple[int, bool]:
    """Create the named mutex; returns (handle, already_running)."""
    handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    already = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    return handle, already


def release_single_instance(handle: int) -> None:
    """Close the mutex handle."""
    if handle:
        _kernel32.CloseHandle(handle)


def cmd_status(files: list[Path], state: dict) -> int:
    queue = state.get("queue", [])
    say(f"Wallpaper folder : {WALLPAPER_DIR}")
    say(f"Images found     : {len(files)}")
    say(f"Queue remaining  : {len(queue)}")
    say(f"Cycle            : {state.get('cycle', 0)}")
    say(f"Last image       : {state.get('last') or '<none>'}")
    if queue:
        say(f"Next image       : {queue[0]}")
    say(f"State file       : {STATE_PATH}")
    say(f"Log file         : {LOG_PATH}")
    log.info("Status: %d images, %d queued, last=%s", len(files), len(queue), state.get("last"))
    return 0


def cmd_dry_run(files: list[Path], state: dict, count: int) -> int:
    """Print the next `count` picks without changing wallpaper or state.

    Uses a fixed seed so a cycle-boundary check is repeatable.
    """
    if not files:
        say("No images in folder.")
        return 1

    rng = random.Random(0xC0FFEE)
    sim_state = {
        "queue": list(state.get("queue", [])),
        "last": state.get("last"),
        "cycle": int(state.get("cycle") or 0),
    }

    seen_by_cycle: dict[int, set[str]] = {}
    problems: list[str] = []
    say(f"Dry run: next {count} pick(s), no changes will be made.")
    for step in range(1, count + 1):
        previous = sim_state["last"]
        chosen, queue, cycle, reshuffled = pick_next(files, sim_state, rng)
        if reshuffled:
            say(f"  --- cycle {cycle} starts ---")
            if len(files) > 1 and previous and chosen.name == previous:
                problems.append(f"cycle {cycle} repeats last image {chosen.name}")
                say(f"{step:>3}. {chosen.name}   <-- REPEAT ACROSS BOUNDARY")
                sim_state.update(queue=queue, last=chosen.name, cycle=cycle)
                continue

        names = seen_by_cycle.setdefault(cycle, set())
        if chosen.name.lower() in names:
            problems.append(f"cycle {cycle} repeats {chosen.name}")
            say(f"{step:>3}. {chosen.name}   <-- REPEAT WITHIN CYCLE")
        else:
            names.add(chosen.name.lower())
            say(f"{step:>3}. {chosen.name}")

        sim_state.update(queue=queue, last=chosen.name, cycle=cycle)

    if problems:
        say("FAILED: " + "; ".join(problems))
        return 1
    say(f"OK: {len(files)} image(s) per cycle, {len(seen_by_cycle)} cycle(s) simulated, no repeats.")
    return 0


def cmd_reset() -> int:
    """Delete state and rebuild a freshly shuffled queue."""
    try:
        STATE_PATH.unlink()
        say(f"Deleted state file: {STATE_PATH}")
    except FileNotFoundError:
        say(f"No state file to delete: {STATE_PATH}")
    except OSError as exc:
        log.error("Could not delete state file: %s", exc)
        say(f"ERROR: could not delete {STATE_PATH}: {exc}")
        return 1

    files = scan_folder()
    if not files:
        say(f"No images in {WALLPAPER_DIR}; state will be built on the first run with images.")
        log.warning("Reset requested but folder is empty.")
        return 1

    fresh = _empty_state()
    chosen, queue, cycle, _ = pick_next(files, fresh, random.Random())
    save_state({"version": STATE_VERSION, "cycle": cycle, "last": None, "queue": [chosen.name, *queue]})
    say(f"Rebuilt queue: {len(files)} image(s), cycle {cycle}.")
    say("Next run will pick from the new shuffled order.")
    log.info("State reset and rebuilt with %d images.", len(files))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rotator.py",
        description="Set the desktop wallpaper to the next image in a shuffle bag.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--next", action="store_true", help="rotate once now (default)")
    group.add_argument("--dry-run", type=int, metavar="N", help="show the next N picks without changing anything")
    group.add_argument("--status", action="store_true", help="show queue length, last image and paths")
    group.add_argument("--reset", action="store_true", help="delete state and rebuild the queue")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging()

    files = scan_folder()

    if args.status:
        state, _ = load_state()
        return cmd_status(files, state)

    if args.reset:
        return cmd_reset()

    state, rebuilt = load_state()
    if rebuilt:
        log.warning("Rebuilt empty state; a new shuffled cycle will be created.")

    if not files:
        log.error("No images found in %s (looked for %s). Nothing to do.",
                  WALLPAPER_DIR, ", ".join(sorted(IMAGE_EXTENSIONS)))
        return 1

    if args.dry_run is not None:
        return cmd_dry_run(files, state, max(0, args.dry_run))

    handle, already_running = acquire_single_instance()
    if already_running:
        log.info("Another instance is running; exiting without changes.")
        release_single_instance(handle)
        return 0

    try:
        return rotate_once(files, state)
    finally:
        release_single_instance(handle)


def rotate_once(files: list[Path], state: dict) -> int:
    """Pick, apply, then commit state; a failed apply leaves the queue intact."""
    rng = random.Random()
    try:
        image, remaining, cycle, reshuffled = pick_next(files, state, rng)
    except Exception as exc:
        log.exception("Selection failed: %s", exc)
        return 1

    if reshuffled:
        log.info("Starting new shuffle cycle %d with %d image(s).", cycle, len(files))

    try:
        method = set_wallpaper(image)
    except Exception as exc:
        log.error("Failed to set wallpaper %s: %s", image.name, exc)
        return 1

    try:
        save_state({
            "version": STATE_VERSION,
            "cycle": cycle,
            "last": image.name,
            "queue": remaining,
        })
    except OSError as exc:
        log.error("Wallpaper changed but state could not be saved (%s); it may repeat.", exc)
        return 1

    log.info(
        "Set wallpaper: %s | %d left in queue | cycle %d | via %s",
        image.name, len(remaining), cycle, method.upper(),
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.exception("Unhandled error")
        sys.exit(1)
