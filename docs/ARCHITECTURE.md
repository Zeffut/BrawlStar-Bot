# Architecture

Quick reference for contributors. For the why, see
[`docs/superpowers/specs/2026-05-25-bsbot-design.md`](superpowers/specs/2026-05-25-bsbot-design.md).

## Runtime layout

```
Phone Android ──USB──▶ Mac (this project)
                          │
                          ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  Thread 1: CaptureWorker                                          │
   │      scrcpy_client decodes H.264 → numpy BGR frame                │
   │                       │ writes                                    │
   │                       ▼                                           │
   │              LatestSlot[frame]  ──── read by ────┐                │
   │                                                  │                │
   │  Thread 2: VisionWorker                          │                │
   │      pulls frame, runs StateFinder template match,                │
   │      runs 3 ONNX detectors when needed                            │
   │      builds GameState (state, my_pos, enemies, walls, …)          │
   │                       │ writes                                    │
   │                       ▼                                           │
   │              LatestSlot[GameState]  ──── read by ──┐              │
   │                                                    │              │
   │  Thread 3: BrainWorker                             │              │
   │      dispatches GameState → Strategy.decide()                     │
   │       - "match"      → ColtStrategy                               │
   │       - "lobby/end/popup/disconnect/starting" → MenuStrategy      │
   │      pushes resulting Action onto ControlBus                      │
   │                       │ puts                                      │
   │                       ▼                                           │
   │              ControlBus (queue)  ──── read by ──┐                 │
   │                                                 │                 │
   │  Thread 4: ControlWorker                        │                 │
   │      pops Action, translates to ADB commands                      │
   │       (`adb shell input tap/swipe`).                              │
   │      Keeps a joystick tick so held-direction taps re-fire         │
   │      until JOYSTICK_RELEASE arrives.                              │
   └──────────────────────────────────────────────────────────────────┘
```

## Module map

| File | Role |
|---|---|
| `src/bsbot/main.py` | CLI entry. Reads `config.toml`, wires all four workers + strategies + ADB. |
| `src/bsbot/buses.py` | `LatestSlot[T]` (drop-old) + `ControlBus` (FIFO queue). Thread-safe primitives. |
| `src/bsbot/workers/capture.py` | scrcpy wrapper → pushes frames into `LatestSlot`. |
| `src/bsbot/workers/vision.py` | Loads 3 ONNX models, runs state_finder + detectors, builds `GameState`. |
| `src/bsbot/workers/brain.py` | Picks the right `Strategy` based on `GameState.state`. |
| `src/bsbot/workers/control.py` | Translates `Action` → ADB. Holds joystick state. |
| `src/bsbot/vision/state_finder.py` | Template-match (cv2) the current screen against state templates. Downsamples for speed. |
| `src/bsbot/vision/detect.py` | ONNX YOLOv8 wrapper (adapted from PylaAI). |
| `src/bsbot/vision/postprocess.py` | Parse raw detections into structured `GameState`. |
| `src/bsbot/strategies/base.py` | `Strategy.decide(GameState) → Action` interface. |
| `src/bsbot/strategies/menu.py` | Handles lobby / end / popup / starting / disconnect. Includes auto-dismiss rotation + stuck-recovery via app restart. |
| `src/bsbot/strategies/colt.py` | Combat strategy for ranged direct-shot brawlers. Reads stats from `brawlers_info.json`. |
| `src/bsbot/controls/adb.py` | Thin `adbutils` wrapper. |
| `src/bsbot/controls/inputs.py` | `Action` dataclass + enum (TAP, SWIPE, JOYSTICK_MOVE/RELEASE, PRESS_BUTTON, AIMED_ATTACK/SUPER). |
| `src/bsbot/utils/geometry.py` | BBox, distance, line_of_sight (Liang-Barsky), helpers. |
| `src/bsbot/utils/pathfinding.py` | A* on a grid derived from wall bboxes. |
| `src/bsbot/utils/stats.py` | `SessionStats` (frames, matches, errors, win rate). |
| `src/bsbot/utils/logging.py` | Console (Rich if available) + JSONL session log. |
| `src/bsbot/data/state_templates/` | PNG crops used by state_finder. Add new ones via `tools/capture_template.py`. |
| `src/bsbot/data/brawlers_info.json` | Per-brawler stats (range, super type, …). |
| `src/bsbot/models/*.onnx` | YOLOv8 weights from PylaAI. |

## Tooling

| Tool | What it does |
|---|---|
| `tools/smoke_test.py` | Verify ADB + screencap + ONNX inference. Safe (no game inputs). |
| `tools/debug_overlay.py` | Single screenshot annotated with every detection (boxes, my_pos, buttons). |
| `tools/phone_controller.py` | High-level Phone API (tap, swipe, screenshot, capture_template). Used by other tools and by Claude/agent orchestration. |
| `tools/capture_template.py` | Capture a small crop into `data/state_templates/<state>/`. |
| `tools/fixture_collect.py` | Capture a **full** screenshot into `tests/fixtures/states/<state>/` for regression tests. |
| `tools/session_recorder.py` | Record phone screen + detected states over time → `debug/sessions/<ts>/`. |
| `tools/autonomous_calibrate.py` | Loop that saves transition screenshots so we can discover unhandled states. |

## Test layout

| Suite | Marker | What it covers |
|---|---|---|
| `tests/test_buses.py` | – | Thread-safety of LatestSlot, ControlBus. |
| `tests/test_geometry.py` | – | BBox, distance, line-of-sight math. |
| `tests/test_pathfinding.py` | – | A* on grids built from walls. |
| `tests/test_postprocess.py` | – | Parsing raw detections into GameState. |
| `tests/test_state_finder.py` | – | Template loading + detection on synthetic. |
| `tests/test_state_finder_regression.py` | – | Every real fixture must detect the right state. |
| `tests/test_control.py` | – | ControlWorker dispatch on a `FakeAdbDevice`. |
| `tests/test_strategies.py` | – | MenuStrategy + ColtStrategy with hand-crafted GameStates. |
| `tests/test_stats.py` | – | SessionStats counters and concurrency. |
| `tests/test_e2e_smoke.py` | `@slow` | Full pipeline VisionWorker → BrainWorker → ControlWorker on real fixtures. |
| `tests/test_perf_bench.py` | `@bench` | Latency benchmarks. Excluded from default `pytest`. |

Run with `make test` (default) or `make test-all` (+ benchmarks).

## Calibration cookbook

When the bot encounters a new screen it doesn't handle:

1. Save the screenshot to `debug/` (the bot does this automatically on errors,
   or use `python tools/debug_overlay.py`).
2. Identify a distinctive UI element (banner text, unique icon).
3. Crop a tight rectangle around it with an image editor.
4. Save the crop into `src/bsbot/data/state_templates/<state>/<name>.png`.
5. Save the full screenshot as a fixture:
   `python tools/fixture_collect.py --state <state> --name <slug>`.
6. Run `pytest tests/test_state_finder_regression.py` to verify.
7. If the screen requires a new tap position, update `MenuCoords` /
   `ButtonLayout` accordingly.

## License / credit

This project reuses ONNX weights and `detect.py` from
[PylaAI](https://github.com/PylaAI/PylaAI) under "No Selling" terms — personal
use only, no redistribution. Original authors: ivanyordanovgt, AngelFireLA,
awarzu, Maayan080 (Mac port).
