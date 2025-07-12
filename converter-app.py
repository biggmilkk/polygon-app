import streamlit as st
import simplekml
import folium
import re
import geopandas as gpd
from rasterstats import zonal_stats
from streamlit_folium import st_folium
import tempfile
import os
import json
from xml.etree import ElementTree as ET
import zipfile
from io import BytesIO

st.set_page_config(page_title="Polygon Generator and Population Estimate", layout="centered")

for key, default in {
    "coords": [],
    "generate_trigger": False,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

st.markdown("<h2 style='text-align: center;'>Polygon Generator and Population Estimate</h2>", unsafe_allow_html=True)
st.markdown(
    "<p style='text-align: center; font-size: 0.9rem; color: grey;'>Upload spatial data files or enter coordinates manually to visualize geographic areas on an interactive map. Define custom polygons and generate population estimates using LandScan data.</p>",
    unsafe_allow_html=True
)

input_mode = st.radio("Choose Input Method", ["Paste Coordinates", "Upload Map Files"], horizontal=True)

if input_mode == "Paste Coordinates":
    st.text_area("Coordinates:", height=150, key="coord_input")

uploaded_files = []
if input_mode == "Upload Map Files":
    uploaded_files = st.file_uploader("Upload Polygon Files (KML, KMZ, GeoJSON, JSON)", type=["kml", "kmz", "geojson", "json"], accept_multiple_files=True)

if input_mode == "Upload Map Files" and not uploaded_files:
    st.session_state["coords"] = []
elif input_mode == "Paste Coordinates" and not st.session_state.get("coord_input", "").strip():
    st.session_state["coords"] = []

def dm_to_dd(dm):
    degrees = int(dm // 100)
    minutes = dm % 100
    return round(degrees + minutes / 60, 6)

def dms_to_dd(degrees, minutes, seconds, direction):
    dd = degrees + minutes / 60 + seconds / 3600
    if direction in ['S', 'W']:
        dd *= -1
    return round(dd, 6)

def parse_coords(text):
    text = text.replace(',', ' ').replace(';', ' ')
    dms_pattern = re.findall(r'(\d+)[\u00b0:\s](\d+)[\u2032:\s](\d+)[\u2033\s]?([NSEW])', text.upper())
    float_tokens = re.findall(r'[-+]?\d*\.\d+', text)
    int_tokens = re.findall(r'\b\d+\b', text)
    coords = []

    if len(dms_pattern) >= 2:
        try:
            for i in range(0, len(dms_pattern) - 1, 2):
                lat_d, lat_m, lat_s, lat_dir = dms_pattern[i]
                lon_d, lon_m, lon_s, lon_dir = dms_pattern[i + 1]
                lat = dms_to_dd(int(lat_d), int(lat_m), int(lat_s), lat_dir)
                lon = dms_to_dd(int(lon_d), int(lon_m), int(lon_s), lon_dir)
                coords.append((lat, lon))
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            return [coords]
        except:
            pass

    if len(float_tokens) >= 2:
        try:
            floats = list(map(float, float_tokens))
            for i in range(0, len(floats) - 1, 2):
                lat = floats[i]
                lon = floats[i + 1]
                coords.append((lat, lon))
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            return [coords]
        except:
            pass

    try:
        tokens = list(map(int, int_tokens))
        if all(t % 100 < 60 for t in tokens):
            for i in range(0, len(tokens) - 1, 2):
                lat_dm = tokens[i]
                lon_dm = tokens[i + 1]
                lat = dm_to_dd(lat_dm)
                lon = -dm_to_dd(lon_dm)
                coords.append((lat, lon))
            if coords and coords[0] != coords[-1]:
                coords.append(coords[0])
            return [coords]
    except:
        pass

    try:
        tokens = list(map(int, int_tokens))
        for i in range(0, len(tokens) - 1, 2):
            lat = tokens[i] / 100.0
            lon = -tokens[i + 1] / 100.0
            coords.append((lat, lon))
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        return [coords]
    except Exception as e:
        st.error(f"Auto-detect failed: {e}")
        return []

def extract_coords_from_kml_string(kml_string):
    ns = {'kml': 'http://www.opengis.net/kml/2.2'}
    root = ET.fromstring(kml_string)
    polygons = []
    for coord_text in root.findall(".//kml:Polygon/kml:outerBoundaryIs/kml:LinearRing/kml:coordinates", ns):
        coords = []
        raw_coords = coord_text.text.strip().split()
        for coord in raw_coords:
            parts = coord.split(',')
            if len(parts) >= 2:
                lon, lat = map(float, parts[:2])
                coords.append((lat, lon))
        if coords and coords[0] != coords[-1]:
            coords.append(coords[0])
        if coords:
            polygons.append(coords)
    return polygons

def extract_coords_from_kmz(file_bytes):
    with zipfile.ZipFile(BytesIO(file_bytes)) as kmz:
        for name in kmz.namelist():
            if name.endswith(".kml"):
                kml_string = kmz.read(name).decode("utf-8")
                return extract_coords_from_kml_string(kml_string)
    return []

def estimate_population_from_coords(multi_coords, raster_path):
    try:
        features = []
        for coords in multi_coords:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[(lon, lat) for lat, lon in coords]]},
                "properties": {}
            })
        poly_geojson = {"type": "FeatureCollection", "features": features}
        with tempfile.NamedTemporaryFile(delete=False, suffix=".geojson", mode="w") as tmp:
            gdf = gpd.GeoDataFrame.from_features(poly_geojson["features"])
            gdf.to_file(tmp.name, driver="GeoJSON")
            tmp_path = tmp.name
        stats = zonal_stats(tmp_path, raster_path, stats=["sum"])
        os.unlink(tmp_path)
        return sum(s["sum"] for s in stats if s["sum"] is not None)
    except Exception as e:
        st.error(f"Error estimating population: {e}")
        return None

if st.button("Generate Map", use_container_width=True):
    st.session_state["generate_trigger"] = True

if st.session_state.get("generate_trigger"):
    all_polygons = []

    with st.spinner("Processing input..."):
        if input_mode == "Paste Coordinates":
            text = st.session_state.get("coord_input", "")
            all_polygons = parse_coords(text)

        elif input_mode == "Upload Map Files" and uploaded_files:
            for uploaded_file in uploaded_files:
                file_type = uploaded_file.name.split('.')[-1].lower()
                try:
                    if file_type in ["geojson", "json"]:
                        geojson = json.load(uploaded_file)
                        features = geojson["features"] if geojson["type"] == "FeatureCollection" else [geojson]
                        for feature in features:
                            geom = feature["geometry"]
                            if geom["type"].lower() == "polygon":
                                coords = geom["coordinates"][0]
                                if coords[0] != coords[-1]:
                                    coords.append(coords[0])
                                coords = [(lat, lon) for lon, lat in coords]
                                all_polygons.append(coords)
                            elif geom["type"].lower() == "multipolygon":
                                for part in geom["coordinates"]:
                                    coords = part[0]
                                    if coords[0] != coords[-1]:
                                        coords.append(coords[0])
                                    coords = [(lat, lon) for lon, lat in coords]
                                    all_polygons.append(coords)
                    elif file_type == "kml":
                        doc = uploaded_file.read().decode("utf-8")
                        all_polygons.extend(extract_coords_from_kml_string(doc))
                    elif file_type == "kmz":
                        all_polygons.extend(extract_coords_from_kmz(uploaded_file.read()))
                except Exception as e:
                    st.error(f"Error processing {uploaded_file.name}: {e}")

    if all_polygons:
        st.session_state["coords"] = all_polygons
        st.session_state["generate_trigger"] = False
    else:
        st.session_state["coords"] = []
        st.error("No valid coordinates or polygons provided. Please enter coordinates or upload a valid file.")
    
if not st.session_state.get("coords"):
    if st.session_state.get("generate_trigger") is False:
        pass
    else:
        st.error("No valid coordinates or polygons provided. Please enter coordinates or upload a valid file.")
else:
    polygons = st.session_state["coords"]

    with st.spinner("Generating map and estimating population..."):
        kml = simplekml.Kml()
        for i, poly in enumerate(polygons):
            kml_coords = [(lon, lat) for lat, lon in poly]
            kml.newpolygon(name=f"Polygon {i+1}", outerboundaryis=kml_coords)

        geojson_data = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[(lon, lat) for lat, lon in poly]]}, "properties": {}} for poly in polygons
            ]
        }

        col1, col2 = st.columns(2)
        with col1:
            st.download_button("Download KML", kml.kml().encode("utf-8"), file_name="polygons.kml", mime="application/vnd.google-earth.kml+xml", use_container_width=True)
        with col2:
            st.download_button("Download GeoJSON", json.dumps(geojson_data, indent=2).encode("utf-8"), file_name="polygons.geojson", mime="application/geo+json", use_container_width=True)

        raster_path = "data/landscan-global-2023.tif"
        population = estimate_population_from_coords(polygons, raster_path)
        if population is not None:
            st.success(f"Estimated Population: {population:,.0f}")

        with st.container():
            st.markdown("<h4 style='text-align: center;'>Polygon Preview</h4>", unsafe_allow_html=True)
            m = folium.Map(tiles="CartoDB positron")
            all_points = []
            for poly in polygons:
                folium.Polygon(locations=poly, color="blue", fill=True).add_to(m)
                all_points.extend(poly)

            if all_points:
                bounds = [[min(p[0] for p in all_points), min(p[1] for p in all_points)],
                          [max(p[0] for p in all_points), max(p[1] for p in all_points)]]
                m.fit_bounds(bounds, padding=(5, 5))

            st_folium(m, width=700, height=400, key="map_view", returned_objects=[])
