# GISGP MCP Server

Free remote MCP (Model Context Protocol) server for GIS/ArcGIS Online automation.

**Endpoint:** `https://gisgp.com/mcp` (Streamable HTTP, stateless, no auth for public layers)

## Client config

```json
{ "mcpServers": { "gisgp": { "url": "https://gisgp.com/mcp" } } }
```

## Tools (12)

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

Docs: https://gisgp.com/api · Homepage: https://gisgp.com
