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

# 2) ESTILOS (título responsive + headers + métricas)
st.markdown("""
<style>
    /* Título adaptable con límites claros */
    .titulo-responsive {
        text-align: center;
        color: #1E3A8A;
        font-weight: bold;
        padding: 10px;
        /* Tamaño responsivo: min 1.15rem, preferido 2.5vw + 0.6rem, max 2rem */
        font-size: clamp(1.15rem, 2.5vw + 0.6rem, 2rem);
        line-height: 1.2;
        border-bottom: 2px solid #1E3A8A;
        margin-bottom: 16px; /* más compacto para móviles */
        word-wrap: break-word;
    }

    .titulo-responsive .emoji {
        font-size: 1.1em;
        vertical-align: -2px;
        margin-right: .4rem;
    }

    @media (max-width: 768px) {
        .titulo-responsive {
            font-size: clamp(1.05rem, 2.2vw + 0.5rem, 1.6rem);
            padding: 8px;
            margin-bottom: 14px;
        }
        .titulo-responsive .emoji {
            font-size: 1.05em;
            vertical-align: -1px;
        }
    }

    @media (max-width: 480px) {
        .titulo-responsive {
            font-size: clamp(1rem, 2vw + 0.5rem, 1.35rem);
            padding: 6px;
            margin-bottom: 12px;
            border-bottom-width: 1px;
        }
        .titulo-responsive .emoji {
            font-size: 1em;
            margin-right: .35rem;
            vertical-align: 0;
        }
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

# 3) CARGA DE DATOS
@st.cache_data(ttl=300)
def cargar_datos_kobo():
    URL_AFORO = "https://kf.kobotoolbox.org/api/v2/assets/adRKxesyy7hBQNQbNVCtdt/data.json?limit=1000&ordering=-_submission_time"
    URL_MAPA  = "https://kf.kobotoolbox.org/api/v2/assets/and5RtS5yp74muGFDddySr/data.json?limit=1000"
    
    
    TOKEN = st.secrets["AFORO_TOKEN"]
    HEADERS = {'Authorization': f'Token {TOKEN}'}
    try:
        r1 = requests.get(URL_AFORO, headers=HEADERS)
        r2 = requests.get(URL_MAPA, headers=HEADERS)
        df_a = pd.DataFrame(r1.json().get('results', []))
        df_m = pd.DataFrame(r2.json().get('results', []))

        if df_m.empty:
            return pd.DataFrame(), pd.DataFrame()

        # Limpieza de columnas y creación de campos
        df_m.columns = [c.strip() for c in df_m.columns]
        col_sistema = next((c for c in df_m.columns if 'sistema' in c.lower()), None)
        df_m['Sistema_Interno'] = df_m[col_sistema] if col_sistema else "General"
        df_m['id_aforador'] = df_m['Codigo_del_aforador_texto'].astype(str).str.strip()

        # --- Tipo: tomado del Kobo (columna "Tipo") y normalizado a la leyenda solicitada
        if 'Tipo' in df_m.columns:
            df_m['Tipo'] = df_m['Tipo'].astype(str).str.strip()
        else:
            df_m['Tipo'] = 'N/D'

        # Mapeo solicitado:
        #  a50x210 -> Aforador Flume 50x210 cm
        #  a20x90  -> Aforador Flume 20x90 cm
        mapa_tipo = {
            'a50x210': 'Aforador Flume 50x210 cm',
            'a20x90': 'Aforador Flume 20x90 cm'
        }
        # Creamos una versión formateada (por si querés mantener el original)
        df_m['Tipo_fmt'] = df_m['Tipo'].map(mapa_tipo).fillna(df_m['Tipo'])

        # Extraer coordenadas (en Kobo suelen venir como "lat lon")
        def ext_coords(v):
            try:
                p = v.split()
                return float(p[0]), float(p[1])
            except:
                return None, None

        df_m['lat'], df_m['lon'] = zip(*df_m['Ubicaci_n'].apply(ext_coords))
        df_m = df_m.dropna(subset=['lat', 'lon'])

        # Historial
        if not df_a.empty:
            df_a['af_actual'] = df_a['af_actual'].astype(str).str.strip()
            df_a['fecha_dt']  = pd.to_datetime(df_a['Fecha'] + ' ' + df_a['Hora'], errors='coerce')
            df_a['caudal']    = pd.to_numeric(df_a['q_final'], errors='coerce').fillna(0).astype(int)
            df_a['fecha_format'] = df_a['fecha_dt'].dt.strftime('%d/%m')
            df_a['hora_format']  = df_a['fecha_dt'].dt.strftime('%H:%M')

        return df_a, df_m
    except Exception:
        return pd.DataFrame(), pd.DataFrame()

def get_color_sistema(sistema):
    colores = {
        "San Ramón - Las Pircas": "#FF5733",
        "Santos Lugares": "#2ECC71",
        "Las Ceibas": "#3498DB",
        "El Mollar": "#F333FF",
        "El Pedregal": "#000000"
    }
    return colores.get(sistema, "#808080")

# ---- Helpers para preparar GeoJSON (IDs)
def preparar_canales(data):
    for i, feature in enumerate(data.get('features', [])):
        p = feature.setdefault('properties', {})
        p['fid'] = p.get('fid', f"canal_{i}")
    return data

def preparar_catastro(data):
    for i, feature in enumerate(data.get('features', [])):
        p = feature.setdefault('properties', {})
        p['fid'] = p.get('fid', f"finca_{i}")
    return data

# ---- Geometría básica (sin dependencias externas)
def _point_in_polygon(pt, polygon):
    """Ray casting: pt=(lon,lat); polygon=[(lon,lat), ...]"""
    x, y = pt
    inside = False
    for i in range(len(polygon)):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % len(polygon)]
        cond = ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1)
        if cond:
            inside = not inside
    return inside

def _point_in_multipolygon(pt, coords):
    """coords = GeoJSON coords of Polygon or MultiPolygon (lon,lat) con agujeros"""
    def poly_contains(pt, poly):
        exterior = poly[0]
        if not _point_in_polygon(pt, exterior):
            return False
        for hole in poly[1:]:
            if _point_in_polygon(pt, hole):
                return False
        return True

    # Detectar Polygon vs MultiPolygon
    if len(coords) > 0 and isinstance(coords[0][0][0], (float, int)):
        # Polygon
        return poly_contains(pt, coords)
    else:
        # MultiPolygon
        for poly in coords:
            if poly_contains(pt, poly):
                return True
        return False

def _squared_distance_point_to_segment(px, py, x1, y1, x2, y2):
    """Distancia al cuadrado de punto a segmento (en grados)."""
    vx, vy = x2 - x1, y2 - y1
    wx, wy = px - x1, py - y1
    c1 = vx * wx + vy * wy
    if c1 <= 0:
        return (px - x1)**2 + (py - y1)**2
    c2 = vx * vx + vy * vy
    if c2 <= c1:
        return (px - x2)**2 + (py - y2)**2
    b = c1 / (c2 + 1e-12)
    bx, by = x1 + b * vx, y1 + b * vy
    return (px - bx)**2 + (py - by)**2

def _min_squared_distance_to_lines(coords, px, py):
    """coords: LineString [[x,y],...] o MultiLineString [[[x,y],...],...] (en grados lon/lat)."""
    def line_dist(line):
        dmin = float('inf')
        for i in range(len(line) - 1):
            x1, y1 = line[i]
            x2, y2 = line[i + 1]
            d = _squared_distance_point_to_segment(px, py, x1, y1, x2, y2)
            if d < dmin:
                dmin = d
        return dmin

    # LineString o MultiLineString
    if len(coords) > 0 and isinstance(coords[0][0], (float, int)):
        return line_dist(coords)
    else:
        dmin = float('inf')
        for line in coords:
            d = line_dist(line)
            if d < dmin:
                dmin = d
        return dmin

# ---- Buscar marcador (aforador) por proximidad
def _buscar_marker_por_click(lat, lon, df_pts, tol_metros=30):
    """
    Devuelve la fila del aforador más cercano al click si está dentro de tol_metros (≈30 m).
    Distancia aproximada en metros (corrigiendo por latitud).
    """
    if df_pts is None or df_pts.empty:
        return None

    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(np.cos(np.radians(abs(lat))), 1e-6)

    dx_deg = df_pts['lon'].astype(float) - float(lon)
    dy_deg = df_pts['lat'].astype(float) - float(lat)

    dist2_m = (dx_deg * m_per_deg_lon)**2 + (dy_deg * m_per_deg_lat)**2
    if dist2_m.empty:
        return None

    idx_min = dist2_m.idxmin()
    if np.isfinite(dist2_m.loc[idx_min]) and dist2_m.loc[idx_min] <= (tol_metros ** 2):
        return df_pts.loc[idx_min]
    return None

# ---- Buscar canal/catastro por clic (prioriza canal) con tol_metros=25 para líneas
def _buscar_feature_por_click(
    lat, lon,
    canales_all, catastro_all,
    tol_metros=25,
    considerar_canales=True,
    considerar_catastro=True,
    priorizar_canal=True
):
    """
    Devuelve (fid, props) a partir de un clic (lat, lon) sin popups.
    """
    # convertir tolerancia a grados aproximados (lat/lon)
    lat_abs = abs(lat)
    deg_lat = tol_metros / 111_320.0
    deg_lon = tol_metros / (111_320.0 * max(np.cos(np.radians(lat_abs)), 1e-6))
    tol2 = max(deg_lat, deg_lon) ** 2  # grados^2

    def pick_parcela():
        if not (considerar_catastro and catastro_all):
            return None
        for feat in catastro_all.get('features', []):
            g = feat.get('geometry', {})
            if g.get('type') in ('Polygon', 'MultiPolygon'):
                if _point_in_multipolygon((lon, lat), g.get('coordinates', [])):
                    p = feat.get('properties', {})
                    return p.get('fid'), p
        return None

    def pick_canal():
        if not (considerar_canales and canales_all):
            return None
        best = (None, None, float('inf'))  # (fid, props, dist2_en_grados)
        for feat in canales_all.get('features', []):
            g = feat.get('geometry', {})
            if g.get('type') in ('LineString', 'MultiLineString'):
                d2 = _min_squared_distance_to_lines(g.get('coordinates', []), lon, lat)
                if d2 < best[2]:
                    p = feat.get('properties', {})
                    best = (p.get('fid'), p, d2)
        if best[0] is not None and best[2] <= tol2:
            return best[0], best[1]
        return None

    if priorizar_canal:
        cand = pick_canal()
        if cand: return cand
        cand = pick_parcela()
        if cand: return cand
    else:
        cand = pick_parcela()
        if cand: return cand
        cand = pick_canal()
        if cand: return cand
    return None, None

# ---- ESTADO DE SELECCIÓN
if "sel_type" not in st.session_state:
    st.session_state.sel_type = None
if "sel_data" not in st.session_state:
    st.session_state.sel_data = None
if "base_layer" not in st.session_state:
    st.session_state.base_layer = "Satélite"
if "_clear_once" not in st.session_state:
    st.session_state._clear_once = False

# Cargar datos
df_historial, df_maestro = cargar_datos_kobo()

# 4) TÍTULO
st.markdown(
    '<div class="titulo-responsive"><span class="emoji">🌊</span> Red de Aforos: San Ramón - Las Pircas</div>',
    unsafe_allow_html=True
)

if not df_maestro.empty:
    # 5) SIDEBAR: CONTROLES
    with st.sidebar:
        st.markdown('<div class="ficha-header">CAPAS Y MAPA</div>', unsafe_allow_html=True)
        st.session_state.base_layer = st.radio("Fondo:", ["Satélite", "OSM"], index=0 if st.session_state.base_layer=="Satélite" else 1, horizontal=True)
        show_canales = st.checkbox("Ver Canales", value=True)
        show_catastro = st.checkbox("Ver Catastro", value=False)
        show_aforadores = st.checkbox("Ver Aforadores", value=True)

    # 6) Cargar GeoJSON locales
    canales_all = preparar_canales(json.load(open("canales.geojson", encoding='utf-8'))) if os.path.exists("canales.geojson") else None
    catastro_all = preparar_catastro(json.load(open("catastro.geojson", encoding='utf-8'))) if os.path.exists("catastro.geojson") else None

    # 7) MAPA
    m = folium.Map(
        location=[df_maestro['lat'].mean(), df_maestro['lon'].mean()],
        zoom_start=13,
        tiles=None
    )

    # Base Layer
    if st.session_state.base_layer == "Satélite":
        folium.TileLayer('https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}', attr='Google', name='Satélite').add_to(m)
    else:
        folium.TileLayer('OpenStreetMap', name='OSM').add_to(m)

    # Panes (Z-Index)
    folium.map.CustomPane('pane_catastro', z_index=300).add_to(m)
    folium.map.CustomPane('pane_canales',  z_index=400).add_to(m)
    folium.map.CustomPane('pane_markers',  z_index=650).add_to(m)

    # Catastro
    if show_catastro and catastro_all:
        folium.GeoJson(
            catastro_all,
            style_function=lambda x: {'color': '#E67E22', 'weight': 1, 'fillColor': '#F39C12', 'fillOpacity': 0.12},
            highlight_function=lambda x: {'weight': 3, 'color': 'red', 'fillOpacity': 0.45},
            pane='pane_catastro'
        ).add_to(m)

    # Canales
    if show_canales and canales_all:
        folium.GeoJson(
            canales_all,
            style_function=lambda f: {
                'color': get_color_sistema(f['properties'].get('sistema')),
                'weight': 4, 'opacity': 0.85
            },
            highlight_function=lambda x: {'weight': 8, 'color': 'yellow', 'opacity': 1},
            pane='pane_canales'
        ).add_to(m)

    # Aforadores (markers)
    if show_aforadores:
        markers_fg = folium.FeatureGroup(name='Aforadores', overlay=True, pane='pane_markers')
        for _, p in df_maestro.iterrows():
            # Popup con historial 3 últimos
            u3 = (
                df_historial[df_historial['af_actual'] == p['id_aforador']]
                .sort_values('fecha_dt', ascending=False)
                .head(3)
            )

            filas_html = "".join([
                (
                    f"<tr>"
                    f"<td style='padding:2px 4px;'>{r['fecha_format']}</td>"
                    f"<td style='padding:2px 4px;'>{r['hora_format']}</td>"
                    f"<td style='padding:2px 4px; text-align:right;'><b>{r['caudal']} l/s</b></td>"
                    f"</tr>"
                )
                for _, r in u3.iterrows()
            ])

            pop_html = (
                f"<div style='width:220px; font-family:sans-serif;'>"
                f"<div style='background:#1E3A8A; color:white; padding:5px; text-align:center; font-weight:bold;'>{p['Aforador']}</div>"
                f"<table style='width:100%; font-size:11px; margin-top:5px; border-collapse:collapse;'>"
                f"<thead><tr>"
                    f"<th style='text-align:left; padding:2px 4px;'>Fecha</th>"
                    f"<th style='text-align:left; padding:2px 4px;'>Hora</th>"
                    f"<th style='text-align:right; padding:2px 4px;'>Caudal</th>"
                f"</tr></thead>"
                f"<tbody>{filas_html}</tbody>"
                f"</table>"
                f"</div>"
            )
            folium.Marker(
                [p['lat'], p['lon']],
                popup=folium.Popup(pop_html, max_width=240),
                tooltip=p['Aforador'],
                icon=folium.Icon(color='blue', icon='tint', prefix='fa')
            ).add_to(markers_fg)
        markers_fg.add_to(m)

    LocateControl(position='topleft').add_to(m)

    # 8) RENDER y CAPTURA DE CLIC
    salida = st_folium(
        m, width="100%", height=600, key="mapa_final",
        returned_objects=["last_clicked", "last_object_clicked"]
    )

    # 9) PROCESAMIENTO DE CLIC
    if st.session_state._clear_once:
        st.session_state._clear_once = False
        clic = None
    else:
        clic = salida.get("last_object_clicked") or salida.get("last_clicked")

    if clic and isinstance(clic, dict) and {'lat', 'lng'} <= clic.keys():
        lat, lon = clic['lat'], clic['lng']

        match_m = _buscar_marker_por_click(lat, lon, df_maestro if show_aforadores else df_maestro.iloc[0:0], tol_metros=30)
        if match_m is not None:
            st.session_state.sel_type = 'marker'
            st.session_state.sel_data = match_m.to_dict()
        else:
            fid, props = _buscar_feature_por_click(
                lat, lon,
                canales_all if show_canales else None,
                catastro_all if show_catastro else None,
                tol_metros=25,
                considerar_canales=show_canales,
                considerar_catastro=show_catastro,
                priorizar_canal=True
            )
            if fid:
                st.session_state.sel_type = 'geojson'
                st.session_state.sel_data = {"fid": fid, "props": props}
            else:
                st.session_state.sel_type = None
                st.session_state.sel_data = None

    # 10) SIDEBAR: DETALLE + Limpiar
    with st.sidebar:
        st.markdown('<div class="ficha-header">DETALLE DEL ELEMENTO</div>', unsafe_allow_html=True)

        if st.button("🧹 Limpiar selección"):
            st.session_state.sel_type = None
            st.session_state.sel_data = None
            st.session_state._clear_once = True
            st.rerun()

        if st.session_state.sel_type == 'marker' and st.session_state.sel_data:
            sel = st.session_state.sel_data
            st.subheader(f"📍 {sel.get('Aforador', 'Aforador')}")
            # Mostrar Tipo normalizado (usar Tipo_fmt si está, si no, Tipo)
            st.write(f"**Tipo:** {sel.get('Tipo_fmt', sel.get('Tipo', 'N/D'))}")
            st.markdown("---")
            st.write("**Historial Reciente (5):**")
            u5 = df_historial[df_historial['af_actual'] == sel.get('id_aforador', '')].sort_values('fecha_dt', ascending=False).head(5)
            if not u5.empty:
                for _, r in u5.iterrows():
                    st.markdown(
                        f'<div class="metric-box">📅 {r["fecha_format"]} {r["hora_format"]}<br><b>Caudal: {r["caudal"]} l/s</b></div>',
                        unsafe_allow_html=True
                    )
            else:
                st.info("Sin registros de caudal.")
        elif st.session_state.sel_type == 'geojson' and st.session_state.sel_data:
            data = st.session_state.sel_data
            p = data['props']
            if data['fid'].startswith("canal_"):
                st.subheader(f"🌊 Canal: {p.get('nombre', 'Sin nombre')}")
                st.markdown(f"""
- **Sistema:** {p.get('sistema', 'N/A')}
- **Longitud:** {p.get('longi', '0')} m
- **Tipo:** {p.get('tipo', 'N/A')}
- **Ref.:** {p.get('pu_priv', 'N/A')}
                """)
            else:
                area_raw = p.get('shape_area', 0) or 0
                try:
                    area_ha = round(float(area_raw) / 10000, 2)
                except:
                    area_ha = 0.0
                st.subheader(f"🚜 Parcela: {p.get('finca', 'S/N')}")
                st.markdown(f"""
- **Catastro:** {p.get('catastro', 'N/A')}
- **Área:** {area_ha} Ha
                """)
        else:
            st.info("💡 Haz clic en un aforador, canal o parcela en el mapa para ver su ficha técnica.")

    # 11) ANÁLISIS HISTÓRICO
    if not df_historial.empty:
        st.markdown("---")
        with st.expander("📊 Gráficos y Comparativas"):
            af_sel = st.selectbox("Elegir punto de control:", options=sorted(df_maestro['Aforador'].unique()))
            id_sel = df_maestro[df_maestro['Aforador'] == af_sel]['id_aforador'].values[0]
            df_plot = df_historial[df_historial['af_actual'] == id_sel].sort_values('fecha_dt')
            if not df_plot.empty:
                st.area_chart(df_plot.set_index('fecha_dt')['caudal'])
else:
    st.error("No se pudieron cargar los datos de Kobo.")
