# WallpaperRotator

Rotates the Windows desktop wallpaper through a folder of images, one image per
scheduled invocation, applying the **same** image to **every** monitor.

| | |
| --- | --- |
| Version | 1.1.0 |
| Platform | Windows 11 (x64) |
| Runtime | Python 3.11+ (standard library only) |
| Status | Deployed and verified on this machine |

---

## AI disclosure

This repository was produced with AI assistance. The application code
(`rotator.py`, `tests/test_rotator.py`, `install-task.ps1`,
`uninstall-task.ps1`) and this documentation were written by an AI coding agent
running in OpenCode (model: DeepSeek V4.1 Flash Free), at the request and under
the direction of the repository owner.

The behaviour described in this document was verified on the target machine: the
regression suite passes, both monitors were confirmed to report the same
wallpaper after a rotation, and the registered task was inspected after
installation. That evidence applies to this environment; it is not a guarantee
for another one.

AI-generated code can be wrong. Review it before relying on it, particularly
before running it against different paths or on another Windows build. The
software is provided without warranty.

---

## Contents

1. [Overview](#1-overview)
2. [Requirements](#2-requirements)
3. [Repository layout](#3-repository-layout)
4. [Configuration](#4-configuration)
5. [Installation](#5-installation)
6. [CLI reference](#6-cli-reference)
7. [Operations runbook](#7-operations-runbook)
8. [Architecture and design decisions](#8-architecture-and-design-decisions)
9. [Testing](#9-testing)
10. [Troubleshooting](#10-troubleshooting)
11. [Maintenance](#11-maintenance)
12. [Known limitations](#12-known-limitations)
13. [Changelog](#13-changelog)

---

## 1. Overview

The built-in Windows wallpaper slideshow does not guarantee that two monitors
show the same image at the same time. This tool replaces it with a deterministic
rotation that changes both monitors together at a logon-anchored interval.

Each invocation of `rotator.py` performs exactly one step:

```
scan folder -> repair queue -> pick next image -> set wallpaper -> commit state
```

The process then exits. Windows Task Scheduler is responsible for running it
again later; there is no resident process, no polling loop and no GUI.

**Guarantees**

* Every image in the folder is shown exactly once per cycle.
* The same image is applied to all monitors in a single operation.
* A failed run never consumes an image from the queue.
* State survives reboots, logoffs and sleep.
* Nothing steals focus, opens a window or shows a notification.

**Out of scope**

* Per-monitor different images.
* Slideshow transitions or timing below the Task Scheduler minimum (1 minute).
* Image conversion or validation of file contents.

---

## 2. Requirements

| Requirement | Detail |
| --- | --- |
| OS | Windows 11 (uses `IDesktopWallpaper`, available since Windows 8) |
| Python | 3.11 or newer; verified with 3.13.15 |
| Interpreter | `pythonw.exe` (no console window) |
| Wallpaper folder | `C:\Wallpapers` by default, configurable in `rotator.py` |
| Permissions | Standard user. No elevation is required. |

The installer resolves `pythonw.exe` automatically. It rejects the 0-byte
Microsoft Store alias in `%LOCALAPPDATA%\Microsoft\WindowsApps`, which is not a
real interpreter.

---

## 3. Repository layout

```text
C:\Scripts\WallpaperRotator\
├── rotator.py            # application; run once per invocation
├── install-task.ps1      # registers the scheduled task
├── uninstall-task.ps1    # removes the scheduled task
├── README.md             # this document
└── tests\
    └── test_rotator.py   # automated regression suite (stdlib unittest)

%LOCALAPPDATA%\WallpaperRotator\
├── state.json            # queue, cycle number, last shown image
└── rotator.log           # rotating log (256 KB, 2 backups)
```

`rotator.py` has no third-party dependencies. The tests stub the wallpaper API
and never modify the real desktop.

---

## 4. Configuration

All tunables are module-level constants at the top of `rotator.py`.

| Constant | Default | Meaning |
| --- | --- | --- |
| `WALLPAPER_DIR` | `C:\Wallpapers` | Source folder, scanned non-recursively. |
| `ROTATION_INTERVAL_MINUTES` | `60` | Task repetition interval. Parsed by `install-task.ps1`. |
| `IMAGE_EXTENSIONS` | `.jpg .jpeg .png .bmp .webp` | Case-insensitive extension allow-list. |
| `APP_DIR` | `%LOCALAPPDATA%\WallpaperRotator` | Runtime data directory. |
| `LOG_MAX_BYTES` | `262144` | Log rotation threshold (~256 KB). |
| `LOG_BACKUP_COUNT` | `2` | Retained rotated logs. |
| `MUTEX_NAME` | `Local\WallpaperRotator.SingleInstance` | Single-instance guard, scoped to the logon session. |

`ROTATION_INTERVAL_MINUTES` is the single source of truth for scheduling: the
installer reads it with a regular expression, so changing the constant and
re-running `install-task.ps1 -Force` updates the task. The Python code itself
never sleeps or schedules anything.

**WebP support.** `.webp` is enabled because Windows 11 ships the WebP Image
Extension (verified here: `Microsoft.WebpImageExtension 1.2.31`).
`SystemParametersInfoW` returns success and Windows produces a valid
`TranscodedWallpaper` for WebP sources. On a machine without the codec, the
wallpaper call fails and the log records the error; remove `.webp` from
`IMAGE_EXTENSIONS` in that case.

---

## 5. Installation

```powershell
cd C:\Scripts\WallpaperRotator
.\install-task.ps1
```

If script execution is blocked:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-task.ps1
```

### Parameters

| Parameter | Default | Purpose |
| --- | --- | --- |
| `-TaskName` | `WallpaperRotator` | Task name in Task Scheduler. |
| `-PythonwPath` | auto-detected | Override the `pythonw.exe` path. |
| `-IntervalMinutes` | `0` (use config) | Override `ROTATION_INTERVAL_MINUTES` for the trigger only. |
| `-RunNow` | off | Start the task once after registering. |
| `-Force` | off | Replace an existing task. |

The installer validates that `rotator.py` exists, resolves the interpreter,
builds a complete Task Scheduler XML document, registers it, and prints the
resulting state. Registration is idempotent only with `-Force`; without it an
existing task is never silently replaced.

### Registered task

| Setting | Value | Reason |
| --- | --- | --- |
| Action | `pythonw.exe "<path>\rotator.py" --next` | No console window. |
| Trigger | Logon, repeating every `ROTATION_INTERVAL_MINUTES` indefinitely | Anchors the cadence to the moment you sign in. |
| Logon type | Interactive token | Runs in your desktop session, where the wallpaper lives. |
| Run level | Least privilege | No elevation needed. |
| Multiple instances | `IgnoreNew` | Overlapping starts are discarded. |
| Hidden | `true` | No UI presence in Task Scheduler by default. |
| Start when available | `true` | A run missed during sleep starts at wake. |
| Execution time limit | `PT0S` (unlimited) | Never terminated mid-run. |
| Battery | Start allowed, no stop on switch | Laptops rotate normally. |

> **Why the cadence is anchored to logon.** The repetition belongs to the logon
> trigger, so its start time is the sign-in time, not a fixed wall-clock time.
> Signing in at 12:32 rotates immediately and then at 13:32, 14:32, and so on;
> signing in at 16:05 the next day rotates at 16:05, 17:05, and so on. Each new
> logon re-anchors the sequence.
>
> Consequence: `NextRunTime` is empty while no logon trigger instance exists —
> for example immediately after registering the task in an active session.
> Nothing is broken; the cadence starts at the next sign-in. Use `-RunNow` or
> `Start-ScheduledTask` for an immediate one-off rotation in the meantime.

> **Elevation.** `Register-ScheduledTask` uses the CIM provider and can be denied
> for a non-elevated shell (`0x80070005`). The installer falls back to
> `schtasks.exe /create /xml` with the identical XML, so installation works
> without UAC. Both paths produce the same task.

---

## 6. CLI reference

```powershell
python .\rotator.py [--next | --dry-run N | --status | --reset]
```

> If `python` opens the Microsoft Store instead of running, the terminal predates
> the Python installation and still holds the old `PATH`. Open a new terminal
> (Python is prepended to the user `PATH`, ahead of the Store alias). The
> scheduled task is unaffected: it stores the absolute `pythonw.exe` path.

| Flag | Effect | Exit code |
| --- | --- | --- |
| *(none)* / `--next` | Perform one rotation now. | 0 success, 1 failure |
| `--dry-run N` | Print the next `N` picks; no wallpaper change, no state write. | 0 clean, 1 repeats detected |
| `--status` | Show image count, queue length, cycle, last/next image, file paths. | 0 |
| `--reset` | Delete `state.json` and build a fresh shuffled queue. | 0 success, 1 failure |

Notes:

* Flags are mutually exclusive.
* `--dry-run` uses a fixed RNG seed so repeated runs produce identical output and
  a "no repeats" check is reproducible. The persisted queue is replayed as-is,
  so when a queue exists its first picks match the real ones. With an empty queue
  the whole simulation is necessarily invented.
* The scheduled task only ever uses `--next`, which prints nothing. Interactive
  flags print to stdout, and all activity is also logged.

---

## 7. Operations runbook

### Install / reinstall

```powershell
cd C:\Scripts\WallpaperRotator
.\install-task.ps1                 # first install
.\install-task.ps1 -Force          # reinstall after changing config
.\install-task.ps1 -RunNow         # install and rotate immediately
```

Re-registering replaces the trigger but does not rotate immediately: the new
cadence begins at your next logon. Use `-RunNow` (or `Start-ScheduledTask`) if
you want a rotation right away.

### Run now

```powershell
Start-ScheduledTask -TaskName WallpaperRotator
```

### Check registration and execution

```powershell
Get-ScheduledTask -TaskName WallpaperRotator | Format-List TaskName,State,Settings
Get-ScheduledTaskInfo -TaskName WallpaperRotator | Format-List LastRunTime,LastTaskResult,NextRunTime
schtasks /query /tn WallpaperRotator /v /fo LIST
```

`State` should be `Ready` and `LastTaskResult` should be `0`. A non-zero result
means `rotator.py` exited with an error; see the log.

### View the log

```powershell
Get-Content "$env:LOCALAPPDATA\WallpaperRotator\rotator.log" -Tail 40 -Wait
notepad "$env:LOCALAPPDATA\WallpaperRotator\rotator.log"
```

### Pause / resume

```powershell
Disable-ScheduledTask -TaskName WallpaperRotator   # stop future runs
Enable-ScheduledTask  -TaskName WallpaperRotator   # resume
Stop-ScheduledTask    -TaskName WallpaperRotator   # stop a running instance
```

### Uninstall

```powershell
.\uninstall-task.ps1                # remove task, keep state and log
.\uninstall-task.ps1 -RemoveData    # remove task, state and log
```

### State inspection

```powershell
Get-Content "$env:LOCALAPPDATA\WallpaperRotator\state.json"
```

---

## 8. Architecture and design decisions

### 8.1 Execution model

```text
Task Scheduler
      │  pythonw.exe rotator.py --next   (at logon, then every interval)
      ▼
┌──────────────────────────────────────────────────────────────┐
│ main()                                                       │
│  configure_logging()                                         │
│  scan_folder() ──────────────► [image paths]                 │
│  load_state()  ──────────────► {queue, last, cycle}          │
│  acquire_single_instance() ──► named mutex                   │
│  rotate_once()                                               │
│    pick_next() ──────────────► image, remaining, cycle       │
│    set_wallpaper() ──────────► SPI + per-monitor verify      │
│    save_state() ─────────────► atomic write                  │
│  release_single_instance()                                   │
└──────────────────────────────────────────────────────────────┘
```

**Why run-once instead of a sleeping process**

* No process can hold a file lock, leak memory, or be interrupted by
  sleep/hibernate in a half-finished state.
* Reboots and logoffs need no watchdog; the scheduler starts a fresh process.
* The cost is one interpreter start per interval, which is negligible.

### 8.2 Selection algorithm (shuffle bag)

`pick_next()` implements a shuffle bag rather than `random.choice()`:

1. Prune queued names that no longer exist on disk.
2. If the bag is empty, shuffle the scanned images and start a new cycle. If the
   first pick would repeat the previously shown image, swap it with a random
   other entry (only when 2+ images exist).
3. Return the head of the bag and the remaining queue.

This guarantees that every image appears exactly once per cycle, which
`random.choice()` cannot do: it permits immediate repeats and can starve images
indefinitely.

New files added mid-cycle are picked up at the next cycle boundary, so the
"exactly once per cycle" invariant remains true while a cycle is in progress.
Deleted files are dropped on the next run without an error.

### 8.3 State management

State lives in `%LOCALAPPDATA%\WallpaperRotator\state.json`:

```json
{
  "version": 1,
  "cycle": 2,
  "last": "example.webp",
  "queue": ["a.jpg", "b.png"],
  "updated_at": "2026-10-29T01:05:00"
}
```

| Field | Meaning |
| --- | --- |
| `version` | Schema version, for future migrations. |
| `cycle` | Increments each time a new shuffle cycle begins. |
| `last` | File name of the last successfully applied image. |
| `queue` | Remaining file names, in display order. |
| `updated_at` | Last successful commit (local time). |

**File names, not absolute paths.** The folder is a configuration constant, so
storing bare names keeps the file readable and survives a folder move. Values
that look like paths are reduced to their basename on load.

**Atomic writes.** `save_state()` writes `state.json.tmp`, flushes and `fsync`s
it, then calls `os.replace()`. Replacement is atomic on the same volume, so a
crash or power loss leaves either the previous complete file or the new complete
file, never truncated JSON.

**Commit after success.** The queue is popped in memory, but state is written
only after the wallpaper call has verifiably succeeded. A failed run leaves the
state byte-identical, so the same image is retried on the next invocation
instead of being skipped.

**Recovery.** A missing state file is normal on first run. Unreadable, malformed
or wrongly-typed state is logged as a warning, discarded, and rebuilt as a fresh
cycle. Hand-edited state is tolerated: absolute paths, duplicate entries and
unknown fields are all handled.

### 8.4 Applying the wallpaper

The primary call is:

```c
SystemParametersInfoW(SPI_SETDESKWALLPAPER, 0, absolute_path,
                      SPIF_UPDATEINIFILE | SPIF_SENDCHANGE)
```

* `SPI_SETDESKWALLPAPER` (20) sets the desktop image for the current user.
* `SPIF_UPDATEINIFILE` persists the change to the user profile.
* `SPIF_SENDCHANGE` broadcasts `WM_SETTINGCHANGE` so the shell redraws
  immediately.
* The path is resolved to an absolute path first; relative paths are not
  reliably accepted.
* The call is an ordinary API call: no window, no focus change, no UI.
  `ctypes.get_last_error()` supplies the error code when it returns false.

**Registry style values.** Before each set, the tool writes these under
`HKCU\Control Panel\Desktop`:

| Value | Setting | Reason |
| --- | --- | --- |
| `WallpaperStyle` | `"10"` (Fill) | Each monitor gets the image scaled to cover its own screen. |
| `TileWallpaper` | `"0"` | Tiling would repeat the image instead of filling. |

Both are `REG_SZ` strings, not DWORDs; Windows reads them as text. `"22"` (Span)
is deliberately never used: Span treats both monitors as one wide canvas and
stretches a single image across them, which is visually different from "the same
image on each monitor".

**Per-monitor verification.** After a successful SPI call, the tool queries
`IDesktopWallpaper::GetWallpaper` for every monitor and compares the results
(normalised with `os.path.normcase`) against the requested file.

**COM fallback.** If the monitors report different images, the tool calls
`IDesktopWallpaper::SetWallpaper(NULL, path)`, where a `NULL` monitor ID means
"all monitors", then re-verifies. This path exists because SPI is the legacy
single-wallpaper API: when a monitor's wallpaper has previously been pinned
individually (Settings → Personalisation, or a third-party tool), SPI can leave
one screen on the old image. The COM interface addresses monitors explicitly and
is the reliable way to force them to match. The log records `via SPI` or
`via COM` for every change.

The COM binding is implemented directly with `ctypes` (no `comtypes`): the
`IDesktopWallpaper` vtable is addressed by slot number (3 `SetWallpaper`,
4 `GetWallpaper`, 5 `GetMonitorDevicePathAt`, 6 `GetMonitorDevicePathCount`),
and COM-allocated strings are released with `CoTaskMemFree`.

**Logging.** `RotatingFileHandler` with `maxBytes=256 KB` and
`backupCount=2`, in UTF-8, format
`YYYY-MM-DD HH:MM:SS LEVEL message`. Every rotation logs the image name, the
remaining queue length, the cycle number and the API path used; every failure is
logged with its exception.

### 8.5 Concurrency

Two independent guards:

* Task Scheduler's `MultipleInstancesPolicy = IgnoreNew` discards overlapping
  starts.
* A named mutex `Local\WallpaperRotator.SingleInstance` is acquired for the
  duration of the rotation and closed in a `finally` block. This covers manual
  `--next` runs racing the scheduled task. A second instance logs the condition
  and exits 0, because it is a normal no-op rather than an error.

The `Local\` prefix scopes the mutex to the logon session, so different users can
rotate their own desktops independently.

### 8.6 Error handling and exit codes

| Situation | Behaviour | Exit |
| --- | --- | --- |
| Rotation applied and committed | Logged | 0 |
| Another instance running | Logged, no changes | 0 |
| Folder missing or empty | Logged as error | 1 |
| Wallpaper API failure | Logged; queue untouched | 1 |
| State write failure after apply | Logged; image may repeat | 1 |
| Unexpected exception | Logged with traceback | 1 |

Nothing is ever written to stdout by the scheduled task, so a failure is
diagnosed from `rotator.log` and `LastTaskResult`.

---

## 9. Testing

### Automated tests

```powershell
cd C:\Scripts\WallpaperRotator
python -m unittest discover -s tests -v
```

The suite redirects state, log and wallpaper paths to a temporary directory and
stubs the wallpaper API, so it never changes the real desktop. Coverage:

| Test | Invariant |
| --- | --- |
| `test_cycle_contains_each_image_exactly_once` | Full cycle, no duplicates, boundary differs |
| `test_failed_apply_does_not_commit` | Failure leaves state byte-identical |
| `test_corrupt_state_is_rebuilt` | Malformed JSON recovers into a full cycle |
| `test_stale_queue_entries_are_dropped` | Deleted files are pruned |
| `test_missing_folder_exits_non_zero` | Missing folder fails cleanly |
| `test_empty_folder_exits_non_zero` | Empty folder fails cleanly |
| `test_single_image_rotates_every_run` | One-image folder always applies |
| `test_dry_run_does_not_mutate_state` | Dry run is side-effect free |
| `test_second_instance_is_a_no_op` | Mutex prevents concurrent rotation |
| `test_mutex_is_released_after_run` | No handle leak between runs |
| `test_state_write_is_atomic` | No `.tmp` file left behind |
| `test_status_exits_zero` | Status command works |

Expected result: `Ran 12 tests ... OK`.

### Manual acceptance procedure

**1. Both monitors show the identical image**

1. Note the current desktop.
2. `python .\rotator.py --next`
3. Confirm both screens show the same picture, each filled to its own screen
   (not one wide image split across the bezel).
4. Confirm the log line `Set wallpaper: <name> | N left in queue | cycle C | via SPI`.
   `via COM` means the SPI result disagreed and the fallback corrected it.
5. Settings → Personalisation → Background should show a single picture, Fit = Fill.

**2. No repeats within a cycle**

```powershell
python .\rotator.py --dry-run 40
```

Each block between `--- cycle N starts ---` markers must contain distinct names,
and the first name of a block must differ from the last name of the previous one.
The final line must read `OK: <n> image(s) per cycle, ... no repeats.`

**3. Cycle continues after reboot**

1. `python .\rotator.py --status` — note queue length, last image, cycle.
2. Reboot; the logon trigger rotates once.
3. `--status` again — the queue is one shorter, `last` is the new image, the
   cycle number is unchanged unless the queue crossed a boundary.

**4. No window flashes**

1. Open a fullscreen application or game.
2. `Start-ScheduledTask -TaskName WallpaperRotator`
3. No console, notification or focus change should occur; only the wallpaper
   changes. Task Manager shows `pythonw.exe` for well under a second and nothing
   remains resident.

**5. Failure does not skip an image**

1. `--status` to note the next queued name.
2. Rename that file temporarily.
3. `--next`: the stale entry is dropped and the following image is applied.
4. Rename the file back; it returns in the next cycle.

---

## 10. Troubleshooting

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `LastTaskResult` is 1 | Script error (empty folder, API failure, state write) | Read `rotator.log`. |
| Nothing rotates, `NextRunTime` empty | Expected until the next logon (logon-anchored schedule) | Sign out and back in; use `Start-ScheduledTask` for an immediate run. |
| Rotation happens at an unexpected minute | Task was re-registered; cadence re-anchors at next logon | Log off and on to re-anchor to your sign-in time. |
| Old wallpaper reappears | Another tool or the built-in slideshow is active | Set Background to a single picture; check other tasks. |
| One monitor shows the old image | Per-monitor pin left by another tool | Re-run `--next`; the COM fallback should correct it. |
| Log does not mention `via COM` but monitors differ | Verification unavailable (COM blocked) | Run `python .\rotator.py --next` manually and inspect. |
| Installer says `pythonw.exe was not found` | Python missing or Store stub only | `winget install -e --id Python.Python.3.13`. |
| Task never runs on battery | Policy/group policy override | Inspect task settings in Task Scheduler. |
| Rotation skipped after sleep | Machine was asleep | Expected; `StartWhenAvailable` runs it at wake. |

Diagnostics:

```powershell
Get-ScheduledTaskInfo -TaskName WallpaperRotator | Format-List *
Get-Content "$env:LOCALAPPDATA\WallpaperRotator\rotator.log" -Tail 50
python .\rotator.py --status
```

---

## 11. Maintenance

**Change the interval**

1. Edit `ROTATION_INTERVAL_MINUTES` in `rotator.py`.
2. `.\install-task.ps1 -Force`

**Add or remove images**

Copy or delete files in `WALLPAPER_DIR`. Additions join at the next cycle
boundary; deletions are pruned automatically on the next run.

**Restart the sequence**

```powershell
python .\rotator.py --reset
```

**Move the wallpaper folder**

Update `WALLPAPER_DIR` and re-run `--reset`, because the stored queue refers to
file names in the old folder.

**Upgrade**

Replace `rotator.py` and the PowerShell scripts, then run
`.\install-task.ps1 -Force`. `state.json` is forward-compatible within schema
version 1; an unknown version is discarded like any other malformed state.

---

## 12. Known limitations

* Windows Task Scheduler cannot run tasks while the machine is asleep; rotation
  resumes at wake, not on the original wall-clock cadence.
* Wallpapers are applied per user session; the task only runs while the user is
  logged on (`InteractiveToken`).
* Image file contents are not validated. A corrupt image fails at the wallpaper
  call, is logged, and is retried until it is removed or replaced.
* WebP depends on the Microsoft WebP Image Extension being installed.
* Rotating to the *previous* image is prevented at cycle boundaries only; inside
  a cycle the shuffle order is random and may produce visually similar
  neighbours.
* The COM fallback assumes the standard `IDesktopWallpaper` vtable. A future
  Windows version that changes it would surface as a verification failure, not
  silent misbehaviour.

---

## 13. Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.1.0 | 2026-10-31 | Schedule anchored to logon: the repetition now belongs to the logon trigger, so the cadence starts at sign-in instead of a fixed daily time. |
| 1.0.1 | 2026-10-29 | Documentation: added AI assistance disclosure. No functional changes. |
| 1.0.0 | 2026-10-29 | Initial release: run-once rotator, shuffle-bag state, SPI + COM verification, scheduled-task installer, regression suite. |
