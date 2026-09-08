# Recording the transit fixtures

Run these on a machine with unrestricted network access, from the repo root. Every
fixture is a trimmed copy of a real response; never hand-edit or invent entities.

Set the same User-Agent the adapters send:

```bash
UA='nyc-live/0.1 (https://github.com/arinze-okigbo/nyc-live)'
BASE='https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds'
OUT=tests/fixtures/transit
```

## 1. GTFS-realtime trip feeds (`nyct-<slug>.pb`, one per slug)

```bash
for slug in gtfs gtfs-ace gtfs-bdfm gtfs-g gtfs-jz gtfs-nqrw gtfs-l gtfs-si; do
  curl -fsS -H "User-Agent: $UA" "$BASE/nyct/$slug" -o "$OUT/nyct-$slug.full.pb"
done
```

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
