# Recording the DOT camera fixtures

`webcams.nyctmc.org` is blocked from the sandbox these tests were written in, so
the fixtures below are **not** checked in yet. The offline replay tests in
`tests/feeds/test_cameras.py` and `tests/feeds/test_cameras_frames.py` skip with
a reason naming the missing file until you record them. Never hand-write these
files: they must be trimmed copies of real responses.

Run from the repo root on a machine with network access.

## 1. `cameras.json` (camera list, trimmed to 20 real entries)

```sh
UA='nyc-live/0.1 (https://github.com/arinze-okigbo/nyc-live)'

curl -sS -H "User-Agent: $UA" "https://webcams.nyctmc.org/api/cameras/" \
  | jq 'if type == "array" then .[:20] else . end' \
  > tests/fixtures/cameras/cameras.json
```

If the response is a dict wrapping the array (e.g. `{"cameras": [...]}`), trim the
inner array instead, e.g. `jq '.cameras |= .[:20]'`, and tell the orchestrator so
the wrapper key can be confirmed against `cameras.py`.

Sanity check: `jq 'length' tests/fixtures/cameras/cameras.json` prints `20` and
`jq '.[0] | keys' tests/fixtures/cameras/cameras.json` lists the real field names
(expected: `id`, `name`, `latitude`, `longitude`, `area`, `isOnline`, `imageUrl`, ...).
If the names differ from the assumptions in the docstring of
`src/nyc_live/feeds/cameras.py`, report them.

## 2. `frame.jpg` (one JPEG frame from an online camera in the trimmed list)

```sh
UA='nyc-live/0.1 (https://github.com/arinze-okigbo/nyc-live)'
ID=$(jq -r '[.[] | select(.isOnline == true or .isOnline == "true")][0].id' \
       tests/fixtures/cameras/cameras.json)

curl -sS -H "User-Agent: $UA" "https://webcams.nyctmc.org/api/cameras/$ID/image" \
  -o tests/fixtures/cameras/frame.jpg

file tests/fixtures/cameras/frame.jpg       # must say "JPEG image data"
stat -c %s tests/fixtures/cameras/frame.jpg # must be > 1024 bytes
```

If `$ID` is empty the `isOnline` key name differs from the assumption; inspect
`jq '.[0]'` and pick any camera id by hand for the `curl` (do not edit the JSON).

Note on privacy: `frame.jpg` is a single static test fixture of a public traffic
camera. The runtime pipeline (`CameraFrameSource`) never writes frames to disk.
