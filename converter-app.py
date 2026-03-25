import streamlit as st
import simplekml
import folium
import re
import geopandas as gpd
from rasterstats import zonal_stats
from streamlit.components.v1 import html as st_html
import tempfile
import os
import json
from xml.etree import ElementTree as ET
import zipfile
from io import BytesIO
from shapely.geometry import Polygon, MultiPolygon, mapping, shape
from shapely.ops import unary_union

st.set_page_config(page_title="Polygon Generator and Population Estimate", layout="centered")

# ---------------------------
# Session state initialization
# ---------------------------
for key, default in {
    "coords": [],              # stored internally as [(lat, lon), ...]
    "coord_trigger": False,
    "upload_trigger": False,
    "last_input_mode": None,
    "generate_done": False,
    "map_key": 0,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# ---------------------------
# Page header
# ---------------------------
st.markdown("<h2>Polygon Generator and Population Estimate</h2>", unsafe_allow_html=True)
st.markdown(
    "<p style='font-size: 0.9rem; color: grey;'>"
    "Upload spatial data files or enter coordinates manually to visualize geographic areas "
    "on an interactive map. Define custom polygons and generate population estimates using LandScan Global 2024 data from ORNL."
    "</p>",
    unsafe_allow_html=True
)

# ---------------------------
# Helper functions
# ---------------------------
def dm_to_dd(dm):
    deg = int(dm // 100)
    mins = dm % 100
    return round(deg + mins / 60, 6)

def dms_to_dd(d, m, s, direction):
    dd = d + m / 60 + s / 3600
    if direction in ["S", "W"]:
        dd *= -1
    return round(dd, 6)

def close_ring(coords):
    if coords and coords[0] != coords[-1]:
        coords.append(coords[0])
    return coords

def latlon_to_lonlat(coords):
    return [(lon, lat) for lat, lon in coords]

def lonlat_to_latlon(coords):
    return [(lat, lon) for lon, lat in coords]

def parse_nws_latlon(text):
    """
    Parse NOAA/NWS style polygon text such as:
    LAT...LON 1839 6620 1842 6619 ...
    Returns [[(lat, lon), ...]]
    """
    nums = re.findall(r"\b\d{4}\b", text)
    if len(nums) < 6 or len(nums) % 2 != 0:
        return []

    coords = []
    for i in range(0, len(nums), 2):
        lat_dm = int(nums[i])
        lon_dm = int(nums[i + 1])

        lat_deg = lat_dm // 100
        lat_min = lat_dm % 100
        lon_deg = lon_dm // 100
        lon_min = lon_dm % 100

        if lat_min >= 60 or lon_min >= 60:
            return []

        lat = round(lat_deg + lat_min / 60.0, 6)
        lon = round(-(lon_deg + lon_min / 60.0), 6)  # assume western hemisphere

        coords.append((lat, lon))

    return [close_ring(coords)]

def parse_coords(text):
    text = text.strip()

    # 1) Explicit NOAA/NWS LAT...LON parser
    if "LAT...LON" in text.upper():
        parsed = parse_nws_latlon(text)
        if parsed:
            return parsed

    normalized = text.replace(",", " ").replace(";", " ")

    # 2) DMS parser
    dms = re.findall(r"(\d+)[°:\s](\d+)[′'\s:](\d+)[″\"\s]?([NSEW])", normalized.upper())
    if len(dms) >= 2:
        try:
            coords = []
            for i in range(0, len(dms) - 1, 2):
                ld, lm, ls, ldir = dms[i]
                od, om, os, odir = dms[i + 1]
                lat = dms_to_dd(int(ld), int(lm), int(ls), ldir)
                lon = dms_to_dd(int(od), int(om), int(os), odir)
                coords.append((lat, lon))
            return [close_ring(coords)]
        except Exception:
            pass

    # 3) Decimal degrees parser
    floats = re.findall(r"[-+]?\d+(?:\.\d+)?", normalized)
    try:
        nums = list(map(float, floats))
        if len(nums) >= 6 and len(nums) % 2 == 0:
            coords = []
            for i in range(0, len(nums), 2):
                coords.append((nums[i], nums[i + 1]))
            return [close_ring(coords)]
    except Exception:
        pass

    # 4) DM parser fallback
    ints = re.findall(r"\b\d+\b", normalized)
    try:
        toks = list(map(int, ints))
        if len(toks) >= 6 and len(toks) % 2 == 0 and all((t % 100) < 60 for t in toks):
            coords = []
            for i in range(0, len(toks), 2):
                lat = dm_to_dd(toks[i])
                lon = -dm_to_dd(toks[i + 1])
                coords.append((lat, lon))
            return [close_ring(coords)]
    except Exception:
        pass

    return []

def extract_coords_from_kml_string(kml_str):
    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    root = ET.fromstring(kml_str)
    polys = []

    for node in root.findall(".//kml:Polygon//kml:outerBoundaryIs//kml:LinearRing//kml:coordinates", ns):
        pts = []
        raw = node.text.strip().split()
        for c in raw:
            lon, lat = map(float, c.split(",")[:2])
            pts.append((lat, lon))
        polys.append(close_ring(pts))

    return polys

def extract_coords_from_kmz(kmz_bytes):
    with zipfile.ZipFile(BytesIO(kmz_bytes)) as z:
        for name in z.namelist():
            if name.lower().endswith(".kml"):
                return extract_coords_from_kml_string(z.read(name).decode("utf-8"))
    return []

def geometry_to_latlon_polygons(geom):
    """
    Convert shapely Polygon or MultiPolygon to list of polygons in (lat, lon).
    Only exterior rings are used.
    """
    polygons = []

    if isinstance(geom, Polygon):
        coords = list(geom.exterior.coords)  # (lon, lat)
        polygons.append(lonlat_to_latlon(coords))
    elif isinstance(geom, MultiPolygon):
        for part in geom.geoms:
            coords = list(part.exterior.coords)
            polygons.append(lonlat_to_latlon(coords))

    return polygons

def estimate_population_from_coords(multi_coords, raster_path):
    try:
        feats = [{
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [latlon_to_lonlat(coords)]
            },
            "properties": {}
        } for coords in multi_coords]

        with tempfile.NamedTemporaryFile(delete=False, suffix=".geojson", mode="w", encoding="utf-8") as tmp:
            gpd.GeoDataFrame.from_features(feats).to_file(tmp.name, driver="GeoJSON")
            stats = zonal_stats(tmp.name, raster_path, stats=["sum"])
        os.unlink(tmp.name)

        return sum(s["sum"] for s in stats if s["sum"] is not None)
    except Exception as e:
        st.error(f"Error estimating population: {e}")
        return None

def build_merged_geometry(polygons_latlon):
    """
    polygons_latlon: list of polygons where each polygon is [(lat, lon), ...]
    returns shapely geometry in correct GIS coordinate order (lon, lat)
    """
    shapely_polys = []
    for poly in polygons_latlon:
        lonlat = latlon_to_lonlat(poly)
        geom = Polygon(lonlat)
        if not geom.is_valid:
            geom = geom.buffer(0)
        if not geom.is_empty:
            shapely_polys.append(geom)

    if not shapely_polys:
        return None

    merged = unary_union(shapely_polys)

    if not merged.is_valid:
        merged = merged.buffer(0)

    return merged

# ---------------------------
# Uploader callback
# ---------------------------
def _on_upload_change():
    files = st.session_state.uploaded_files or []
    polys = []

    for u in files:
        ext = u.name.rsplit(".", 1)[-1].lower()
        try:
            if ext in ("geojson", "json"):
                gj = json.load(u)

                if gj.get("type") == "FeatureCollection":
                    features = gj.get("features", [])
                elif gj.get("type") == "Feature":
                    features = [gj]
                else:
                    features = [{"type": "Feature", "geometry": gj, "properties": {}}]

                for f in features:
                    geom = shape(f["geometry"])

                    if isinstance(geom, Polygon):
                        polys.extend(geometry_to_latlon_polygons(geom))
                    elif isinstance(geom, MultiPolygon):
                        polys.extend(geometry_to_latlon_polygons(geom))

            elif ext == "kml":
                polys.extend(extract_coords_from_kml_string(u.read().decode("utf-8")))

            elif ext == "kmz":
                polys.extend(extract_coords_from_kmz(u.read()))

        except Exception as e:
            st.error(f"Error in {u.name}: {e}")

    st.session_state["coords"] = polys

    if not files:
        st.session_state["generate_done"] = False
    elif st.session_state["generate_done"]:
        st.session_state["map_key"] += 1

# ---------------------------
# Input mode selector
# ---------------------------
input_mode = st.radio("Choose Input Method", ["Paste Coordinates", "Upload Map Files"], horizontal=True)

if st.session_state["last_input_mode"] != input_mode:
    st.session_state["last_input_mode"] = input_mode
    st.session_state["coords"] = []
    st.session_state["generate_done"] = False

# ---------------------------
# Paste Coordinates
# ---------------------------
if input_mode == "Paste Coordinates":
    st.text_area("Coordinates:", height=150, key="coord_input")

    if st.button("Generate Map", use_container_width=True):
        st.session_state["coord_trigger"] = True
        st.session_state["upload_trigger"] = False
        st.session_state["generate_done"] = True
        st.session_state["map_key"] += 1

# ---------------------------
# Upload files
# ---------------------------
else:
    st.file_uploader(
        "Upload Polygon Files (KML, KMZ, GeoJSON, JSON)",
        type=["kml", "kmz", "geojson", "json"],
        key="uploaded_files",
        accept_multiple_files=True,
        on_change=_on_upload_change
    )

    if st.button("Generate Map", use_container_width=True):
        st.session_state["upload_trigger"] = True
        st.session_state["coord_trigger"] = False
        st.session_state["generate_done"] = True
        st.session_state["map_key"] += 1

# ---------------------------
# Coordinate trigger logic
# ---------------------------
if st.session_state["coord_trigger"]:
    txt = st.session_state.get("coord_input", "").strip()
    if not txt:
        st.error("Please enter some coordinates.")
    else:
        parsed = parse_coords(txt)
        if parsed:
            st.session_state["coords"] = parsed
        else:
            st.error("No valid coordinates found.")
    st.session_state["coord_trigger"] = False

# ---------------------------
# Upload trigger cleanup
# ---------------------------
if st.session_state["upload_trigger"]:
    if not st.session_state.get("uploaded_files"):
        st.error("Please upload a valid file.")
    st.session_state["upload_trigger"] = False

# ---------------------------
# Main output
# ---------------------------
if st.session_state["generate_done"] and st.session_state["coords"]:
    polygons = st.session_state["coords"]

    with st.spinner("Generating map and estimating population..."):
        # Build individual KML
        kml = simplekml.Kml()
        for i, poly in enumerate(polygons):
            # simplekml expects (lon, lat)
            kml.newpolygon(
                name=f"Polygon {i + 1}",
                outerboundaryis=latlon_to_lonlat(poly)
            )

        # Build GeoJSON
        gj = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [latlon_to_lonlat(p)]
                    },
                    "properties": {}
                }
                for p in polygons
            ]
        }

        # Correct merged geometry
        merged_geom = build_merged_geometry(polygons)

        merged_available = False
        merged_kml_bytes = None
        merged_gj = None

        if merged_geom and isinstance(merged_geom, Polygon):
            merged_available = True
            merged_coords = list(merged_geom.exterior.coords)  # already (lon, lat)

            mkml = simplekml.Kml()
            mkml.newpolygon(
                name="Merged Polygon",
                outerboundaryis=merged_coords
            )
            merged_kml_bytes = mkml.kml().encode("utf-8")

            merged_gj = {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": mapping(merged_geom),
                        "properties": {}
                    }
                ]
            }

        elif merged_geom and isinstance(merged_geom, MultiPolygon):
            st.warning("Non-contiguous polygons detected; merged download is disabled.")

        else:
            st.error("Unable to build merged geometry.")

        col1, col2 = st.columns(2)

        with col1:
            st.download_button(
                "Download KML",
                kml.kml().encode("utf-8"),
                file_name="polygons.kml",
                mime="application/vnd.google-earth.kml+xml",
                use_container_width=True
            )
            if merged_available:
                st.download_button(
                    "Download Merged KML",
                    merged_kml_bytes,
                    file_name="merged_polygons.kml",
                    mime="application/vnd.google-earth.kml+xml",
                    use_container_width=True
                )

        with col2:
            st.download_button(
                "Download GeoJSON",
                json.dumps(gj, indent=2).encode("utf-8"),
                file_name="polygons.geojson",
                mime="application/geo+json",
                use_container_width=True
            )
            if merged_available:
                st.download_button(
                    "Download Merged GeoJSON",
                    json.dumps(merged_gj, indent=2).encode("utf-8"),
                    file_name="merged_polygons.geojson",
                    mime="application/geo+json",
                    use_container_width=True
                )

        pop = estimate_population_from_coords(polygons, "data/landscan-global-2024.tif")
        if pop is not None:
            st.success(f"Estimated Population: {pop:,.0f}")

    st.markdown("<h4 style='text-align: center;'>Polygon Preview</h4>", unsafe_allow_html=True)

    # Folium expects (lat, lon)
    m = folium.Map(tiles="CartoDB positron")
    pts = []

    for poly in polygons:
        folium.Polygon(locations=poly, color="blue", fill=True).add_to(m)
        pts.extend(poly)

    if pts:
        m.fit_bounds(
            [
                [min(p[0] for p in pts), min(p[1] for p in pts)],
                [max(p[0] for p in pts), max(p[1] for p in pts)],
            ],
            padding=(5, 5),
        )

    html_str = m.get_root().render()
    st_html(html_str, height=400, width=700, scrolling=False)
