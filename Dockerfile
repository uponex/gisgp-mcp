# GISGP MCP Server — open-source subset (server.py), runs locally over stdio.
# Implements the non-AGOL-dependent tools (coordinate/CRS conversion, GeoJSON
# validate/stats/simplify, CSV/KML/GPX/Shapefile/WKT conversion). AGOL-dependent
# tools (feature queries, service inspection) remain on the hosted remote
# endpoint at https://gisgp.com/mcp — see README.md.

FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .

CMD ["python", "server.py"]
