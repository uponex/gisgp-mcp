# GISGP MCP Cookbook — real tasks, real tool calls

Copy-paste examples for common GIS tasks, phrased the way people actually
ask for them. Any MCP client (Claude, Cursor, Windsurf, custom agents)
connected to `gisgp-mcp` should resolve these automatically from natural
language — this file exists so both humans and coding agents scanning this
repo can see the exact call shape.

## "I need this Google Earth file as a Shapefile for ArcGIS"

```json
{"tool": "kml_to_shapefile", "arguments": {"kml": "<contents of the .kml file>"}}
```
Returns `{"shapefile_zip_base64": "...", "warnings": [...]}` — decode the
base64 to get a ready-to-import `.zip` (`.shp`/`.shx`/`.dbf`).

## "Turn this GPS track from my Garmin/Strava export into a map layer"

```json
{"tool": "gpx_to_geojson", "arguments": {"gpx": "<contents of the .gpx file>"}}
```
Returns a GeoJSON `FeatureCollection` — waypoints, tracks and routes as
separate features, ready to load in any web map or GIS tool.

## "My spreadsheet has lat/lon columns, I need it on a map"

```json
{"tool": "csv_to_geojson", "arguments": {"csv": "<contents of the .csv file>"}}
```
Coordinate columns are auto-detected (`lat`/`latitude`/`y`, `lon`/`lng`/`x`
and common variants) — no column-mapping step needed.

## "This GeoJSON file is huge, I need it smaller before I send it anywhere"

Two tools, often used together:

```json
{"tool": "simplify_geometry", "arguments": {"geojson": "<geojson>", "tolerance": 0.001}}
```
```json
{"tool": "reduce_precision", "arguments": {"geojson": "<simplified geojson>", "decimals": 6}}
```
`simplify_geometry` drops excess vertices (Douglas-Peucker); `reduce_precision`
rounds every coordinate (6 decimals ≈ 11cm, plenty for almost anything).
Chaining both typically cuts file size by 60-90% on dense polygon/line data.

## "I have coordinates from a database query, I need GeoJSON"

PostGIS/SQL geometry columns print as WKT (Well-Known Text) by default:

```json
{"tool": "wkt_to_geojson", "arguments": {"wkt": "POLYGON((...))"}}
```
The reverse direction (`geojson_to_wkt`) works for inserting a GeoJSON
geometry back into a PostGIS `INSERT`/`UPDATE`.

## "Find the center point of every parcel/building in this layer"

```json
{"tool": "centroids_geojson", "arguments": {"geojson": "<geojson>"}}
```
Returns one Point feature per input feature (properties kept) — useful for
labeling, or as input to a distance/nearest-neighbor calculation.

## "What's the elevation at this location?"

```json
{"tool": "get_terrain_profile", "arguments": {"lon": 23.32, "lat": 42.70}}
```
Free, no key, Copernicus DEM GLO-90 (~90m resolution) via Open-Meteo.
Returns `{"ok": true, "lon": ..., "lat": ..., "elevation_m": ..., "source": "Open-Meteo"}` —
`source` is always present so you know exactly where a number came from.

## "Geocode an address" (hosted endpoint only — `https://gisgp.com/mcp`)

```json
{"tool": "geocode_address", "arguments": {"address": "1600 Amphitheatre Parkway, Mountain View, CA"}}
```
Free, no key. Tries OSM Nominatim first, falls back to Photon (komoot),
falls back to the US Census Bureau geocoder for US addresses — three
independent free sources chained so one source being down/rate-limited
doesn't fail the whole lookup. Every response includes `source` so you know
which one actually answered.

---

Full tool list and hosted-only endpoints: see [README.md](README.md).
Docs: https://gisgp.com/api · Homepage: https://gisgp.com
