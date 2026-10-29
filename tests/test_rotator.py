"""Regression tests for rotator.py.

Run with:  python -m unittest discover -s tests -v

The wallpaper API is stubbed and all state/log paths are redirected to a
temporary directory, so these tests never touch the real desktop.
"""

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rotator


class RotatorTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rotator_test_"))
        self.wallpapers = self.root / "wallpapers"
        self.wallpapers.mkdir()
        for name in ("a.jpg", "b.jpg", "c.png", "d.bmp", "e.webp"):
            (self.wallpapers / name).write_bytes(b"")

        self.app_dir = self.root / "app"
        self._saved = {
            "WALLPAPER_DIR": rotator.WALLPAPER_DIR,
            "APP_DIR": rotator.APP_DIR,
            "STATE_PATH": rotator.STATE_PATH,
            "LOG_PATH": rotator.LOG_PATH,
            "set_wallpaper": rotator.set_wallpaper,
        }
        rotator.WALLPAPER_DIR = self.wallpapers
        rotator.APP_DIR = self.app_dir
        rotator.STATE_PATH = self.app_dir / "state.json"
        rotator.LOG_PATH = self.app_dir / "rotator.log"

        self.applied = []
        rotator.set_wallpaper = self._record

    def tearDown(self):
        for handler in list(rotator.log.handlers):
            handler.close()
            rotator.log.removeHandler(handler)
        rotator.WALLPAPER_DIR = self._saved["WALLPAPER_DIR"]
        rotator.APP_DIR = self._saved["APP_DIR"]
        rotator.STATE_PATH = self._saved["STATE_PATH"]
        rotator.LOG_PATH = self._saved["LOG_PATH"]
        rotator.set_wallpaper = self._saved["set_wallpaper"]
        shutil.rmtree(self.root, ignore_errors=True)

    def _record(self, path):
        self.applied.append(Path(path).name)
        return "spi"

    def _run(self, args):
        with redirect_stdout(io.StringIO()):
            return rotator.main(args)

    def _fail(self, path):
        raise OSError(5, "simulated failure")

    def _state(self):
        return json.loads(rotator.STATE_PATH.read_text(encoding="utf-8"))

    def _seed(self, **overrides):
        state = {"version": 1, "cycle": 1, "last": None, "queue": []}
        state.update(overrides)
        rotator.save_state(state)

    def test_cycle_contains_each_image_exactly_once(self):
        for _ in range(5):
            self.assertEqual(self._run(["--next"]), 0)

        self.assertEqual(len(self.applied), 5)
        self.assertEqual(len(set(self.applied)), 5)
        self.assertEqual(set(self.applied), {p.name for p in self.wallpapers.iterdir()})

        self.assertEqual(self._run(["--next"]), 0)
        self.assertNotEqual(self.applied[5], self.applied[4])
        self.assertEqual(self._state()["cycle"], 2)

    def test_failed_apply_does_not_commit(self):
        self._seed(cycle=3, last="a.jpg", queue=["b.jpg", "c.png"])
        before = rotator.STATE_PATH.read_bytes()
        rotator.set_wallpaper = self._fail

        self.assertEqual(self._run(["--next"]), 1)
        self.assertEqual(rotator.STATE_PATH.read_bytes(), before)

    def test_corrupt_state_is_rebuilt(self):
        rotator.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        rotator.STATE_PATH.write_text("{ not json", encoding="utf-8")

        self.assertEqual(self._run(["--next"]), 0)
        self.assertEqual(len(self.applied), 1)
        state = self._state()
        self.assertEqual(state["cycle"], 1)
        self.assertEqual(len(state["queue"]), 4)

    def test_stale_queue_entries_are_dropped(self):
        self._seed(cycle=2, last=None, queue=["deleted.jpg", "a.jpg", "b.jpg"])

        self.assertEqual(self._run(["--next"]), 0)
        self.assertEqual(self.applied, ["a.jpg"])
        self.assertEqual(self._state()["queue"], ["b.jpg"])

    def test_missing_folder_exits_non_zero(self):
        rotator.WALLPAPER_DIR = self.root / "does-not-exist"
        self.assertEqual(self._run(["--next"]), 1)
        self.assertEqual(self.applied, [])

    def test_empty_folder_exits_non_zero(self):
        empty = self.root / "empty"
        empty.mkdir()
        rotator.WALLPAPER_DIR = empty
        self.assertEqual(self._run(["--next"]), 1)
        self.assertEqual(self.applied, [])

    def test_single_image_rotates_every_run(self):
        single = self.root / "single"
        single.mkdir()
        (single / "only.jpg").write_bytes(b"")
        rotator.WALLPAPER_DIR = single

        self.assertEqual(self._run(["--next"]), 0)
        self.assertEqual(self._run(["--next"]), 0)
        self.assertEqual(self.applied, ["only.jpg", "only.jpg"])

    def test_dry_run_does_not_mutate_state(self):
        self._seed(cycle=4, last="a.jpg", queue=["b.jpg", "c.png"])
        before = rotator.STATE_PATH.read_bytes()

        self.assertEqual(self._run(["--dry-run", "12"]), 0)
        self.assertEqual(rotator.STATE_PATH.read_bytes(), before)
        self.assertEqual(self.applied, [])

    def test_second_instance_is_a_no_op(self):
        self._seed(cycle=1, last=None, queue=["a.jpg", "b.jpg"])
        handle, already = rotator.acquire_single_instance()
        self.assertFalse(already)
        try:
            self.assertEqual(self._run(["--next"]), 0)
        finally:
            rotator.release_single_instance(handle)
        self.assertEqual(self.applied, [])

    def test_mutex_is_released_after_run(self):
        self.assertEqual(self._run(["--next"]), 0)
        handle, already = rotator.acquire_single_instance()
        try:
            self.assertFalse(already)
        finally:
            rotator.release_single_instance(handle)

    def test_state_write_is_atomic(self):
        self._seed(cycle=1, last=None, queue=["a.jpg"])
        self._run(["--next"])
        self.assertFalse(rotator.STATE_PATH.with_name("state.json.tmp").exists())

    def test_status_exits_zero(self):
        self.assertEqual(self._run(["--status"]), 0)


if __name__ == "__main__":
    unittest.main()
