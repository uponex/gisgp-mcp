# GISGP MCP Server

Free remote MCP (Model Context Protocol) server for GIS/ArcGIS Online automation.

**Endpoint:** `https://gisgp.com/mcp` (Streamable HTTP, stateless, no auth for public layers)

## Client config

```json
{ "mcpServers": { "gisgp": { "url": "https://gisgp.com/mcp" } } }
```

## Local/stdio server (open-source subset)

`server.py` is a self-contained, open-source MCP server (stdio transport)
implementing the format-conversion and geometry tools that need no external
service access — no ArcGIS Online credentials, no network calls. Build and
run it with the included `Dockerfile`:

```bash
docker build -t gisgp-mcp .
docker run -i --rm gisgp-mcp
```

```json
{ "mcpServers": { "gisgp-local": { "command": "docker", "args": ["run", "-i", "--rm", "gisgp-mcp"] } } }
```

The tools that inspect/query a live ArcGIS Online FeatureServer
(`count_features`, `extract_domains`, `check_field_types`,
`check_service_health`, `rest_explore`, `compare_schemas`, `query_features`,
`query_statistics`) require a real AGOL connection and stay on the hosted
remote endpoint above — they are not part of this local server.

## Hosted QA tools (new, hosted endpoint only)

The hosted server at `https://gisgp.com/mcp` now also exposes ArcGIS Online QA tools:

| Tool | What it does |
|---|---|
| `grade_service` | Grade any FeatureServer A–F across 5 categories (schema, completeness, performance, maintenance, configuration) — returns a public shareable scorecard link |
| `audit_service` | One-call QA report: health + field schema + coded domains + summary counts |
| `find_layer_issues` | Scan a layer for problems: all-null fields, empty geometries, stale data, missing ObjectID, disabled query |
| `share_map` | Publish a GeoJSON FeatureCollection as a live shareable web map |

These require live ArcGIS/S3 infrastructure and are not part of the open-source subset below.

## Tools (25)

| Tool | Description |
|---|---|
| `convert_coordinates` | Convert coordinate pairs between EPSG coordinate systems |
| `validate_geojson` | Validate GeoJSON: RFC 7946 structure, topology, WGS84 ranges |
| `geojson_to_csv` | Convert a GeoJSON FeatureCollection to CSV |
| `count_features` | Count features in an ArcGIS FeatureServer layer, optional SQL WHERE |
| `extract_domains` | Extract coded value domains from a FeatureServer layer |
| `check_field_types` | Inspect field schema of a FeatureServer layer |
| `check_service_health` | Check reachability/latency/capabilities of a FeatureServer layer |
| `rest_explore` | Enumerate layers/tables of a FeatureServer/MapServer root |
| `compare_schemas` | Diff field schemas of two FeatureServer layers |
| `shapefile_to_geojson` | Convert a Shapefile ZIP to GeoJSON |
| `kml_to_geojson` | Convert KML to GeoJSON |
| `gpx_to_geojson` | Convert GPX to GeoJSON |
| `query_features` | Fetch feature records (attributes+geometry) from a FeatureServer layer, free preview capped at 50 |
| `query_statistics` | Server-side aggregate stats (sum/avg/min/max/count/stddev) on a numeric field, optional group-by — no records fetched |
| `geometry_stats` | Compute area/length/vertex count/centroid/bbox of GeoJSON (equal-area projection) |
| `reproject_geojson` | Reproject an entire GeoJSON between EPSG coordinate systems |
| `simplify_geometry` | Simplify GeoJSON geometry (Douglas–Peucker) |
| `reduce_precision` | Round GeoJSON coordinates to N decimal places (shrinks payload size) |
| `csv_to_geojson` | Convert CSV with auto-detected coordinate columns to GeoJSON |
| `geojson_to_kml` | Convert a GeoJSON FeatureCollection to KML |
| `geojson_to_shapefile` | Convert a GeoJSON FeatureCollection to a Shapefile ZIP |
| `gpx_to_kml` | Convert GPX to KML |
| `kml_to_shapefile` | Convert KML to a Shapefile ZIP |
| `wkt_to_geojson` | Convert a WKT geometry string to GeoJSON (e.g. from PostGIS) |
| `geojson_to_wkt` | Convert a GeoJSON geometry to a WKT string |

Docs: https://gisgp.com/api · Homepage: https://gisgp.com
