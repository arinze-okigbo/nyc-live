---
name: feed-cameras
description: NYC DOT traffic camera list and per-camera JPEG frame fetching with the 2 second cadence, plus the key-gated 511NY camera stub. Use for src/nyc_live/feeds/cameras.py and ny511.py.
tools: Read, Write, Edit, Glob, Grep, Bash
---
# feed-cameras

You own `src/nyc_live/feeds/cameras.py` (CameraListAdapter implementing FeedAdapter[Camera]; CameraFrameSource implementing FrameSource) and `src/nyc_live/feeds/ny511.py` (Ny511CamerasAdapter, key-gated on `NY511_API_KEY`).

## Facts (verified; do not re-research)
- `https://webcams.nyctmc.org/api/cameras/` returns the camera list (~900). `https://webcams.nyctmc.org/api/cameras/{id}/image` returns a JPEG refreshing about every 2 s. No auth.
- Camera ids are UUIDs and can rotate: refresh the list on `DEFAULT_TTL[DOT_CAMERAS]`, never hardcode ids, and tolerate ids disappearing between list refreshes (`ErrorKind.NOT_FOUND` on a 404 for a frame).

## Deliverables
- `CameraFrameSource.get_frame(camera_id)`: enforces `CAMERA_FRAME_MIN_INTERVAL` per camera id with `nyc_live.http.RateLimiter`, keeps at most `FRAME_BUFFER_MAX_FRAMES_PER_CAMERA` frames per camera in memory, evicts after `FRAME_BUFFER_MAX_AGE`, and never writes a frame to disk. Returns the buffered frame when a caller asks again inside the cadence window. Records a `CameraFrameFetch` telemetry row for every upstream attempt via a callback or list the caller can drain (do not open DuckDB yourself).
- Validate JPEG magic bytes; a non-JPEG body is `ErrorKind.UPSTREAM_PARSE`.
- Drop cameras outside `NYC_BBOX` and log the count.
- Tests: live list test (>500 online cameras, all ids unique, coordinates in bbox); live frame test (fetch one online camera, assert JPEG and size > 1 KB; fetch it twice and assert the second call did not hit upstream inside 2 s); offline tests replaying `tests/fixtures/cameras/` via `respx`. 511NY stub: skip path test only.

## Rules you must follow
- Read `CLAUDE.md` and `src/nyc_live/contracts.py` before writing anything. `contracts.py` is frozen; if you need a change, stop and report the exact diff you need. Do not work around it.
- Touch only the files in your ownership row in `CLAUDE.md`. Never edit `pyproject.toml`, the `justfile`, `feeds/__init__.py`, or another agent's files. Need a dependency? Report it.
- Never fabricate data or fixtures. Fixtures are trimmed copies of real responses.
- Run `just fmt` then `just check` before you report done. Report exact command output for failures, not paraphrases.
- Your final report: what you shipped (file list), what the gate measured (numbers), what broke, and any contract or dependency change you need. No code summaries.
