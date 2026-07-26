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
import functools
import io
import json
import math
import re
import zipfile
from xml.etree import ElementTree as ET

import gpxpy
import shapefile  # pyshp
import shapely
from mcp.server.fastmcp import FastMCP
from pyproj import Geod, Transformer
from shapely import wkt as shapely_wkt
from shapely.geometry import mapping, shape
from shapely.geometry.polygon import orient as shapely_orient

mcp = FastMCP("gisgp-mcp-oss")
GEOD = Geod(ellps="WGS84")


def _safe_shape(geom):
    """Like shapely.geometry.shape(), but via shapely.from_geojson (GEOS's own
    parser). On some numpy 2.x / shapely 2.0 builds, shape()'s vectorised
    Multi*() constructors raise `ufunc 'create_collection' not supported` for
    Multi* geometries — from_geojson reads through GEOS directly and avoids it."""
    return shapely.from_geojson(json.dumps(geom))


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
            shp = _safe_shape(geom)
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
        geoms = [_safe_shape(f["geometry"]) for f in data["features"] if f.get("geometry")]
    elif data.get("type") == "Feature":
        geoms = [_safe_shape(data["geometry"])]
    else:
        geoms = [_safe_shape(data)]
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
    _apply_to_geometries(data, lambda g: g.update(mapping(_safe_shape(g).simplify(tolerance, preserve_topology=True))))
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
    return shapely_wkt.dumps(_safe_shape(geom))


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
        row.append(shapely_wkt.dumps(_safe_shape(geom)) if geom else "")
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

    geom_type = _safe_shape(features[0]["geometry"]).geom_type
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
        geom = mapping(_safe_shape(sr.shape.__geo_interface__))
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


# ── geoprocessing (buffer / dissolve / centroids / hull) ─────────────────

def _fc(features):
    return {"type": "FeatureCollection", "features": features}


def _geo_features(geojson):
    data = json.loads(geojson) if isinstance(geojson, str) else geojson
    t = data.get("type")
    if t == "FeatureCollection":
        return data.get("features") or []
    if t == "Feature":
        return [data]
    return [{"type": "Feature", "properties": {}, "geometry": data}]


def _union_all(shapes):
    return functools.reduce(lambda a, b: a.union(b), shapes)


def _aeqd_transformers(lon, lat):
    aeqd = f"+proj=aeqd +lat_0={lat} +lon_0={lon} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    fwd = Transformer.from_crs("EPSG:4326", aeqd, always_xy=True).transform
    inv = Transformer.from_crs(aeqd, "EPSG:4326", always_xy=True).transform
    return fwd, inv


def _buffer_geojson(geojson, distance_m):
    from shapely.ops import transform
    if distance_m == 0:
        raise ValueError("distance_m must be non-zero")
    out = []
    for f in _geo_features(geojson):
        g = f.get("geometry")
        if not g:
            continue
        shp = _safe_shape(g)
        if shp.is_empty:
            continue
        c = shp.centroid
        fwd, inv = _aeqd_transformers(c.x, c.y)
        buffered = transform(fwd, shp).buffer(distance_m)
        if buffered.is_empty:
            continue
        out.append({"type": "Feature", "properties": f.get("properties", {}),
                    "geometry": mapping(transform(inv, buffered))})
    if not out:
        raise ValueError("buffer produced no geometry (empty input or over-shrunk)")
    return {"ok": True, "distance_m": distance_m, "geojson": _fc(out)}


@mcp.tool()
def buffer_geojson(geojson: str, distance_m: float) -> dict:
    """Buffer every feature by a distance in metres (negative shrinks), geodesically accurate at any latitude."""
    return _buffer_geojson(geojson, distance_m)


def _dissolve_geojson(geojson, by=None):
    groups = {}
    for f in _geo_features(geojson):
        g = f.get("geometry")
        if not g:
            continue
        shp = _safe_shape(g)
        if shp.is_empty:
            continue
        key = (f.get("properties") or {}).get(by) if by else None
        groups.setdefault(key, []).append(shp)
    if not groups:
        raise ValueError("no geometry to dissolve")
    out = []
    for key, shapes in groups.items():
        props = {by: key} if by else {}
        out.append({"type": "Feature", "properties": props, "geometry": mapping(_union_all(shapes))})
    return {"ok": True, "by": by, "group_count": len(out), "geojson": _fc(out)}


@mcp.tool()
def dissolve_geojson(geojson: str, by: str = "") -> dict:
    """Merge overlapping/adjacent geometries into one, optionally grouped by a property field."""
    return _dissolve_geojson(geojson, by or None)


def _centroids_geojson(geojson):
    out = []
    for f in _geo_features(geojson):
        g = f.get("geometry")
        if not g:
            continue
        c = _safe_shape(g).centroid
        if c.is_empty:
            continue
        out.append({"type": "Feature", "properties": f.get("properties", {}), "geometry": mapping(c)})
    if not out:
        raise ValueError("no geometry to compute centroids from")
    return {"ok": True, "geojson": _fc(out)}


@mcp.tool()
def centroids_geojson(geojson: str) -> dict:
    """Replace each feature's geometry with its centroid Point (properties kept)."""
    return _centroids_geojson(geojson)


def _convex_hull_geojson(geojson):
    shapes = [_safe_shape(f["geometry"]) for f in _geo_features(geojson) if f.get("geometry")]
    shapes = [s for s in shapes if not s.is_empty]
    if not shapes:
        raise ValueError("no geometry to hull")
    hull = _union_all(shapes).convex_hull
    return {"ok": True, "geojson": _fc([{"type": "Feature", "properties": {}, "geometry": mapping(hull)}])}


@mcp.tool()
def convex_hull_geojson(geojson: str) -> dict:
    """Smallest convex polygon containing all input features combined."""
    return _convex_hull_geojson(geojson)


# ── overlay / join / repair / tile-math ──────────────────────────────────

_OVERLAY_OPS = {"intersection", "difference", "symmetric_difference", "union"}


def _overlay_geojson(geojson_a, geojson_b, op):
    if op not in _OVERLAY_OPS:
        raise ValueError(f"op must be one of {sorted(_OVERLAY_OPS)}")
    shapes_a = [_safe_shape(f["geometry"]) for f in _geo_features(geojson_a) if f.get("geometry")]
    shapes_b = [_safe_shape(f["geometry"]) for f in _geo_features(geojson_b) if f.get("geometry")]
    if not shapes_a or not shapes_b:
        raise ValueError("both inputs need at least one geometry")
    result = getattr(_union_all(shapes_a), op)(_union_all(shapes_b))
    feats = [] if result.is_empty else [{"type": "Feature", "properties": {}, "geometry": mapping(result)}]
    return {"ok": True, "op": op, "geojson": _fc(feats)}


@mcp.tool()
def overlay_geojson(geojson_a: str, geojson_b: str, op: str = "intersection") -> dict:
    """Boolean set operation (intersection/difference/symmetric_difference/union) between two GeoJSON inputs."""
    return _overlay_geojson(geojson_a, geojson_b, op)


_JOIN_PREDICATES = {"intersects", "within", "contains", "touches", "crosses", "overlaps"}


def _spatial_join_geojson(geojson_a, geojson_b, predicate):
    if predicate not in _JOIN_PREDICATES:
        raise ValueError(f"predicate must be one of {sorted(_JOIN_PREDICATES)}")
    shapes_b = [(_safe_shape(f["geometry"]), f.get("properties", {}))
                for f in _geo_features(geojson_b) if f.get("geometry")]
    out = []
    matched = 0
    for fa in _geo_features(geojson_a):
        g = fa.get("geometry")
        props = dict(fa.get("properties", {}))
        matches = []
        if g:
            shp_a = _safe_shape(g)
            matches = [pb for sb, pb in shapes_b if getattr(shp_a, predicate)(sb)]
        props["_matches"] = matches
        matched += len(matches)
        out.append({"type": "Feature", "properties": props, "geometry": g})
    return {"ok": True, "predicate": predicate, "match_count": matched, "geojson": _fc(out)}


@mcp.tool()
def spatial_join_geojson(geojson_a: str, geojson_b: str, predicate: str = "intersects") -> dict:
    """Attach properties from every matching B feature to each A feature (as a _matches list)."""
    return _spatial_join_geojson(geojson_a, geojson_b, predicate)


def _nearest_features_geojson(geojson_a, geojson_b):
    centroids_b = []
    for f in _geo_features(geojson_b):
        g = f.get("geometry")
        if not g:
            continue
        c = _safe_shape(g).centroid
        centroids_b.append((c.x, c.y, f.get("properties", {})))
    if not centroids_b:
        raise ValueError("geojson_b has no geometry")
    out = []
    for fa in _geo_features(geojson_a):
        g = fa.get("geometry")
        props = dict(fa.get("properties", {}))
        if g:
            ca = _safe_shape(g).centroid
            best = min(centroids_b, key=lambda b: GEOD.inv(ca.x, ca.y, b[0], b[1])[2])
            _, _, dist_m = GEOD.inv(ca.x, ca.y, best[0], best[1])
            props["_nearest"] = best[2]
            props["_nearest_distance_m"] = round(dist_m, 2)
        out.append({"type": "Feature", "properties": props, "geometry": g})
    return {"ok": True, "geojson": _fc(out)}


@mcp.tool()
def nearest_features_geojson(geojson_a: str, geojson_b: str) -> dict:
    """For each feature in A, find the nearest feature in B by centroid-to-centroid geodesic distance."""
    return _nearest_features_geojson(geojson_a, geojson_b)


def _fix_geometry(geojson):
    out = []
    fixed_count = 0
    for f in _geo_features(geojson):
        g = f.get("geometry")
        if not g:
            out.append(f)
            continue
        shp = _safe_shape(g)
        if not shp.is_valid:
            shp = shapely.make_valid(shp)
            fixed_count += 1
        out.append({"type": "Feature", "properties": f.get("properties", {}), "geometry": mapping(shp)})
    return {"ok": True, "fixed_count": fixed_count, "feature_count": len(_geo_features(geojson)), "geojson": _fc(out)}


@mcp.tool()
def fix_geometry(geojson: str) -> dict:
    """Repair invalid geometries (self-intersections, bad rings) via GEOS make_valid."""
    return _fix_geometry(geojson)


def _geojson_diff(geojson_a, geojson_b, id_field):
    feats_a, feats_b = _geo_features(geojson_a), _geo_features(geojson_b)

    def key(f, i):
        return (f.get("properties") or {}).get(id_field) if id_field else i

    keyed_a = {key(f, i): f for i, f in enumerate(feats_a)}
    keyed_b = {key(f, i): f for i, f in enumerate(feats_b)}
    added = [keyed_b[k] for k in keyed_b.keys() - keyed_a.keys()]
    removed = [keyed_a[k] for k in keyed_a.keys() - keyed_b.keys()]
    changed, unchanged = [], 0
    for k in keyed_a.keys() & keyed_b.keys():
        fa, fb = keyed_a[k], keyed_b[k]
        pa, pb = fa.get("properties") or {}, fb.get("properties") or {}
        geom_changed = fa.get("geometry") != fb.get("geometry")
        diffs = {p: [pa.get(p), pb.get(p)] for p in set(pa) | set(pb) if pa.get(p) != pb.get(p)}
        if geom_changed or diffs:
            changed.append({"id": k, "geometry_changed": geom_changed, "property_diffs": diffs})
        else:
            unchanged += 1
    return {"ok": True, "added_count": len(added), "removed_count": len(removed),
            "changed_count": len(changed), "unchanged_count": unchanged,
            "added": added, "removed": removed, "changed": changed}


@mcp.tool()
def geojson_diff(geojson_a: str, geojson_b: str, id_field: str = "") -> dict:
    """Added/removed/changed features between two GeoJSON FeatureCollections (exact structural diff)."""
    return _geojson_diff(geojson_a, geojson_b, id_field)


def _epsg_suggest(lon, lat):
    if not (-180 <= lon <= 180) or not (-90 <= lat <= 90):
        raise ValueError("lon must be in [-180,180], lat in [-90,90]")
    zone = int((lon + 180) // 6) + 1
    north = lat >= 0
    epsg = (32600 if north else 32700) + zone
    warning = "outside UTM valid latitude range (use polar stereographic instead)" if lat > 84 or lat < -80 else None
    return {"ok": True, "epsg": epsg, "utm_zone": zone, "hemisphere": "north" if north else "south",
            "name": f"WGS 84 / UTM zone {zone}{'N' if north else 'S'}", "warning": warning}


@mcp.tool()
def epsg_suggest(lon: float, lat: float) -> dict:
    """Suggest the correct UTM EPSG code for a WGS84 lon/lat pair, for accurate metric operations."""
    return _epsg_suggest(lon, lat)


def _tile_math(lon, lat, zoom):
    if not (-180 <= lon <= 180) or not (-85.0511 <= lat <= 85.0511):
        raise ValueError("lon must be in [-180,180], lat in [-85.0511,85.0511]")
    if not (0 <= zoom <= 22):
        raise ValueError("zoom must be 0-22")
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)

    def lon_at(xx):
        return xx / n * 360.0 - 180.0

    def lat_at(yy):
        m = math.pi * (1 - 2 * yy / n)
        return math.degrees(math.atan(math.sinh(m)))

    tile_bbox = [lon_at(x), lat_at(y + 1), lon_at(x + 1), lat_at(y)]
    quadkey = "".join(str((((y >> (zoom - i - 1)) & 1) << 1) + ((x >> (zoom - i - 1)) & 1)) for i in range(zoom))
    return {"ok": True, "zoom": zoom, "x": x, "y": y, "tile_bbox": tile_bbox, "quadkey": quadkey}


@mcp.tool()
def tile_math(lon: float, lat: float, zoom: int) -> dict:
    """Convert lon/lat + zoom to Slippy Map (XYZ) tile x/y, that tile's bbox, and its Bing quadkey."""
    return _tile_math(lon, lat, zoom)


def _envelope_geojson(geojson):
    out = []
    for f in _geo_features(geojson):
        g = f.get("geometry")
        if not g:
            continue
        out.append({"type": "Feature", "properties": f.get("properties", {}),
                    "geometry": mapping(_safe_shape(g).envelope)})
    if not out:
        raise ValueError("no geometry to envelope")
    return {"ok": True, "geojson": _fc(out)}


@mcp.tool()
def envelope_geojson(geojson: str) -> dict:
    """Bounding-box rectangle (envelope) per feature, properties kept."""
    return _envelope_geojson(geojson)


def _minimum_rotated_rectangle(geojson):
    shapes = [_safe_shape(f["geometry"]) for f in _geo_features(geojson) if f.get("geometry")]
    if not shapes:
        raise ValueError("no geometry to rectangle")
    mrr = _union_all(shapes).minimum_rotated_rectangle
    return {"ok": True, "geojson": _fc([{"type": "Feature", "properties": {}, "geometry": mapping(mrr)}])}


@mcp.tool()
def minimum_rotated_rectangle(geojson: str) -> dict:
    """Smallest-area rotated rectangle containing all input features combined."""
    return _minimum_rotated_rectangle(geojson)


def _voronoi_geojson(geojson):
    points = []
    for f in _geo_features(geojson):
        g = f.get("geometry")
        if not g:
            continue
        shp = _safe_shape(g)
        points.append(shp if shp.geom_type == "Point" else shp.centroid)
    if len(points) < 2:
        raise ValueError("need at least 2 points for a Voronoi diagram")
    multipoint = shapely_wkt.loads("MULTIPOINT (" + ", ".join(f"{p.x} {p.y}" for p in points) + ")")
    cells = shapely.voronoi_polygons(multipoint)
    out = [{"type": "Feature", "properties": {}, "geometry": mapping(c)} for c in cells.geoms]
    return {"ok": True, "cell_count": len(out), "geojson": _fc(out)}


@mcp.tool()
def voronoi_geojson(geojson: str) -> dict:
    """Voronoi diagram of the input points (non-point features use their centroid)."""
    return _voronoi_geojson(geojson)


# ── composite one-call report ─────────────────────────────────────────────

@mcp.tool()
def geometry_health_report(geojson: str) -> dict:
    """One-call GeoJSON health check: validate + auto-repair invalid geometry + stats + bbox envelope."""
    report = _validate_geojson(geojson)
    fix_out = _fix_geometry(geojson)
    fixed_geojson = fix_out["geojson"]
    return {
        "ok": True,
        "valid": report.get("valid"),
        "errors": report.get("errors"),
        "warnings": report.get("warnings"),
        "fixed_count": fix_out.get("fixed_count"),
        "stats": _geometry_stats(json.dumps(fixed_geojson)),
        "envelope": _envelope_geojson(fixed_geojson).get("geojson"),
        "geojson": fixed_geojson,
    }


if __name__ == "__main__":
    mcp.run()
