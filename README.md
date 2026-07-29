# GISGP MCP Server

[![Glama MCP Server](https://glama.ai/mcp/servers/uponex/gisgp-mcp/badge)](https://glama.ai/mcp/servers/uponex/gisgp-mcp)

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

## Hosted QA tools (hosted endpoint only)

The hosted server at `https://gisgp.com/mcp` also exposes ArcGIS Online QA tools that
need a live AGOL connection and can't run in this local/stdio server:

| Tool | What it does |
|---|---|
| `grade_service` | Grade any FeatureServer A–F across 5 categories (schema, completeness, performance, maintenance, configuration) — returns a public shareable scorecard link |
| `audit_service` | One-call QA report: health + field schema + coded domains + summary counts |
| `find_layer_issues` | Scan a layer for problems: all-null fields, empty geometries, stale data, missing ObjectID, disabled query |
| `share_map` | Publish a GeoJSON FeatureCollection as a live shareable web map |
| `full_service_audit` | Merges grade_service + audit_service + find_layer_issues into a single call |
| `count_features` / `extract_domains` / `check_field_types` / `check_service_health` / `rest_explore` / `compare_schemas` / `query_features` / `query_statistics` | Inspect/query a live ArcGIS FeatureServer |

Also hosted-only: `get_terrain_profile`, `get_climate_history`, `classify_land_cover`,
`analyze_location` — free geo primitives backed by third-party open-data APIs
(Open-Meteo, ESA CCI Land Cover, OSM Nominatim), kept on the hosted endpoint rather
than this offline server since they need outbound internet access to those services.

### Layer 2 — paid tools (hosted endpoint only)

| Tool | What it does |
|---|---|
| `estimate_cost` | Free — quote a paid tool's credit cost without executing it |
| `check_wallet_balance` | Free — check your own MCP credits balance (needs an API key) |
| `assess_property_hazard` | Paid, 200 credits (€0.20) — multi-source US property hazard: USGS seismic risk + FEMA 10-year disaster history + NOAA active weather alerts, charged via credits wallet |
| `assess_property_hazard_x402` | Paid, ~$0.20 via the x402 protocol — same multi-source hazard data, paid directly in USDC on-chain (Base), no GISGP account needed |

These require live infrastructure (ArcGIS/S3, DynamoDB credits wallet, or third-party
APIs) and are not part of the open-source subset below — everything below runs fully
offline.

## Tools (32)

| Tool | Description |
|---|---|
| `convert_coordinates` | Convert coordinate pairs between EPSG coordinate systems |
| `validate_geojson` | Validate GeoJSON: RFC 7946 structure, topology, WGS84 ranges |
| `geojson_to_csv` | Convert a GeoJSON FeatureCollection to CSV |
| `shapefile_to_geojson` | Convert a Shapefile ZIP to GeoJSON |
| `kml_to_geojson` | Convert KML to GeoJSON |
| `gpx_to_geojson` | Convert GPX to GeoJSON |
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
| `buffer_geojson` | Buffer every feature by a distance in metres, geodesically accurate at any latitude |
| `dissolve_geojson` | Merge overlapping/adjacent geometries into one, optionally grouped by a property field |
| `centroids_geojson` | Replace each feature's geometry with its centroid Point |
| `convex_hull_geojson` | Smallest convex polygon containing all input features combined |
| `overlay_geojson` | Boolean set operation (intersection/difference/symmetric_difference/union) between two GeoJSON inputs |
| `spatial_join_geojson` | Attach properties from every matching feature in B to each feature in A |
| `nearest_features_geojson` | For each feature in A, find the nearest feature in B by geodesic distance |
| `fix_geometry` | Repair invalid geometries (self-intersections, bad rings) via GEOS make_valid |
| `geojson_diff` | Added/removed/changed features between two GeoJSON FeatureCollections |
| `epsg_suggest` | Suggest the correct UTM EPSG code for a lon/lat pair |
| `tile_math` | Convert lon/lat + zoom to Slippy Map (XYZ) tile x/y, bbox and Bing quadkey |
| `envelope_geojson` | Bounding-box rectangle (envelope) per feature |
| `minimum_rotated_rectangle` | Smallest-area rotated rectangle containing all input features combined |
| `voronoi_geojson` | Voronoi diagram of the input points |
| `geometry_health_report` | One-call health check: validate + auto-repair + stats + envelope, merged |

Docs: https://gisgp.com/api · Homepage: https://gisgp.com
