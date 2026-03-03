import streamlit as st
import pandas as pd
import requests
import folium
import json
import os
import numpy as np
from streamlit_folium import st_folium
from folium.plugins import LocateControl

# 1) CONFIGURACIÓN
st.set_page_config(page_title="Consorcio San Ramón - Las Pircas", layout="wide")

# 2) ESTILOS (Sin cambios)
st.markdown("""
<style>
    .titulo-responsive {
        text-align: center; color: #1E3A8A; font-weight: bold; padding: 10px;
        font-size: clamp(1.15rem, 2.5vw + 0.6rem, 2rem); line-height: 1.2;
        border-bottom: 2px solid #1E3A8A; margin-bottom: 16px; word-wrap: break-word;
    }
    .ficha-header {
        background-color: #1E3A8A; color: white; padding: 10px; border-radius: 5px;
        text-align: center; margin-bottom: 15px; font-weight: bold;
    }
    .metric-box {
        background: white; padding: 10px; border-radius: 8px; border: 1px solid #ddd; margin-bottom: 5px;
        border-left: 5px solid #1E3A8A;
    }
</style>
""", unsafe_allow_html=True)

# --- OPTIMIZACIÓN: Cache de GeoJSON locales para evitar parpadeo ---
@st.cache_data
def obtener_geojson_local(ruta, tipo):
    if os.path.exists(ruta):
        with open(ruta, encoding='utf-8') as f:
            data = json.load(f)
        if tipo == "canales":
            for i, feature in enumerate(data.get('features', [])):
                p = feature.setdefault('properties', {})
                p['fid'] = p.get('fid', f"canal_{i}")
        else:
            for i, feature in enumerate(data.get('features', [])):
                p = feature.setdefault('properties', {})
                p['fid'] = p.get('fid', f"finca_{i}")
        return data
    return None

# 3) CARGA DE DATOS (Kobo)
@st.cache_data(ttl=1800)
def cargar_datos_kobo():
    URL_AFORO = "https://kf.kobotoolbox.org/api/v2/assets/adRKxesyy7hBQNQbNVCtdt/data.json?limit=1000&ordering=-_submission_time"
    URL_MAPA  = "https://kf.kobotoolbox.org/api/v2/assets/and5RtS5yp74muGFDddySr/data.json?limit=1000"
    TOKEN = st.secrets["AFORO_TOKEN"]
    HEADERS = {'Authorization': f'Token {TOKEN}'}
    try:
        r1 = requests.get(URL_AFORO, headers=HEADERS, timeout=20)
        r2 = requests.get(URL_MAPA,  headers=HEADERS, timeout=20)
        df_a = pd.DataFrame(r1.json().get('results', []))
        df_m = pd.DataFrame(r2.json().get('results', []))
        if df_m.empty: return pd.DataFrame(), pd.DataFrame()
        
        df_m.columns = [c.strip() for c in df_m.columns]
        col_sistema = next((c for c in df_m.columns if 'sistema' in c.lower()), None)
        df_m['Sistema_Interno'] = df_m[col_sistema] if col_sistema else "General"
        df_m['id_aforador'] = df_m['Codigo_del_aforador_texto'].astype(str).str.strip()
        
        if 'Tipo' in df_m.columns:
            df_m['Tipo'] = df_m['Tipo'].astype(str).str.strip()
        else:
            df_m['Tipo'] = 'N/D'
        mapa_tipo = {'a50x210': 'Aforador Flume 50x210 cm', 'a20x90':  'Aforador Flume 20x90 cm'}
        df_m['Tipo_fmt'] = df_m['Tipo'].map(mapa_tipo).fillna(df_m['Tipo'])

        def ext_coords(v):
            try:
                p = str(v).strip().split()
                lat = float(p[0].replace(',', '.'))
                lon = float(p[1].replace(',', '.'))
                return lat, lon
            except: return None, None

        df_m['lat'], df_m['lon'] = zip(*df_m['Ubicaci_n'].apply(ext_coords))
        df_m = df_m.dropna(subset=['lat', 'lon'])

        if not df_a.empty:
            df_a['af_actual'] = df_a['af_actual'].astype(str).str.strip()
            df_a['fecha_dt']  = pd.to_datetime(df_a['Fecha'] + ' ' + df_a['Hora'], errors='coerce')
            df_a['caudal']    = pd.to_numeric(df_a['q_final'], errors='coerce').fillna(0).astype(int)
            df_a['fecha_format'] = df_a['fecha_dt'].dt.strftime('%d/%m')
            df_a['hora_format']  = df_a['fecha_dt'].dt.strftime('%H:%M')
        return df_a, df_m
    except Exception: return pd.DataFrame(), pd.DataFrame()

# --- CREDITOS INTA ---
URL_PRECIPITACIONES = "https://territorios.inta.gob.ar/assets/aYqLUVvU3EYiDa7NoJbPKF/submissions/?format=json"
URL_MAPA_P = "https://territorios.inta.gob.ar/assets/aFwWKNGXZKppgNYKa33wC8/submissions/?format=json"
TOKEN2 = st.secrets["PRECI_TOKEN"]
HEADERS_INTA = {'Authorization': f'Token {TOKEN2}'}

def extraer_coordenadas_pluv(row):
    try:
        valor = row.get('Ubicaci_in') or row.get('ubicaci_in') or row.get('_Ubicaci_in')
        if isinstance(valor, str):
            partes = valor.replace(',', '.').split()
            return float(partes[0]), float(partes[1])
        elif isinstance(valor, (list, tuple)) and len(valor) >= 2:
            return float(valor[0]), float(valor[1])
    except: return None, None
    return None, None

@st.cache_data(ttl=10800)
def cargar_pluviometros_INTAlike():
    try:
        r_p = requests.get(URL_PRECIPITACIONES, headers=HEADERS_INTA, timeout=25)
        r_c = requests.get(URL_MAPA_P, headers=HEADERS_INTA, timeout=25)
        df_p, df_c = pd.DataFrame(r_p.json()), pd.DataFrame(r_c.json())
        if df_p.empty or df_c.empty: return pd.DataFrame(), pd.DataFrame()

        df_p['fecha_dt'] = pd.to_datetime(df_p['Fecha_del_dato'], errors='coerce')
        df_p['mm'] = pd.to_numeric(df_p['Mil_metros_registrados'], errors='coerce')
        df_p['cod'] = df_p['Pluviometros'].astype(str).str.replace(r'\.0$', '', regex=True).str.strip().str.lower()
        df_p = df_p.dropna(subset=['fecha_dt', 'mm'])

        col_nombre = next((c for c in df_c.columns if 'Nombre_del_Pluviometro' in c), None)
        col_code = next((c for c in df_c.columns if 'codigo' in c.lower() and 'pluvi' in c.lower()), 'Codigo_txt_del_pluviometro')
        
        df_c['cod'] = df_c[col_code].astype(str).str.replace(r'\.0$', '', regex=True).str.strip().str.lower()
        coords = df_c.apply(extraer_coordenadas_pluv, axis=1)
        df_c['lat'], df_c['lon'] = zip(*coords)
        
        objetivos = ['aurelia', 'lasceibas']
        df_meta_2 = df_c[df_c['cod'].isin(objetivos)].copy().dropna(subset=['lat', 'lon'])
        mapa_nombres = {'aurelia': 'La Aurelia', 'lasceibas': 'Finca Las Ceibas'}
        df_meta_2['nombre_visible'] = df_meta_2['cod'].map(mapa_nombres)
        return df_meta_2[['cod', 'lat', 'lon', 'nombre_visible']], df_p[['cod', 'fecha_dt', 'mm']]
    except Exception: return pd.DataFrame(), pd.DataFrame()

def get_color_sistema(sistema):
    colores = {"San Ramón - Las Pircas": "#FF5733", "Santos Lugares": "#2ECC71", "Las Ceibas": "#3498DB", "El Mollar": "#F333FF", "El Pedregal": "#000000"}
    return colores.get(sistema, "#808080")

# --- LÓGICA GEOMÉTRICA (Sin cambios) ---
def _point_in_polygon(pt, polygon):
    x, y = pt
    inside = False
    for i in range(len(polygon)):
        x1, y1 = polygon[i]; x2, y2 = polygon[(i + 1) % len(polygon)]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1): inside = not inside
    return inside

def _point_in_multipolygon(pt, coords):
    def poly_contains(pt, poly):
        if not _point_in_polygon(pt, poly[0]): return False
        for hole in poly[1:]:
            if _point_in_polygon(pt, hole): return False
        return True
    if len(coords) > 0 and isinstance(coords[0][0][0], (float, int)): return poly_contains(pt, coords)
    else:
        for poly in coords:
            if poly_contains(pt, poly): return True
        return False

def _squared_distance_point_to_segment(px, py, x1, y1, x2, y2):
    vx, vy = x2 - x1, y2 - y1
    wx, wy = px - x1, py - y1
    c1 = vx * wx + vy * wy
    if c1 <= 0: return (px - x1)**2 + (py - y1)**2
    c2 = vx * vx + vy * vy
    if c2 <= c1: return (px - x2)**2 + (py - y2)**2
    b = c1 / (c2 + 1e-12)
    return (px - (x1 + b * vx))**2 + (py - (y1 + b * vy))**2

def _min_squared_distance_to_lines(coords, px, py):
    def line_dist(line):
        dmin = float('inf')
        for i in range(len(line) - 1):
            d = _squared_distance_point_to_segment(px, py, line[i][0], line[i][1], line[i+1][0], line[i+1][1])
            if d < dmin: dmin = d
        return dmin
    if len(coords) > 0 and isinstance(coords[0][0], (float, int)): return line_dist(coords)
    else:
        dmin = float('inf')
        for line in coords:
            d = line_dist(line)
            if d < dmin: dmin = d
        return dmin

def _buscar_marker_por_click(lat, lon, df_pts, tol_metros=30):
    if df_pts is None or df_pts.empty: return None
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * np.cos(np.radians(abs(lat)))
    dist2_m = ((df_pts['lon'] - lon) * m_per_deg_lon)**2 + ((df_pts['lat'] - lat) * m_per_deg_lat)**2
    idx_min = dist2_m.idxmin()
    if dist2_m.loc[idx_min] <= (tol_metros ** 2): return df_pts.loc[idx_min]
    return None

def _buscar_feature_por_click(lat, lon, canales_all, catastro_all, tol_metros=25, considerar_canales=True, considerar_catastro=True, priorizar_canal=True):
    lat_abs = abs(lat)
    deg_lat = tol_metros / 111320.0
    deg_lon = tol_metros / (111320.0 * np.cos(np.radians(lat_abs)))
    tol2 = max(deg_lat, deg_lon) ** 2

    def pick_parcela():
        if not (considerar_catastro and catastro_all): return None
        for feat in catastro_all.get('features', []):
            g = feat.get('geometry', {})
            if g.get('type') in ('Polygon', 'MultiPolygon') and _point_in_multipolygon((lon, lat), g.get('coordinates', [])):
                return feat.get('properties', {}).get('fid'), feat.get('properties', {})
        return None

    def pick_canal():
        if not (considerar_canales and canales_all): return None
        best = (None, None, float('inf'))
        for feat in canales_all.get('features', []):
            g = feat.get('geometry', {})
            if g.get('type') in ('LineString', 'MultiLineString'):
                d2 = _min_squared_distance_to_lines(g.get('coordinates', []), lon, lat)
                if d2 < best[2]: best = (feat.get('properties', {}).get('fid'), feat.get('properties', {}), d2)
        return (best[0], best[1]) if best[0] and best[2] <= tol2 else None

    if priorizar_canal:
        res = pick_canal() or pick_parcela()
    else:
        res = pick_parcela() or pick_canal()
    return res if res else (None, None)

# --- ESTADO DE SELECCIÓN ---
if "sel_type" not in st.session_state: st.session_state.sel_type = None
if "sel_data" not in st.session_state: st.session_state.sel_data = None
if "base_layer" not in st.session_state: st.session_state.base_layer = "Satélite"
if "_clear_once" not in st.session_state: st.session_state._clear_once = False

df_historial, df_maestro = cargar_datos_kobo()
df_pluv_meta, df_pluv_pp = cargar_pluviometros_INTAlike()

st.markdown('<div class="titulo-responsive"><span class="emoji">🌊</span> Red de Aforos: San Ramón - Las Pircas</div>', unsafe_allow_html=True)

if not df_maestro.empty:
    with st.sidebar:
        st.markdown('<div class="ficha-header">MAPA BASE</div>', unsafe_allow_html=True)
        st.session_state.base_layer = st.radio(label="", options=["Satélite", "OSM"], index=0 if st.session_state.base_layer=="Satélite" else 1, horizontal=True)
        st.markdown('<div class="ficha-header">CAPAS</div>', unsafe_allow_html=True)
        show_canales = st.checkbox("Canales", value=True)
        show_catastro = st.checkbox("Catastro", value=False)
        show_aforadores = st.checkbox("Aforadores", value=True)
        show_pluviometros = st.checkbox("Pluviómetros (INTA)", value=True)

    # --- CARGA OPTIMIZADA DE GEOJSON ---
    canales_all = obtener_geojson_local("canales.geojson", "canales")
    catastro_all = obtener_geojson_local("catastro.geojson", "catastro")

    m = folium.Map(location=[df_maestro['lat'].mean(), df_maestro['lon'].mean()], zoom_start=13, tiles=None)

    if st.session_state.base_layer == "Satélite":
        folium.TileLayer('https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}', attr='Google', name='Satélite').add_to(m)
    else:
        folium.TileLayer('OpenStreetMap', name='OSM').add_to(m)

    folium.map.CustomPane('pane_catastro', z_index=300).add_to(m)
    folium.map.CustomPane('pane_canales',  z_index=400).add_to(m)
    folium.map.CustomPane('pane_markers',  z_index=650).add_to(m)

    if show_catastro and catastro_all:
        folium.GeoJson(catastro_all, style_function=lambda x: {'color': '#E67E22', 'weight': 1, 'fillColor': '#F39C12', 'fillOpacity': 0.12}, highlight_function=lambda x: {'weight': 3, 'color': 'red', 'fillOpacity': 0.45}, pane='pane_catastro').add_to(m)

    if show_canales and canales_all:
        folium.GeoJson(canales_all, style_function=lambda f: {'color': get_color_sistema(f['properties'].get('sistema')), 'weight': 4, 'opacity': 0.85}, highlight_function=lambda x: {'weight': 8, 'color': 'yellow', 'opacity': 1}, pane='pane_canales').add_to(m)

    if show_aforadores:
        markers_fg = folium.FeatureGroup(name='Aforadores', overlay=True, pane='pane_markers')
        for _, p in df_maestro.iterrows():
            u3 = df_historial[df_historial['af_actual'] == p['id_aforador']].sort_values('fecha_dt', ascending=False).head(3)
            filas_html = "".join([f"<tr><td style='padding:2px 4px;'>{r['fecha_format']}</td><td style='padding:2px 4px;'>{r['hora_format']}</td><td style='padding:2px 4px; text-align:right;'><b>{r['caudal']} l/s</b></td></tr>" for _, r in u3.iterrows()])
            pop_html = f"<div style='width:220px; font-family:sans-serif;'><div style='background:#1E3A8A; color:white; padding:5px; text-align:center; font-weight:bold;'>{p['Aforador']}</div><table style='width:100%; font-size:11px; margin-top:5px; border-collapse:collapse;'><thead><tr><th style='text-align:left; padding:2px 4px;'>Fecha</th><th style='text-align:left; padding:2px 4px;'>Hora</th><th style='text-align:right; padding:2px 4px;'>Caudal</th></tr></thead><tbody>{filas_html}</tbody></table></div>"
            folium.Marker([p['lat'], p['lon']], popup=folium.Popup(pop_html, max_width=240), tooltip=p['Aforador'], icon=folium.Icon(color='blue', icon='tint', prefix='fa')).add_to(markers_fg)
        markers_fg.add_to(m)

    if show_pluviometros and not df_pluv_meta.empty:
        fg_pluv = folium.FeatureGroup(name='Pluviómetros INTA', overlay=True, pane='pane_markers')
        for _, row in df_pluv_meta.iterrows():
            u3 = df_pluv_pp[df_pluv_pp['cod'] == row['cod']].sort_values('fecha_dt', ascending=False).head(3)
            filas = "".join([f"<tr><td style='padding:2px 4px;'>{r['fecha_dt'].strftime('%d/%m/%Y')}</td><td style='padding:2px 4px; text-align:right;'><b>{round(float(r['mm']), 1)} mm</b></td></tr>" for _, r in u3.iterrows()])
            pop_html = f"<div style='width:260px; font-family:sans-serif;'><div style='background:#0E7490; color:white; padding:6px; text-align:center; font-weight:bold;'>🌧️ {row['nombre_visible']}</div><div style='margin-top:6px;'><table style='width:100%; font-size:11px; border-collapse:collapse;'><thead><tr><th style='text-align:left; padding:2px 4px;'>Fecha</th><th style='text-align:right; padding:2px 4px;'>Lluvia</th></tr></thead><tbody>{filas}</tbody></table></div></div>"
            folium.Marker([row['lat'], row['lon']], popup=folium.Popup(pop_html, max_width=300), tooltip=f"Pluviómetro - {row['nombre_visible']}", icon=folium.Icon(color='green', icon='cloud', prefix='fa')).add_to(fg_pluv)
        fg_pluv.add_to(m)

    LocateControl(position='topleft').add_to(m)

    # --- RENDER OPTIMIZADO ---
    salida = st_folium(
        m, width="100%", height=600, key="mapa_estatico", # Key fija para evitar refresco total
        returned_objects=["last_clicked", "last_object_clicked"],
        use_container_width=True
    )

    if st.session_state._clear_once:
        st.session_state._clear_once = False
        clic = None
    else:
        clic = salida.get("last_object_clicked") or salida.get("last_clicked")

    if clic and isinstance(clic, dict) and {'lat', 'lng'} <= clic.keys():
        lat, lon = clic['lat'], clic['lng']
        match_m = _buscar_marker_por_click(lat, lon, df_maestro if show_aforadores else df_maestro.iloc[0:0])
        if match_m is not None:
            st.session_state.sel_type, st.session_state.sel_data = 'marker', match_m.to_dict()
        else:
            fid, props = _buscar_feature_por_click(lat, lon, canales_all if show_canales else None, catastro_all if show_catastro else None, considerar_canales=show_canales, considerar_catastro=show_catastro)
            if fid: st.session_state.sel_type, st.session_state.sel_data = 'geojson', {"fid": fid, "props": props}
            else: st.session_state.sel_type, st.session_state.sel_data = None, None

    with st.sidebar:
        st.markdown('<div class="ficha-header">DETALLE DEL ELEMENTO</div>', unsafe_allow_html=True)
        if st.button("🧹 Limpiar selección"):
            st.session_state.sel_type, st.session_state.sel_data, st.session_state._clear_once = None, None, True
            st.rerun()

        if st.session_state.sel_type == 'marker' and st.session_state.sel_data:
            sel = st.session_state.sel_data
            st.subheader(f"📍 {sel.get('Aforador')}")
            st.write(f"**Tipo:** {sel.get('Tipo_fmt')}")
            st.markdown("---")
            u5 = df_historial[df_historial['af_actual'] == sel.get('id_aforador')].sort_values('fecha_dt', ascending=False).head(5)
            for _, r in u5.iterrows():
                st.markdown(f'<div class="metric-box">📅 {r["fecha_format"]} {r["hora_format"]}<br><b>Caudal: {r["caudal"]} l/s</b></div>', unsafe_allow_html=True)
        elif st.session_state.sel_type == 'geojson' and st.session_state.sel_data:
            p = st.session_state.sel_data['props']
            if st.session_state.sel_data['fid'].startswith("canal_"):
                st.subheader(f"🌊 {p.get('tipo', 'N/A')}: {p.get('nombre', 'Sin nombre')}")
                st.markdown(f"- **Sistema:** {p.get('sistema', 'N/A')}\n- **Longitud:** {p.get('longi', '0')} m")
            else:
                area_ha = round(float(p.get('shape_area', 0)) / 10000, 2)
                st.subheader(f"🚜 Parcela: {p.get('finca', 'S/N')}")
                st.markdown(f"- **Catastro:** {p.get('catastro', 'N/A')}\n- **Área:** {area_ha} Ha")
        else:
            st.info("💡 Haz clic en un elemento del mapa.")

    if not df_historial.empty:
        st.markdown("---")
        with st.expander("📊 Gráficos y Comparativas"):
            af_sel = st.selectbox("Elegir punto de control:", options=sorted(df_maestro['Aforador'].unique()))
            id_sel = df_maestro[df_maestro['Aforador'] == af_sel]['id_aforador'].values[0]
            df_plot = df_historial[df_historial['af_actual'] == id_sel].sort_values('fecha_dt')
            if not df_plot.empty: st.area_chart(df_plot.set_index('fecha_dt')['caudal'])
else:
    st.error("No se pudieron cargar los datos de Kobo.")
