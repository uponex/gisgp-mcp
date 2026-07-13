#!/usr/bin/env python3
"""
GISGP MCP Server — open-source subset.

Self-contained implementations of the format-conversion and geometry tools
that need no external service access (no ArcGIS Online credentials, no
network calls). This runs entirely locally over stdio.

Tools that inspect/query a live ArcGIS Online FeatureServer (count_features,
extract_domains, check_field_types, check_service_health, rest_explore,
compare_schemas, query_features, query_statistics) require a real AGOL
connection and are only available on the hosted remote endpoint at
https://gisgp.com/mcp — see README.md.
"""

import base64
import csv
import io
import json
import re
import zipfile
from xml.etree import ElementTree as ET

import gpxpy
import shapefile  # pyshp
from mcp.server.fastmcp import FastMCP
from pyproj import Geod, Transformer
from shapely import wkt as shapely_wkt
from shapely.geometry import mapping, shape
from shapely.geometry.polygon import orient as shapely_orient

mcp = FastMCP("gisgp-mcp-oss")
GEOD = Geod(ellps="WGS84")


# ── coordinate / CRS ──────────────────────────────────────────────────────

def _convert_coordinates(points, from_epsg, to_epsg):
    if len(points) > 1000:
        raise ValueError("max 1000 points")
    transformer = Transformer.from_crs(f"EPSG:{from_epsg}", f"EPSG:{to_epsg}", always_xy=True)
    converted = [list(transformer.transform(x, y)) for x, y in points]
    return {"points": converted, "from_epsg": from_epsg, "to_epsg": to_epsg}


@mcp.tool()
def convert_coordinates(points: list[list[float]], from_epsg: int, to_epsg: int) -> dict:
    """Convert coordinate pairs between EPSG coordinate systems (max 1000 points)."""
    return _convert_coordinates(points, from_epsg, to_epsg)


def _walk_coords(coords, fn):
    if isinstance(coords[0], (int, float)):
        return fn(coords)
    return [_walk_coords(c, fn) for c in coords]


def _apply_to_geometries(data, fn):
    if data.get("type") == "FeatureCollection":
        for f in data["features"]:
            if f.get("geometry"):
                fn(f["geometry"])
    elif data.get("type") == "Feature":
        if data.get("geometry"):
            fn(data["geometry"])
    else:
        fn(data)
    return data


def _reproject_geojson(geojson, from_epsg, to_epsg):
    transformer = Transformer.from_crs(f"EPSG:{from_epsg}", f"EPSG:{to_epsg}", always_xy=True)

    def reproject(coords):
        x, y = transformer.transform(coords[0], coords[1])
        return [x, y] + list(coords[2:])

    data = json.loads(geojson)
    _apply_to_geometries(data, lambda g: g.update(coordinates=_walk_coords(g["coordinates"], reproject)))
    return json.dumps(data)


@mcp.tool()
def reproject_geojson(geojson: str, from_epsg: int, to_epsg: int) -> str:
    """Reproject an entire GeoJSON between EPSG coordinate systems (all geometry types)."""
    return _reproject_geojson(geojson, from_epsg, to_epsg)


def _reduce_precision(geojson, decimals):
    data = json.loads(geojson)
    _apply_to_geometries(
        data,
        lambda g: g.update(coordinates=_walk_coords(g["coordinates"], lambda c: [round(v, decimals) for v in c])),
    )
    return json.dumps(data)


@mcp.tool()
def reduce_precision(geojson: str, decimals: int = 6) -> str:
    """Round every coordinate to N decimal places - shrinks GeoJSON payload size."""
    return _reduce_precision(geojson, decimals)


# ── geometry ops ──────────────────────────────────────────────────────────

def _validate_geojson(geojson):
    errors, warnings = [], []
    try:
        data = json.loads(geojson)
    except json.JSONDecodeError as e:
        return {"valid": False, "errors": [f"Invalid JSON: {e}"], "warnings": [], "stats": {}}

    features = data.get("features", []) if data.get("type") == "FeatureCollection" else []
    if data.get("type") != "FeatureCollection":
        errors.append("Root type must be 'FeatureCollection'")

    geom_counts = {}
    for i, feat in enumerate(features):
        if feat.get("type") != "Feature":
            errors.append(f"Feature {i}: type must be 'Feature'")
            continue
        geom = feat.get("geometry")
        if geom is None:
            warnings.append(f"Feature {i}: null geometry")
            continue
        try:
            shp = shape(geom)
            geom_counts[shp.geom_type] = geom_counts.get(shp.geom_type, 0) + 1
            if not shp.is_valid:
                errors.append(f"Feature {i}: invalid/self-intersecting geometry ({shp.geom_type})")
            minx, miny, maxx, maxy = shp.bounds
            if not (-180 <= minx <= 180 and -180 <= maxx <= 180 and -90 <= miny <= 90 and -90 <= maxy <= 90):
                warnings.append(f"Feature {i}: coordinates outside WGS84 range")
        except Exception as e:
            errors.append(f"Feature {i}: {e}")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "stats": {"feature_count": len(features), "geometry_types": geom_counts},
    }


@mcp.tool()
def validate_geojson(geojson: str) -> dict:
    """Validate GeoJSON (RFC 7946 structure, topology, WGS84 coordinate ranges)."""
    return _validate_geojson(geojson)


def _count_vertices(g):
    # NOTE: don't use hasattr(g, "coords") for this — shapely's Polygon.coords
    # property raises NotImplementedError (not AttributeError) when accessed,
    # which hasattr() does not catch, so it propagates and crashes the caller.
    if g.geom_type == "Polygon":
        return len(list(g.exterior.coords)) + sum(len(list(r.coords)) for r in g.interiors)
    if g.geom_type in ("MultiPolygon", "MultiPoint", "MultiLineString", "GeometryCollection"):
        return sum(_count_vertices(part) for part in g.geoms)
    return len(list(g.coords))  # Point, LineString, LinearRing


def _geometry_stats(geojson):
    data = json.loads(geojson)
    if data.get("type") == "FeatureCollection":
        geoms = [shape(f["geometry"]) for f in data["features"] if f.get("geometry")]
    elif data.get("type") == "Feature":
        geoms = [shape(data["geometry"])]
    else:
        geoms = [shape(data)]
    if not geoms:
        raise ValueError("No geometry found")

    total_area_m2 = 0.0
    total_length_m = 0.0
    vertex_count = 0
    for g in geoms:
        if g.geom_type in ("Polygon", "MultiPolygon"):
            area, perimeter = GEOD.geometry_area_perimeter(g)
            total_area_m2 += abs(area)
            total_length_m += abs(perimeter)
        elif g.geom_type in ("LineString", "MultiLineString"):
            total_length_m += abs(GEOD.geometry_length(g))
        vertex_count += _count_vertices(g)

    union = geoms[0]
    for g in geoms[1:]:
        union = union.union(g)
    centroid = union.centroid
    minx, miny, maxx, maxy = union.bounds

    return {
        "area_m2": total_area_m2,
        "area_km2": total_area_m2 / 1_000_000,
        "length_m": total_length_m,
        "length_km": total_length_m / 1000,
        "vertex_count": vertex_count,
        "centroid": [centroid.x, centroid.y],
        "bbox": [minx, miny, maxx, maxy],
    }


@mcp.tool()
def geometry_stats(geojson: str) -> dict:
    """Compute area (m2/km2), length (m/km), vertex count, centroid and bbox of GeoJSON."""
    return _geometry_stats(geojson)


def _simplify_geometry(geojson, tolerance):
    data = json.loads(geojson)
    _apply_to_geometries(data, lambda g: g.update(mapping(shape(g).simplify(tolerance, preserve_topology=True))))
    return json.dumps(data)


@mcp.tool()
def simplify_geometry(geojson: str, tolerance: float = 0.001) -> str:
    """Simplify GeoJSON geometry (Douglas-Peucker, tolerance in degrees). Lower = more detail."""
    return _simplify_geometry(geojson, tolerance)


def _wkt_to_geojson(wkt):
    return mapping(shapely_wkt.loads(wkt))


@mcp.tool()
def wkt_to_geojson(wkt: str) -> dict:
    """Convert a WKT geometry string to a GeoJSON geometry (e.g. from PostGIS)."""
    return _wkt_to_geojson(wkt)


def _geojson_to_wkt(geojson):
    data = json.loads(geojson)
    geom = data["geometry"] if data.get("type") == "Feature" else data
    return shapely_wkt.dumps(shape(geom))


@mcp.tool()
def geojson_to_wkt(geojson: str) -> str:
    """Convert a GeoJSON geometry (or a Feature's geometry) to a WKT string."""
    return _geojson_to_wkt(geojson)


# ── CSV ───────────────────────────────────────────────────────────────────

def _geojson_to_csv(geojson):
    data = json.loads(geojson)
    features = data.get("features", [])
    prop_keys = []
    for f in features:
        for k in (f.get("properties") or {}).keys():
            if k not in prop_keys:
                prop_keys.append(k)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(prop_keys + ["geometry"])
    for f in features:
        props = f.get("properties") or {}
        row = [props.get(k, "") for k in prop_keys]
        geom = f.get("geometry")
        row.append(shapely_wkt.dumps(shape(geom)) if geom else "")
        writer.writerow(row)
    return buf.getvalue()


@mcp.tool()
def geojson_to_csv(geojson: str) -> str:
    """Convert a GeoJSON FeatureCollection to CSV (feature properties + geometry column)."""
    return _geojson_to_csv(geojson)


def _csv_to_geojson(csv_text):
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    headers = reader.fieldnames or []
    lat_col = next((h for h in headers if h.lower() in ("lat", "latitude", "y")), None)
    lon_col = next((h for h in headers if h.lower() in ("lon", "lng", "longitude", "x")), None)
    if not rows or not lat_col or not lon_col:
        return json.dumps({"type": "FeatureCollection", "features": []})

    features = []
    for row in rows:
        try:
            lon, lat = float(row[lon_col]), float(row[lat_col])
        except (ValueError, TypeError):
            continue
        props = {k: v for k, v in row.items() if k not in (lat_col, lon_col)}
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props,
        })
    return json.dumps({"type": "FeatureCollection", "features": features})


@mcp.tool()
def csv_to_geojson(csv: str) -> str:
    """Convert CSV with coordinate columns (auto-detected) to a GeoJSON FeatureCollection."""
    return _csv_to_geojson(csv)


# ── KML ───────────────────────────────────────────────────────────────────

KML_NS = "http://www.opengis.net/kml/2.2"


def _coords_to_kml(coords):
    return " ".join(",".join(str(v) for v in c) for c in coords)


def _geom_to_kml_element(geom):
    t, coords = geom["type"], geom["coordinates"]
    if t == "Point":
        return f"<Point><coordinates>{','.join(str(v) for v in coords)}</coordinates></Point>"
    if t == "LineString":
        return f"<LineString><coordinates>{_coords_to_kml(coords)}</coordinates></LineString>"
    if t == "Polygon":
        outer, holes = coords[0], coords[1:]
        inner_xml = "".join(
            f"<innerBoundaryIs><LinearRing><coordinates>{_coords_to_kml(h)}</coordinates></LinearRing></innerBoundaryIs>"
            for h in holes
        )
        return (
            f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{_coords_to_kml(outer)}"
            f"</coordinates></LinearRing></outerBoundaryIs>{inner_xml}</Polygon>"
        )
    if t == "MultiPoint":
        return "<MultiGeometry>" + "".join(
            f"<Point><coordinates>{','.join(str(v) for v in c)}</coordinates></Point>" for c in coords
        ) + "</MultiGeometry>"
    if t == "MultiLineString":
        return "<MultiGeometry>" + "".join(
            f"<LineString><coordinates>{_coords_to_kml(c)}</coordinates></LineString>" for c in coords
        ) + "</MultiGeometry>"
    if t == "MultiPolygon":
        return "<MultiGeometry>" + "".join(
            _geom_to_kml_element({"type": "Polygon", "coordinates": c}) for c in coords
        ) + "</MultiGeometry>"
    raise ValueError(f"Unsupported geometry type: {t}")


def _geojson_to_kml(geojson):
    data = json.loads(geojson)
    features = data["features"] if data.get("type") == "FeatureCollection" else [data]
    placemarks = []
    for f in features:
        geom = f.get("geometry")
        if not geom:
            continue
        props = f.get("properties") or {}
        extended = "".join(f'<Data name="{k}"><value>{v}</value></Data>' for k, v in props.items())
        placemarks.append(
            f"<Placemark><name>{props.get('name', '')}</name>"
            f"<ExtendedData>{extended}</ExtendedData>{_geom_to_kml_element(geom)}</Placemark>"
        )
    body = "".join(placemarks)
    return f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="{KML_NS}"><Document>{body}</Document></kml>'


@mcp.tool()
def geojson_to_kml(geojson: str) -> str:
    """Convert a GeoJSON FeatureCollection to KML."""
    return _geojson_to_kml(geojson)


def _kml_coords_to_list(text):
    out = []
    for chunk in text.strip().split():
        parts = chunk.split(",")
        out.append([float(p) for p in parts])
    return out


def _parse_kml_geometry(elem, ns):
    tag = elem.tag.split("}")[-1]
    if tag == "Point":
        return {"type": "Point", "coordinates": _kml_coords_to_list(elem.find(f"{ns}coordinates").text)[0]}
    if tag == "LineString":
        return {"type": "LineString", "coordinates": _kml_coords_to_list(elem.find(f"{ns}coordinates").text)}
    if tag == "Polygon":
        outer_el = elem.find(f"{ns}outerBoundaryIs/{ns}LinearRing/{ns}coordinates")
        rings = [_kml_coords_to_list(outer_el.text)]
        for inner in elem.findall(f"{ns}innerBoundaryIs/{ns}LinearRing/{ns}coordinates"):
            rings.append(_kml_coords_to_list(inner.text))
        return {"type": "Polygon", "coordinates": rings}
    if tag == "MultiGeometry":
        geoms = [_parse_kml_geometry(child, ns) for child in elem]
        types = {g["type"] for g in geoms}
        if types == {"Point"}:
            return {"type": "MultiPoint", "coordinates": [g["coordinates"] for g in geoms]}
        if types == {"LineString"}:
            return {"type": "MultiLineString", "coordinates": [g["coordinates"] for g in geoms]}
        if types == {"Polygon"}:
            return {"type": "MultiPolygon", "coordinates": [g["coordinates"] for g in geoms]}
        return {"type": "GeometryCollection", "geometries": geoms}
    raise ValueError(f"Unsupported KML geometry: {tag}")


def _kml_to_geojson(kml):
    root = ET.fromstring(kml)
    m = re.match(r"\{(.*)\}", root.tag)
    ns = f"{{{m.group(1)}}}" if m else ""
    features = []
    for pm in root.iter(f"{ns}Placemark"):
        name_el = pm.find(f"{ns}name")
        props = {"name": name_el.text} if name_el is not None else {}
        for data_el in pm.findall(f"{ns}ExtendedData/{ns}Data"):
            key = data_el.get("name")
            val_el = data_el.find(f"{ns}value")
            if key:
                props[key] = val_el.text if val_el is not None else None
        geom = None
        for child in pm:
            tag = child.tag.split("}")[-1]
            if tag in ("Point", "LineString", "Polygon", "MultiGeometry"):
                geom = _parse_kml_geometry(child, ns)
                break
        if geom:
            features.append({"type": "Feature", "geometry": geom, "properties": props})
    return json.dumps({"type": "FeatureCollection", "features": features})


@mcp.tool()
def kml_to_geojson(kml: str) -> str:
    """Convert KML (2.0-2.2) to a GeoJSON FeatureCollection."""
    return _kml_to_geojson(kml)


# ── GPX ───────────────────────────────────────────────────────────────────

def _gpx_to_geojson(gpx):
    g = gpxpy.parse(gpx)
    features = []
    for wp in g.waypoints:
        coords = [wp.longitude, wp.latitude] + ([wp.elevation] if wp.elevation is not None else [])
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": coords},
                          "properties": {"name": wp.name or ""}})
    for track in g.tracks:
        for seg in track.segments:
            coords = [[p.longitude, p.latitude] + ([p.elevation] if p.elevation is not None else []) for p in seg.points]
            if coords:
                features.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
                                  "properties": {"name": track.name or ""}})
    for route in g.routes:
        coords = [[p.longitude, p.latitude] for p in route.points]
        if coords:
            features.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
                              "properties": {"name": route.name or "", "kind": "route"}})
    return json.dumps({"type": "FeatureCollection", "features": features})


@mcp.tool()
def gpx_to_geojson(gpx: str) -> str:
    """Convert GPX (waypoints, tracks, routes) to a GeoJSON FeatureCollection."""
    return _gpx_to_geojson(gpx)


@mcp.tool()
def gpx_to_kml(gpx: str) -> str:
    """Convert GPX (waypoints, tracks, routes) to KML."""
    return _geojson_to_kml(_gpx_to_geojson(gpx))


# ── Shapefile ─────────────────────────────────────────────────────────────

_SHP_TYPE_MAP = {
    "Point": shapefile.POINT, "MultiPoint": shapefile.MULTIPOINT,
    "LineString": shapefile.POLYLINE, "MultiLineString": shapefile.POLYLINE,
    "Polygon": shapefile.POLYGON, "MultiPolygon": shapefile.POLYGON,
}


def _shapefile_wind(ring_coords):
    """Reorient a GeoJSON polygon's rings (RFC 7946: CCW exterior, CW holes)
    to the Shapefile convention (CW exterior, CCW holes) — writing GeoJSON
    winding directly into a shapefile produces holes misread as exterior
    rings on read-back (same class of bug as GISGP's main app KML/GeoJSON
    import, see pitfalls_coordinate_crs)."""
    poly = shapely_orient(shape({"type": "Polygon", "coordinates": ring_coords}), sign=-1.0)
    return [list(poly.exterior.coords)] + [list(r.coords) for r in poly.interiors]


def _geojson_to_shapefile(geojson):
    data = json.loads(geojson)
    features = data.get("features", [])
    if not features:
        raise ValueError("No features to convert")

    geom_type = shape(features[0]["geometry"]).geom_type
    shp_type = _SHP_TYPE_MAP.get(geom_type, shapefile.NULL)

    buf_shp, buf_shx, buf_dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    writer = shapefile.Writer(shp=buf_shp, shx=buf_shx, dbf=buf_dbf, shapeType=shp_type)
    writer.autoBalance = 1

    prop_keys = []
    for f in features:
        for k in (f.get("properties") or {}).keys():
            if k not in prop_keys:
                prop_keys.append(k)
    if not prop_keys:
        prop_keys = ["id"]
    for k in prop_keys:
        writer.field(k[:10], "C", size=254)

    warnings = []
    for i, f in enumerate(features):
        geom = f["geometry"]
        gtype, coords = geom["type"], geom["coordinates"]
        try:
            if gtype == "Point":
                writer.point(*coords[:2])
            elif gtype == "MultiPoint":
                writer.multipoint(coords)
            elif gtype == "LineString":
                writer.line([coords])
            elif gtype == "MultiLineString":
                writer.line(coords)
            elif gtype == "Polygon":
                writer.poly(_shapefile_wind(coords))
            elif gtype == "MultiPolygon":
                writer.poly([ring for poly_coords in coords for ring in _shapefile_wind(poly_coords)])
            else:
                warnings.append(f"Feature {i}: unsupported geometry type {gtype}, skipped")
                continue
        except Exception as e:
            warnings.append(f"Feature {i}: {e}")
            continue
        props = f.get("properties") or {}
        writer.record(*[str(props.get(k, "")) for k in prop_keys])

    writer.close()
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("output.shp", buf_shp.getvalue())
        zf.writestr("output.shx", buf_shx.getvalue())
        zf.writestr("output.dbf", buf_dbf.getvalue())
    return {"shapefile_zip_base64": base64.b64encode(zip_buf.getvalue()).decode(), "warnings": warnings}


@mcp.tool()
def geojson_to_shapefile(geojson: str) -> dict:
    """Convert a GeoJSON FeatureCollection to a Shapefile ZIP (base64-encoded)."""
    return _geojson_to_shapefile(geojson)


def _shapefile_to_geojson(shapefile_zip_base64):
    zip_bytes = base64.b64decode(shapefile_zip_base64)
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    shp_name = next(n for n in zf.namelist() if n.lower().endswith(".shp"))
    base = shp_name[:-4]
    shp = io.BytesIO(zf.read(base + ".shp"))
    shx_name, dbf_name, prj_name = base + ".shx", base + ".dbf", base + ".prj"
    shx = io.BytesIO(zf.read(shx_name)) if shx_name in zf.namelist() else None
    dbf = io.BytesIO(zf.read(dbf_name)) if dbf_name in zf.namelist() else None

    reader = shapefile.Reader(shp=shp, shx=shx, dbf=dbf)
    features = []
    for sr in reader.shapeRecords():
        geom = mapping(shape(sr.shape.__geo_interface__))
        features.append({"type": "Feature", "geometry": geom, "properties": sr.record.as_dict()})

    notice = None
    if prj_name in zf.namelist():
        notice = "Shapefile has a .prj — this OSS build does not auto-reproject; assumed already WGS84."
    return {"geojson": {"type": "FeatureCollection", "features": features}, "reprojection_notice": notice}


@mcp.tool()
def shapefile_to_geojson(shapefile_zip_base64: str) -> dict:
    """Convert a Shapefile ZIP (base64-encoded .zip with .shp/.dbf/.shx[/.prj]) to a GeoJSON FeatureCollection."""
    return _shapefile_to_geojson(shapefile_zip_base64)


@mcp.tool()
def kml_to_shapefile(kml: str) -> dict:
    """Convert KML to a Shapefile ZIP (base64-encoded)."""
    result = _geojson_to_shapefile(_kml_to_geojson(kml))
    return {"shapefile_zip_base64": result["shapefile_zip_base64"]}


if __name__ == "__main__":
    mcp.run()
