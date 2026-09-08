# Privacy

nyc-live processes public traffic-camera imagery to produce aggregate counts. It is designed so that no image, and nothing derived from an identifiable person or vehicle, is ever persisted.

## What is stored

Per camera, per sample: timestamp, detection class (person, bicycle, car, motorcycle, bus, truck), count, mean confidence, mean bounding-box area as a fraction of the frame, model name, inference time, frame dimensions. That is the full `density_samples` row defined in `contracts.py`. No box coordinates, no crops, no embeddings, no tracks across frames.

## What is not stored

* Raw frames are never written to disk, to Parquet, or to DuckDB.
* Frames live only in an in-memory rolling buffer: at most `FRAME_BUFFER_MAX_FRAMES_PER_CAMERA` (1) per camera, evicted after `FRAME_BUFFER_MAX_AGE` (10 minutes). The buffer exists so the MCP tool `get_camera_frame` and the detector can share one fetch without exceeding the 2 second upstream cadence.
* `get_camera_frame` returns the current public JPEG to the caller on request. It is the same image the DOT site serves publicly; nyc-live adds no retention.

## Why a buffer at all

Without it, the detector and any tool call would each fetch the camera independently, doubling load on DOT and breaking the 2 second cadence rule. A single-frame, ten-minute buffer is the minimum that lets both share one fetch.

## Other feeds

311 records, inspections, subway, bike, and weather data are public datasets served as published. nyc-live does not join them to camera data at the individual level, only spatially on a map.
