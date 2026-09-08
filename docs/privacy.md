# Privacy

nyc-live processes public traffic-camera imagery to produce aggregate counts. It is designed so that no image, and nothing derived from an identifiable person or vehicle, is ever persisted.

## What is stored

Per camera, per sample: timestamp, detection class (person, bicycle, car, motorcycle, bus, truck), count, mean confidence, mean bounding-box area as a fraction of the frame, model name, inference time, frame dimensions. That is the full `density_samples` row defined in `contracts.py` (`DensitySample`). No box coordinates, no crops, no embeddings, no tracks across frames.

Per frame fetch attempt: camera id, timestamp, success flag, HTTP status, latency, byte size, and an error string. That is the `camera_frame_fetches` row (`CameraFrameFetch`), kept for the frame-fetch failure gate. It contains no image data.

Those two tables are the whole of what the vision pipeline persists, plus the `cameras` row it
upserts so an aggregate can be given a name and a coordinate (camera_id, source, name, lat,
lon, is_online, first_seen, last_seen). Nothing else about a frame is kept.

## What is not stored

* Raw frames are never written to disk, to Parquet, or to DuckDB.
* Frames live only in an in-memory rolling buffer: at most `FRAME_BUFFER_MAX_FRAMES_PER_CAMERA` (1) per camera, evicted after `FRAME_BUFFER_MAX_AGE` (10 minutes). The buffer is `CameraFrameSource` in `src/nyc_live/feeds/cameras.py`. It exists so the MCP tool `get_camera_frame` and the detector can share one fetch without exceeding the `CAMERA_FRAME_MIN_INTERVAL` (2 second) upstream cadence.
* `get_camera_frame` (`packages/nyc-mcp/src/nyc_mcp/server.py`) returns the current public JPEG to the caller on request, as an MCP image block. It is the same image the DOT site serves publicly; nyc-live adds no retention. The `CameraFrame` bytes are excluded from the JSON envelope (`data` is `exclude=True` in `contracts.py`), so only the image block carries pixels, and only for the duration of that response.

## The vision pipeline

`packages/nyc-vision` is the only component that runs a model over a frame, and it is the only
writer of `density_samples` and `camera_frame_fetches`. Its path is: `FrameSource.get_frame()`
hands it JPEG bytes from the in-memory buffer, `nyc_vision.detector.decode_jpeg` decodes them
with Pillow in memory, `YoloDetector.detect` returns per-class counts with mean confidence and
mean box-area fraction, and `nyc_vision.pipeline.build_samples` turns that into the
`DensitySample` rows above. The bytes are never copied out of the buffer to disk, no crop or
annotated image is produced, and `detector.py` opens no file. Individual box coordinates are
summarised into a mean area fraction inside `summarize_boxes` and discarded; they are not
written anywhere. Zero counts are written too, so an absence is a recorded observation rather
than a hole, and `confidence_mean` and `bbox_area_frac_mean` stay `NULL` on a zero row.

The daily Parquet archive (`Store.archive_day`, `NYC_VISION_ARCHIVE`) copies rows of those same
two tables and nothing else. See `docs/vision.md`.

## The dashboard

`packages/nyc-dash` never receives a raw frame. It reads `nyc_live.services` only and has no
frame endpoint: the density layer is `Envelope[CameraDensity]` from
`services/density.py::density_now`, which is per-camera means, maxima and sample counts over
`density_samples`. The `dot_cameras` layer carries camera metadata (id, name, coordinates,
online flag, and the public DOT image URL as published by the city) but no image bytes. The
only place a frame reaches a client at all is the MCP `get_camera_frame` tool described above,
on explicit request, and nothing about it is retained.

## Why a buffer at all

Without it, the detector and any tool call would each fetch the camera independently, doubling load on DOT and breaking the 2 second cadence rule. A single-frame, ten-minute buffer is the minimum that lets both share one fetch.

## The frame test fixture

`tests/fixtures/cameras/RECORD.md` proposes checking in one real `frame.jpg`, a single static JPEG from a public DOT camera, as a fixture for the offline replay test in `tests/feeds/test_cameras_frames.py`. Checking a camera image into the repository is a decision the maintainer must make explicitly; it is not covered by the runtime rule above, which is about what the pipeline writes. Until that decision is made the fixture is absent and the replay test skips with `fixture not recorded yet`. The runtime pipeline never writes frames to disk regardless of that choice.

## Other feeds

311 records, inspections, subway, bike, and weather data are public datasets served as published. nyc-live does not join them to camera data at the individual level, only spatially on a map.
