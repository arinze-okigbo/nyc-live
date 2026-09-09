# Recording the transit fixtures

Run these on a machine with unrestricted network access, from the repo root. Every
fixture is a trimmed copy of a real response; never hand-edit or invent entities.

Trip feeds (`nyct-<slug>.pb`) and `subway-alerts.pb` were recorded 2026-09-08 and are
checked in; re-run the steps below only after an upstream schema change.

Set the same User-Agent the adapters send:

```bash
UA='nyc-live/0.1 (https://github.com/arinze-okigbo/nyc-live)'
BASE='https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds'
OUT=tests/fixtures/transit
```

## 1. GTFS-realtime trip feeds (`nyct-<slug>.pb`, one per slug)

```bash
for slug in gtfs gtfs-ace gtfs-bdfm gtfs-g gtfs-jz gtfs-nqrw gtfs-l gtfs-si; do
  curl -fsS -H "User-Agent: $UA" "$BASE/nyct%2F$slug" -o "$OUT/nyct-$slug.full.pb"
done
```

The `%2F` must be sent verbatim (one URL-encoded path segment); an unescaped `/nyct/$slug`
gets API Gateway's misleading `403 Missing Authentication Token` instead of the feed.

Trim each to its first 12 entities (keeps a mix of trip_update and vehicle entities for
the same trips, since NYCT interleaves them). This keeps every byte that survives real:

```bash
uv run python - <<'EOF'
from pathlib import Path
from google.transit import gtfs_realtime_pb2
out = Path("tests/fixtures/transit")
for full in sorted(out.glob("nyct-*.full.pb")):
    msg = gtfs_realtime_pb2.FeedMessage(); msg.ParseFromString(full.read_bytes())
    keep = list(msg.entity[:12]); del msg.entity[:]; msg.entity.extend(keep)
    slim = out / full.name.replace(".full.pb", ".pb")
    slim.write_bytes(msg.SerializeToString()); full.unlink()
    print(slim, len(keep), "entities, header ts", msg.header.timestamp)
EOF
```

## 2. Alerts (`subway-alerts.pb`)

The `%2F` is part of the path; do not decode it.

```bash
curl -fsS -H "User-Agent: $UA" "$BASE/camsys%2Fsubway-alerts" -o "$OUT/subway-alerts.full.pb"
uv run python - <<'EOF'
from pathlib import Path
from google.transit import gtfs_realtime_pb2
out = Path("tests/fixtures/transit")
msg = gtfs_realtime_pb2.FeedMessage(); msg.ParseFromString((out/"subway-alerts.full.pb").read_bytes())
keep = [e for e in msg.entity if e.HasField("alert")][:15]; del msg.entity[:]; msg.entity.extend(keep)
(out/"subway-alerts.pb").write_bytes(msg.SerializeToString()); (out/"subway-alerts.full.pb").unlink()
print(len(keep), "alerts, header ts", msg.header.timestamp)
EOF
```

## 3. Static GTFS (`gtfs_subway_trimmed.zip`) -- ALREADY RECORDED 2026-09-08

Source: `https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip` (Last-Modified 27 Aug 2026).
The checked-in zip holds `feed_info.txt` and `agency.txt` verbatim plus the real rows of
`stops.txt`, `trips.txt`, `stop_times.txt` for stations 127, R16, A27, L01, G22, S31
(one real trip per route/stop pair). To re-record after an upstream change:

```bash
curl -fsS -H "User-Agent: $UA" https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip -o /tmp/gtfs_subway.zip
uv run python - <<'EOF'
import csv, io, zipfile
SRC, DST = "/tmp/gtfs_subway.zip", "tests/fixtures/transit/gtfs_subway_trimmed.zip"
PARENTS = ["127", "R16", "A27", "L01", "G22", "S31"]
src = zipfile.ZipFile(SRC)
def rows(name):
    with src.open(name) as f:
        r = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig", newline="")); return next(r), list(r)
s_hdr, s_rows = rows("stops.txt")
keep_stops = [r for r in s_rows if r[0] in PARENTS or r[s_hdr.index("parent_station")] in PARENTS]
keep_ids = {r[0] for r in keep_stops}
t_hdr, t_rows = rows("trips.txt")
trip_route = {r[t_hdr.index("trip_id")]: r[t_hdr.index("route_id")] for r in t_rows}
st_hdr, st_rows = rows("stop_times.txt")
ti, si = st_hdr.index("trip_id"), st_hdr.index("stop_id")
touching = [r for r in st_rows if r[si] in keep_ids]
chosen, seen = set(), set()
for r in touching:
    pair = (trip_route[r[ti]], r[si])
    if pair not in seen: seen.add(pair); chosen.add(r[ti])
keep_st = [r for r in touching if r[ti] in chosen]
keep_trips = [r for r in t_rows if r[t_hdr.index("trip_id")] in chosen]
def csv_bytes(hdr, rs):
    buf = io.StringIO(); w = csv.writer(buf, lineterminator="\n"); w.writerow(hdr); w.writerows(rs)
    return buf.getvalue().encode()
with zipfile.ZipFile(DST, "w", zipfile.ZIP_DEFLATED) as out:
    out.writestr("feed_info.txt", src.read("feed_info.txt")); out.writestr("agency.txt", src.read("agency.txt"))
    out.writestr("stops.txt", csv_bytes(s_hdr, keep_stops)); out.writestr("trips.txt", csv_bytes(t_hdr, keep_trips))
    out.writestr("stop_times.txt", csv_bytes(st_hdr, keep_st))
print(len(keep_stops), "stops", len(keep_trips), "trips", len(keep_st), "stop_times")
EOF
```

If the station set or route assignments change upstream, update the expected values in
`tests/feeds/test_transit.py::test_stops_replays_recorded_trimmed_gtfs` to match the new
real rows; do not edit the zip by hand.

## 4. Route shapes (`shapes.txt` inside `gtfs_subway_trimmed.zip`) -- ALREADY RECORDED 2026-09-09

The trimmed zip above did not originally carry `shapes.txt` (only added once
`SubwayShapesAdapter` needed it). `trips.txt` in the trimmed zip already has a real
`shape_id` per trip (one real GTFS row, untouched), so this step only adds the matching
`shapes.txt` rows -- it does not touch `stops.txt`/`trips.txt`/`stop_times.txt`. Kept the
first 6 real points (by `shape_pt_sequence`, ascending) of each of the 26 shape_ids
already referenced by the trimmed `trips.txt`, i.e. a contiguous prefix of each real
shape, not a synthetic point. To re-record after an upstream change:

```bash
curl -fsS -H "User-Agent: $UA" https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip -o /tmp/gtfs_subway.zip
uv run python - <<'EOF'
import csv, io, zipfile
SRC, DST = "/tmp/gtfs_subway.zip", "tests/fixtures/transit/gtfs_subway_trimmed.zip"
src = zipfile.ZipFile(SRC)

def read_csv(name):
    with src.open(name) as f:
        r = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))
        return r.fieldnames, list(r)

with zipfile.ZipFile(DST) as existing:
    with existing.open("trips.txt") as f:
        existing_trips = list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline="")))
    existing_files = {n: existing.read(n) for n in existing.namelist()}

shape_ids = sorted({r["shape_id"] for r in existing_trips if r.get("shape_id")})
hdr, rows = read_csv("shapes.txt")
by_shape: dict[str, list[dict]] = {}
for row in rows:
    if row["shape_id"] in shape_ids:
        by_shape.setdefault(row["shape_id"], []).append(row)
kept = []
for sid in shape_ids:
    pts = sorted(by_shape[sid], key=lambda r: int(r["shape_pt_sequence"]))
    kept.extend(pts[:6])

def csv_bytes(fieldnames, dict_rows):
    buf = io.StringIO(); w = csv.writer(buf, lineterminator="\n"); w.writerow(fieldnames)
    for row in dict_rows: w.writerow([row[c] for c in fieldnames])
    return buf.getvalue().encode()

with zipfile.ZipFile(DST, "w", zipfile.ZIP_DEFLATED) as out:
    for name, data in existing_files.items():
        out.writestr(name, data)
    out.writestr("shapes.txt", csv_bytes(hdr, kept))
print(len(kept), "shape points for", len(shape_ids), "shapes")
EOF
```

If the shape_ids referenced by the trimmed `trips.txt` ever change, update the expected
values in `tests/feeds/test_transit.py::test_shapes_replays_recorded_trimmed_gtfs` to
match; do not edit the zip by hand.

## 5. Elevator/escalator outages (`nyct_ene.json`) -- ALREADY RECORDED 2026-09-09

Live shape that day: HTTP 200, a JSON array of 136 outage objects, `content-type:
${header.s3FileType}` (an unsubstituted template on MTA's side -- ignore it). The `%2F`
is part of the path, same rule as every feed above.

```bash
curl -fsS -H "User-Agent: $UA" "$BASE/nyct%2Fnyct_ene.json" -o "$OUT/nyct_ene.full.json"
```

The checked-in `nyct_ene.json` is **11 whole rows of that response, copied verbatim** and
re-serialised with `json.dumps(indent=2)` for a readable diff -- no key or value is
edited. They were chosen to cover every branch the adapter has: upcoming vs currently
out, `EL` vs `ES`, `ADA` `Y` vs `N`, a station that matches one GTFS station uniquely,
one that only matches inside a station complex, one whose routes contradict the
name match (`Cortlandt St` on the 1), and one whose name matches two stations 1.9 km
apart (`Gun Hill Rd`).

```bash
uv run python - <<'EOF'
import json
from pathlib import Path
out = Path("tests/fixtures/transit")
full = json.loads((out / "nyct_ene.full.json").read_text())
KEEP = {"EL224","EL229","EL230","EL712","ES105","EL289X","ES607X","EL445X","ES461X","EL14X","EL290X"}
kept = [r for r in full if r["equipment"] in KEEP]
(out / "nyct_ene.json").write_text(json.dumps(kept, indent=2) + "\n")
(out / "nyct_ene.full.json").unlink()
print(len(kept), "of", len(full), "rows")
EOF
```

Equipment ids are reused across scheduled windows, so `KEEP` may match more or fewer
rows after an upstream change; update the expected values in
`tests/feeds/test_accessibility.py` to the new real rows rather than editing the JSON.

### `nyct_ene_nosuchkey.xml` -- the 200-that-means-404

A wrong key on this endpoint does **not** 404: API Gateway proxies S3 and returns HTTP
200 with an XML error body. That is why `parse_outages` sniffs the body. Recorded from a
deliberately bogus key:

```bash
curl -fsS -H "User-Agent: $UA" "$BASE/nyct%2Fnyct_ene_does_not_exist.json" \
  -o "$OUT/nyct_ene_nosuchkey.xml"
```

### `gtfs_subway_ene_stops.zip` -- stops for the outage join

Same trimming recipe as section 3 (real `stops.txt` / `trips.txt` / `stop_times.txt`
rows, one real trip per route/stop pair) but for the parent stations the outage fixture
names, **including the two that must not be matched**: `R25` (the only GTFS "Cortlandt
St", N/R/W) and `208`/`503` (two distinct "Gun Hill Rd" stations 1,910 m apart). Kept
separate from `gtfs_subway_trimmed.zip` so the two test suites cannot break each other.

```bash
curl -fsS -H "User-Agent: $UA" https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip -o /tmp/gtfs_subway.zip
uv run python - <<'EOF'
import csv, io, zipfile
SRC, DST = "/tmp/gtfs_subway.zip", "tests/fixtures/transit/gtfs_subway_ene_stops.zip"
PARENTS = ["127", "725", "R16", "A27", "G22", "719", "L01", "208", "503", "R25"]
src = zipfile.ZipFile(SRC)
def rows(name):
    with src.open(name) as f:
        r = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig", newline="")); return next(r), list(r)
s_hdr, s_rows = rows("stops.txt")
keep_stops = [r for r in s_rows if r[0] in PARENTS or r[s_hdr.index("parent_station")] in PARENTS]
keep_ids = {r[0] for r in keep_stops}
t_hdr, t_rows = rows("trips.txt")
trip_route = {r[t_hdr.index("trip_id")]: r[t_hdr.index("route_id")] for r in t_rows}
st_hdr, st_rows = rows("stop_times.txt")
ti, si = st_hdr.index("trip_id"), st_hdr.index("stop_id")
touching = [r for r in st_rows if r[si] in keep_ids]
chosen, seen = set(), set()
for r in touching:
    pair = (trip_route[r[ti]], r[si])
    if pair not in seen: seen.add(pair); chosen.add(r[ti])
keep_st = [r for r in touching if r[ti] in chosen]
keep_trips = [r for r in t_rows if r[t_hdr.index("trip_id")] in chosen]
def csv_bytes(hdr, rs):
    buf = io.StringIO(); w = csv.writer(buf, lineterminator="\n"); w.writerow(hdr); w.writerows(rs)
    return buf.getvalue().encode()
with zipfile.ZipFile(DST, "w", zipfile.ZIP_DEFLATED) as out:
    out.writestr("feed_info.txt", src.read("feed_info.txt")); out.writestr("agency.txt", src.read("agency.txt"))
    out.writestr("stops.txt", csv_bytes(s_hdr, keep_stops)); out.writestr("trips.txt", csv_bytes(t_hdr, keep_trips))
    out.writestr("stop_times.txt", csv_bytes(st_hdr, keep_st))
print(len(keep_stops), "stops", len(keep_trips), "trips", len(keep_st), "stop_times")
EOF
```

If a station moves or is renamed upstream, update the expected coordinates in
`tests/feeds/test_accessibility.py` to the new real rows; do not edit the zip by hand.
