import streamlit as st
import pandas as pd
import requests
import folium
import json
import os
from streamlit_folium import st_folium

# 1. CONFIGURACIÓN DE PÁGINA
st.set_page_config(
    page_title="Consorcio San Ramón - Las Pircas", 
    page_icon="🌊", 
    layout="wide"
)

try:
    locale.setlocale(locale.LC_TIME, "es_AR.UTF-8")
except:
    pass


URL_AFORO = "https://kf.kobotoolbox.org/api/v2/assets/adRKxesyy7hBQNQbNVCtdt/data.json"
URL_MAPA = "https://kf.kobotoolbox.org/api/v2/assets/and5RtS5yp74muGFDddySr/data.json"
TOKEN = st.secrets["AFORO_TOKEN"]

HEADERS = {'Authorization': f'Token {TOKEN}'}
PATH_CANALES = "canales.geojson"
PATH_CATASTRO = "catastro.geojson"

# --- ESTILOS ---
st.markdown("""
    <style>
    .main-title { font-size: 2.2rem; font-weight: bold; color: #1E3A8A; text-align: center; padding: 10px; border-bottom: 2px solid #1E3A8A; margin-bottom: 20px; }
    .leaflet-popup-content { font-family: 'Arial', sans-serif; font-size: 13px; }
    [data-testid="stSidebar"] { display: none; }
    </style>
    """, unsafe_allow_html=True)

@st.cache_data(ttl=300)
def cargar_datos_kobo():
    try:
        r1 = requests.get(URL_AFORO, headers=HEADERS)
        r2 = requests.get(URL_MAPA, headers=HEADERS)
        df_a = pd.DataFrame(r1.json().get('results', []))
        df_m = pd.DataFrame(r2.json().get('results', []))
        if df_m.empty: return pd.DataFrame(), pd.DataFrame()
        df_m['id_aforador'] = df_m['Codigo_del_aforador_texto'].astype(str).str.strip()
        def ext_coords(v):
            try:
                p = v.split()
                return float(p[0]), float(p[1])
            except: return None, None
        df_m['lat'], df_m['lon'] = zip(*df_m['Ubicaci_n'].apply(ext_coords))
        df_m = df_m.dropna(subset=['lat', 'lon'])
        if not df_a.empty:
            df_a['af_actual'] = df_a['af_actual'].astype(str).str.strip()
            df_a['fecha_dt'] = pd.to_datetime(df_a['Fecha'] + ' ' + df_a['Hora'], errors='coerce')
            df_a['caudal'] = pd.to_numeric(df_a['q_final'], errors='coerce').fillna(0).astype(int)
            df_a['fecha_format'] = df_a['fecha_dt'].dt.strftime('%d/%m')
            df_a['hora_format'] = df_a['fecha_dt'].dt.strftime('%H:%M')
        return df_a, df_m
    except: return pd.DataFrame(), pd.DataFrame()

df_historial, df_maestro = cargar_datos_kobo()

def get_color_sistema(sistema):
    colores = {"San Ramón - Las Pircas": "#FF5733", "Santos Lugares": "#2ECC71", "Las Ceibas": "#3498DB", "El Mollar": "#F333FF", "El Pedregal": "#000000"}
    return colores.get(sistema, "#808080")

st.markdown('<div class="main-title">🌊 Red de Aforos Consorcio San Ramón - Las Pircas</div>', unsafe_allow_html=True)

if not df_maestro.empty:
    m = folium.Map(location=[df_maestro['lat'].mean(), df_maestro['lon'].mean()], zoom_start=13, tiles=None)
    folium.TileLayer('https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}', attr='Google Satélite', name='Satélite', overlay=False).add_to(m)
    folium.TileLayer('OpenStreetMap', name='Mapa Calles (OSM)', overlay=False).add_to(m)

    # --- CAPA CANALES (Optimizada) ---
    if os.path.exists(PATH_CANALES):
        with open(PATH_CANALES, encoding='utf-8') as f:
            canales_data = json.load(f)
        
        # Inyectamos el HTML del popup en una propiedad nueva para cada canal
        for feature in canales_data['features']:
            p = feature['properties']
            p['html_content'] = f"""
                <div style="width:180px;">
                    <div style="background:#1E3A8A; color:white; padding:5px; text-align:center; font-weight:bold;">{p.get('nombre', 'Canal')}</div>
                    <table style="width:100%; font-size:11px; margin-top:5px;">
                        <tr><td><b>Sistema:</b></td><td style="text-align:right;">{p.get('sistema','-')}</td></tr>
                        <tr><td><b>Longitud:</b></td><td style="text-align:right;">{p.get('longi','-')} m</td></tr>
                    </table>
                </div>"""

        folium.GeoJson(
            canales_data,
            name="Red de Canales",
            style_function=lambda f: {'color': get_color_sistema(f['properties'].get('sistema')), 'weight': 4, 'opacity': 0.8},
            popup=folium.GeoJsonPopup(fields=['html_content'], labels=False)
        ).add_to(m)

    # --- CAPA CATASTRO (Optimizada) ---
    if os.path.exists(PATH_CATASTRO):
        with open(PATH_CATASTRO, encoding='utf-8') as f:
            catastro_data = json.load(f)
            
        for feature in catastro_data['features']:
            p = feature['properties']
            area_ha = round(p.get('shape_area', 0) / 10000, 2)
            p['html_content'] = f"""
                <div style="width:180px;">
                    <div style="background:#E67E22; color:white; padding:5px; text-align:center; font-weight:bold;">Finca: {p.get('finca', 'S/N')}</div>
                    <table style="width:100%; font-size:11px; margin-top:5px;">
                        <tr><td><b>Catastro:</b></td><td style="text-align:right;">{p.get('catastro','-')}</td></tr>
                        <tr><td><b>Superficie:</b></td><td style="text-align:right;">{area_ha} Ha</td></tr>
                    </table>
                </div>"""

        folium.GeoJson(
            catastro_data,
            name="Catastro Parcelario",
            show=False,
            style_function=lambda x: {'color': '#E67E22', 'weight': 1, 'fillColor': '#F39C12', 'fillOpacity': 0.1},
            popup=folium.GeoJsonPopup(fields=['html_content'], labels=False)
        ).add_to(m)

    # --- MARCADORES AFORADORES ---
    for _, punto in df_maestro.iterrows():
        cod = punto['id_aforador']
        ultimos_3 = df_historial[df_historial['af_actual'] == cod].sort_values('fecha_dt', ascending=False).head(3)
        filas_html = "".join([f"<tr><td>{r['fecha_format']}</td><td>{r['hora_format']}</td><td style='text-align:right;'><b>{r['caudal']} l/s</b></td></tr>" for _, r in ultimos_3.iterrows()])
        pop_html = f"""<div style="width:200px;"><div style="background:#1E3A8A; color:white; padding:5px; text-align:center; font-weight:bold;">{punto['Aforador']}</div><table style="width:100%; font-size:11px; margin-top:5px;">{filas_html}</table></div>"""
        folium.Marker([punto['lat'], punto['lon']], popup=folium.Popup(pop_html, max_width=250),
            icon=folium.Icon(color='blue' if not ultimos_3.empty else 'gray', icon='water', prefix='fa'),
            tooltip=punto['Aforador']).add_to(m)

    folium.LayerControl(position='topright', collapsed=False).add_to(m)
    st_folium(m, width="100%", height=700, key="mapa_final", returned_objects=[])
else:
    st.error("No se pudieron cargar los datos.")