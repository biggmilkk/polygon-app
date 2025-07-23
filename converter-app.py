import streamlit as st
import simplekml
import folium
import re
import geopandas as gpd
from rasterstats import zonal_stats
from streamlit_folium import st_folium  # you can keep this import if you ever need returned_objects
from streamlit.components.v1 import html as st_html
import tempfile
import os
import json
from xml.etree import ElementTree as ET
import zipfile
from io import BytesIO

st.set_page_config(page_title="Polygon Generator and Population Estimate", layout="centered")

# --- Session state initialization ---
for key, default in {
    "coords": [],
    "coord_trigger": False,
    "upload_trigger": False,
    "last_input_mode": None,
    "generate_done": False,  # has the user clicked "Generate Map"?
    "map_key": 0,            # bump this to remount the map component
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# --- Page header ---
st.markdown("<h2 style='text-align: center;'>Polygon Generator and Population Estimate</h2>", unsafe_allow_html=True)
st.markdown(
    "<p style='text-align: center; font-size: 0.9rem; color: grey;'>"
    "Upload spatial data files or enter coordinates manually to visualize geographic areas "
    "on an interactive map. Define custom polygons and generate population estimates using LandScan data."
    "</p>",
    unsafe_allow_html=True
)

# --- Helper functions ---
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
                coords.append((floats[i], floats[i+1]))
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            return [coords]
        except:
            pass

    try:
        tokens = list(map(int, int_tokens))
        if all(t % 100 < 60 for t in tokens):
            for i in range(0, len(tokens) - 1, 2):
                lat = dm_to_dd(tokens[i])
                lon = -dm_to_dd(tokens[i+1])
                coords.append((lat, lon))
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            return [coords]
    except:
        pass

    try:
        tokens = list(map(int, int_tokens))
        for i in range(0, len(tokens) - 1, 2):
            coords.append((tokens[i]/100.0, -tokens[i+1]/100.0))
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        return [coords]
    except:
        return []

def extract_coords_from_kml_string(kml_string):
    ns = {'kml': 'http://www.opengis.net/kml/2.2'}
    root = ET.fromstring(kml_string)
    polygons = []
    for coord_text in root.findall(".//kml:Polygon/kml:outerBoundaryIs/kml:LinearRing/kml:coordinates", ns):
        raw = coord_text.text.strip().split()
        coords = []
        for c in raw:
            lon, lat = map(float, c.split(',')[:2])
            coords.append((lat, lon))
        if coords and coords[0] != coords[-1]:
            coords.append(coords[0])
        polygons.append(coords)
    return polygons

def extract_coords_from_kmz(file_bytes):
    with zipfile.ZipFile(BytesIO(file_bytes)) as kmz:
        for name in kmz.namelist():
            if name.endswith('.kml'):
                return extract_coords_from_kml_string(kmz.read(name).decode())
    return []

def estimate_population_from_coords(multi_coords, raster_path):
    try:
        features = [{
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[(lon, lat) for lat, lon in coords]]},
            "properties": {}
        } for coords in multi_coords]
        with tempfile.NamedTemporaryFile(delete=False, suffix='.geojson', mode='w') as tmp:
            gpd.GeoDataFrame.from_features(features).to_file(tmp.name, driver='GeoJSON')
            stats = zonal_stats(tmp.name, raster_path, stats=['sum'])
        os.unlink(tmp.name)
        return sum(s['sum'] for s in stats if s['sum'] is not None)
    except Exception as e:
        st.error(f"Error estimating population: {e}")
        return None

# --- Uploader on_change callback ---
def _on_upload_change():
    files = st.session_state.uploaded_files or []
    polygons = []
    for u in files:
        ext = u.name.rsplit('.',1)[-1].lower()
        try:
            if ext in ('geojson','json'):
                gj = json.load(u)
                feats = gj.get('features',[gj])
                for f in feats:
                    geom = f['geometry']
                    if geom['type'].lower()=='polygon':
                        coords = geom['coordinates'][0]
                        if coords[0]!=coords[-1]:
                            coords.append(coords[0])
                        polygons.append([(lat,lon) for lon,lat in coords])
                    elif geom['type'].lower()=='multipolygon':
                        for part in geom['coordinates']:
                            coords = part[0]
                            if coords[0]!=coords[-1]:
                                coords.append(coords[0])
                            polygons.append([(lat,lon) for lon,lat in coords])
            elif ext=='kml':
                polygons.extend(extract_coords_from_kml_string(u.read().decode()))
            elif ext=='kmz':
                polygons.extend(extract_coords_from_kmz(u.read()))
        except Exception as e:
            st.error(f"Error in {u.name}: {e}")
    st.session_state['coords'] = polygons
    # if map already generated, force update
    if st.session_state['generate_done']:
        st.session_state['map_key'] += 1

# --- Input selector ---
input_mode = st.radio("Choose Input Method", ["Paste Coordinates","Upload Map Files"], horizontal=True)
if st.session_state['last_input_mode'] != input_mode:
    st.session_state['last_input_mode'] = input_mode
    st.session_state['coords'] = []

# --- Coordinates branch ---
if input_mode=="Paste Coordinates":
    st.text_area("Coordinates:", height=150, key="coord_input")
    if st.button("Generate Map", use_container_width=True):
        st.session_state['coord_trigger'] = True
        st.session_state['upload_trigger'] = False
        st.session_state['generate_done'] = True
        st.session_state['map_key'] += 1

# --- Upload branch ---
else:
    uploaded = st.file_uploader(
        "Upload Polygon Files (KML, KMZ, GeoJSON, JSON)",
        type=["kml","kmz","geojson","json"],
        accept_multiple_files=True,
        key="uploaded_files",
        on_change=_on_upload_change
    )
    if st.button("Generate Map", use_container_width=True):
        st.session_state['upload_trigger'] = True
        st.session_state['coord_trigger'] = False
        st.session_state['generate_done'] = True
        st.session_state['map_key'] += 1

# --- Coordinate trigger logic ---
if st.session_state['coord_trigger']:
    txt = st.session_state.get('coord_input','')
    if not txt.strip():
        st.error("Please enter some coordinates.")
    else:
        parsed = parse_coords(txt)
        if parsed:
            st.session_state['coords'] = parsed
        else:
            st.error("No valid coordinates found.")
    st.session_state['coord_trigger'] = False

# --- Legacy upload trigger (optional) ---
if st.session_state['upload_trigger']:
    if not st.session_state.get('uploaded_files'):
        st.error("Please upload a valid file.")
    st.session_state['upload_trigger'] = False

# --- Display Map & Population ---
if st.session_state['generate_done'] and st.session_state['coords']:
    polys = st.session_state['coords']

    with st.spinner("Generating map and estimating population..."):
        # KML + GeoJSON downloads
        kml = simplekml.Kml()
        for i, poly in enumerate(polys):
            kml.newpolygon(name=f"Polygon {i+1}", outerboundaryis=[(lon,lat) for lat,lon in poly])
        gj = {"type":"FeatureCollection","features":[
            {"type":"Feature","geometry":{"type":"Polygon","coordinates":[[(lon,lat) for lat,lon in p]]},"properties":{}}
            for p in polys
        ]}

        c1,c2 = st.columns(2)
        with c1:
            st.download_button("Download KML", kml.kml().encode('utf-8'),
                               file_name="polygons.kml",
                               mime="application/vnd.google-earth.kml+xml",
                               use_container_width=True)
        with c2:
            st.download_button("Download GeoJSON", json.dumps(gj,indent=2).encode('utf-8'),
                               file_name="polygons.geojson",
                               mime="application/geo+json",
                               use_container_width=True)

        pop = estimate_population_from_coords(polys, "data/landscan-global-2023.tif")
        if pop is not None:
            st.success(f"Estimated Population: {pop:,.0f}")

    st.markdown("<h4 style='text-align: center;'>Polygon Preview</h4>", unsafe_allow_html=True)

    # build folium map
    m = folium.Map(tiles="CartoDB positron")
    pts = []
    for poly in polys:
        folium.Polygon(locations=poly, color="blue", fill=True).add_to(m)
        pts.extend(poly)

    if pts:
        m.fit_bounds([
            [min(p[0] for p in pts), min(p[1] for p in pts)],
            [max(p[0] for p in pts), max(p[1] for p in pts)]
        ], padding=(5,5))

    # embed with fixed height via components.html to avoid extra whitespace
    html_str = m.get_root().render()
    st_html(html_str, height=400, width=700, scrolling=False")
