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
from shapely.geometry import Polygon, mapping, MultiPolygon
from shapely.ops import unary_union

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
st.caption("Population estimates use LandScan Global 2024 data from ORNL.")

# --- Helper functions ---
def dm_to_dd(dm):
    deg = int(dm // 100)
    mins = dm % 100
    return round(deg + mins/60, 6)

def dms_to_dd(d, m, s, dir):
    dd = d + m/60 + s/3600
    if dir in ['S', 'W']:
        dd *= -1
    return round(dd, 6)

def parse_coords(text):
    text = text.replace(',', ' ').replace(';', ' ')
    dms = re.findall(r'(\d+)[°:\s](\d+)[′:\s](\d+)[″\s]?([NSEW])', text.upper())
    floats = re.findall(r'[-+]?\d*\.\d+', text)
    ints = re.findall(r'\b\d+\b', text)
    coords = []

    if len(dms) >= 2:
        try:
            for i in range(0, len(dms)-1, 2):
                ld, lm, ls, ldir = dms[i]
                od, om, os, odir = dms[i+1]
                lat = dms_to_dd(int(ld), int(lm), int(ls), ldir)
                lon = dms_to_dd(int(od), int(om), int(os), odir)
                coords.append((lat, lon))
            if coords and coords[0] != coords[-1]:
                coords.append(coords[0])
            return [coords]
        except:
            pass

    if len(floats) >= 2:
        try:
            nums = list(map(float, floats))
            for i in range(0, len(nums)-1, 2):
                coords.append((nums[i], nums[i+1]))
            if coords and coords[0] != coords[-1]:
                coords.append(coords[0])
            return [coords]
        except:
            pass

    try:
        toks = list(map(int, ints))
        if all(t % 100 < 60 for t in toks):
            for i in range(0, len(toks)-1, 2):
                coords.append((dm_to_dd(toks[i]), -dm_to_dd(toks[i+1])))
            if coords and coords[0] != coords[-1]:
                coords.append(coords[0])
            return [coords]
    except:
        pass

    try:
        toks = list(map(int, ints))
        for i in range(0, len(toks)-1, 2):
            coords.append((toks[i]/100.0, -toks[i+1]/100.0))
        if coords and coords[0] != coords[-1]:
            coords.append(coords[0])
        return [coords]
    except:
        return []

def extract_coords_from_kml_string(kml_str):
    ns = {'kml': 'http://www.opengis.net/kml/2.2'}
    root = ET.fromstring(kml_str)
    polys = []
    for node in root.findall(".//kml:Polygon//kml:coordinates", ns):
        pts = []
        for c in node.text.strip().split():
            lon, lat = map(float, c.split(',')[:2])
            pts.append((lat, lon))
        if pts and pts[0] != pts[-1]:
            pts.append(pts[0])
        polys.append(pts)
    return polys

def extract_coords_from_kmz(kmz_bytes):
    with zipfile.ZipFile(BytesIO(kmz_bytes)) as z:
        for name in z.namelist():
            if name.endswith('.kml'):
                return extract_coords_from_kml_string(z.read(name).decode())
    return []

def estimate_population_from_coords(multi_coords, raster_path):
    try:
        feats = [{
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[(lon, lat) for lat, lon in coords]]},
            "properties": {}
        } for coords in multi_coords]
        with tempfile.NamedTemporaryFile(delete=False, suffix='.geojson', mode='w') as tmp:
            gpd.GeoDataFrame.from_features(feats).to_file(tmp.name, driver='GeoJSON')
            stats = zonal_stats(tmp.name, raster_path, stats=['sum'])
        os.unlink(tmp.name)
        return sum(s['sum'] for s in stats if s['sum'] is not None)
    except Exception as e:
        st.error(f"Error estimating population: {e}")
        return None

# --- Uploader on_change callback ---
def _on_upload_change():
    files = st.session_state.uploaded_files or []
    polys = []
    for u in files:
        ext = u.name.rsplit('.',1)[-1].lower()
        try:
            if ext in ('geojson','json'):
                gj = json.load(u)
                feats = gj.get('features',[gj])
                for f in feats:
                    geom = f['geometry']
                    if geom['type'].lower() == 'polygon':
                        coords = geom['coordinates'][0]
                        if coords[0] != coords[-1]:
                            coords.append(coords[0])
                        polys.append([(lat,lon) for lon,lat in coords])
                    elif geom['type'].lower() == 'multipolygon':
                        for part in geom['coordinates']:
                            c = part[0]
                            if c[0] != c[-1]:
                                c.append(c[0])
                            polys.append([(lat,lon) for lon,lat in c])
            elif ext == 'kml':
                polys.extend(extract_coords_from_kml_string(u.read().decode()))
            elif ext == 'kmz':
                polys.extend(extract_coords_from_kmz(u.read()))
        except Exception as e:
            st.error(f"Error in {u.name}: {e}")
    st.session_state['coords'] = polys
    if not files:
        # reset generate if all removed
        st.session_state['generate_done'] = False
    elif st.session_state['generate_done']:
        st.session_state['map_key'] += 1

# --- Input mode selector ---
input_mode = st.radio("Choose Input Method", ["Paste Coordinates", "Upload Map Files"], horizontal=True)
if st.session_state['last_input_mode'] != input_mode:
    st.session_state['last_input_mode'] = input_mode
    st.session_state['coords'] = []
    st.session_state['generate_done'] = False

# --- Paste Coordinates branch ---
if input_mode == "Paste Coordinates":
    st.text_area("Coordinates:", height=150, key="coord_input")
    if st.button("Generate Map", use_container_width=True):
        st.session_state['coord_trigger']  = True
        st.session_state['upload_trigger'] = False
        st.session_state['generate_done']  = True
        st.session_state['map_key']       += 1

# --- Upload Map Files branch ---
else:
    st.file_uploader(
        "Upload Polygon Files (KML, KMZ, GeoJSON, JSON)",
        type=["kml","kmz","geojson","json"],
        key="uploaded_files",
        accept_multiple_files=True,
        on_change=_on_upload_change
    )
    if st.button("Generate Map", use_container_width=True):
        st.session_state['upload_trigger'] = True
        st.session_state['coord_trigger']  = False
        st.session_state['generate_done']  = True
        st.session_state['map_key']       += 1

# --- Coordinate trigger logic ---
if st.session_state['coord_trigger']:
    txt = st.session_state.get('coord_input','').strip()
    if not txt:
        st.error("Please enter some coordinates.")
    else:
        parsed = parse_coords(txt)
        if parsed:
            st.session_state['coords'] = parsed
        else:
            st.error("No valid coordinates found.")
    st.session_state['coord_trigger'] = False

# --- Legacy upload trigger cleanup ---
if st.session_state['upload_trigger']:
    if not st.session_state.get('uploaded_files'):
        st.error("Please upload a valid file.")
    st.session_state['upload_trigger'] = False

# --- Display Map, Downloads & Population (only after Generate) ---
if st.session_state['generate_done'] and st.session_state['coords']:
    polygons = st.session_state['coords']

    with st.spinner("Generating map and estimating population..."):
        # build individual KML & GeoJSON
        kml = simplekml.Kml()
        for i, poly in enumerate(polygons):
            kml.newpolygon(name=f"Polygon {i+1}", outerboundaryis=[(lon,lat) for lat,lon in poly])
        gj = {
            "type":"FeatureCollection",
            "features":[
                {
                    "type":"Feature",
                    "geometry":{"type":"Polygon","coordinates":[[(lon,lat) for lat,lon in p]]},
                    "properties":{}
                } for p in polygons
            ]
        }

        # build merged geometry but only allow if result is a single Polygon
        merged_geom = unary_union([Polygon(poly) for poly in polygons])
        merged_available = False
        merged_kml_bytes = None
        merged_gj = None
        if isinstance(merged_geom, Polygon):
            merged_available = True
            merged_coords = list(merged_geom.exterior.coords)
            mkml = simplekml.Kml()
            mkml.newpolygon(name="Merged Polygon", outerboundaryis=[(lon,lat) for lat,lon in merged_coords])
            merged_kml_bytes = mkml.kml().encode('utf-8')
            merged_gj = {
                "type":"FeatureCollection",
                "features":[
                    {"type":"Feature","geometry":mapping(merged_geom),"properties":{}}
                ]
            }
        else:
            st.error("Non-contiguous polygons detected; download merged is disabled.")

        col1, col2 = st.columns(2)
        with col1:
            st.download_button(
                "Download KML",
                kml.kml().encode('utf-8'),
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
                json.dumps(gj, indent=2).encode('utf-8'),
                file_name="polygons.geojson",
                mime="application/geo+json",
                use_container_width=True
            )
            if merged_available:
                st.download_button(
                    "Download Merged GeoJSON",
                    json.dumps(merged_gj, indent=2).encode('utf-8'),
                    file_name="merged_polygons.geojson",
                    mime="application/geo+json",
                    use_container_width=True
                )

        pop = estimate_population_from_coords(polygons, "data/landscan-global-2024.tif")
        if pop is not None:
            st.success(f"Estimated Population: {pop:,.0f}")

    st.markdown("<h4 style='text-align: center;'>Polygon Preview</h4>", unsafe_allow_html=True)

    m = folium.Map(tiles="CartoDB positron")
    pts = []
    for poly in polygons:
        folium.Polygon(locations=poly, color="blue", fill=True).add_to(m)
        pts.extend(poly)
    if pts:
        m.fit_bounds([[min(p[0] for p in pts), min(p[1] for p in pts)],
                      [max(p[0] for p in pts), max(p[1] for p in pts)]],
                     padding=(5,5))

    html_str = m.get_root().render()
    st_html(html_str, height=400, width=700, scrolling=False)
