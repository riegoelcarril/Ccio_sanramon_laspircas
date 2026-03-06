import streamlit as st
import pandas as pd
import requests
import folium
import json
import os
import numpy as np
from base64 import b64encode
from reportlab.lib.utils import ImageReader
from reportlab.platypus import HRFlowable  # línea horizontal elegante

from datetime import date, timedelta
from streamlit_folium import st_folium
from folium.plugins import LocateControl
from pandas.api.types import is_datetime64tz_dtype
import altair as alt  # para gráficos combinados (lluvia + caudal)
from io import BytesIO
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.platypus import Image as RLImage, Table as RLTable
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader

# ========= LOGO =========
LOGO_PATH = "Logo-SRP-01.png"  # ajustá la ruta si está en /assets/...

def _logo_base64(path: str):
    try:
        with open(path, "rb") as f:
            return b64encode(f.read()).decode("utf-8")
    except Exception:
        return None

# 1) CONFIGURACIÓN
st.set_page_config(page_title="Consorcio San Ramón - Las Pircas", layout="wide",initial_sidebar_state="expanded")

# 2) ESTILOS (IMPORTANTE: <style> REAL, NO ESCAPADO)
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
    /* Para encuadrar el logo cuando se usa Opción A */
    .logo-chip {
        background: #ffffff;
        border-radius: 8px;
        padding: 6px;
        box-shadow: 0 0 0 1px rgba(0,0,0,.08);
        display: inline-block;
    }
    .leaflet-interactive { cursor: pointer !important; }
</style>
""", unsafe_allow_html=True)

# --- OPTIMIZACIÓN: Cache de GeoJSON locales ---
@st.cache_data
def obtener_geojson_local(ruta: str, tipo: str):
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

# --- HELPER: Parser robusto Fecha+Hora de Kobo ---
def parse_datetime_kobo(fecha_s: pd.Series, hora_s: pd.Series) -> pd.Series:
    def is_utcish(s: str) -> bool:
        if not isinstance(s, str):
            return False
        s = s.strip()
        return s.endswith("Z") or ("+" in s) or ("-" in s and ":" in s)

    out = []
    fecha_s = fecha_s if isinstance(fecha_s, pd.Series) else pd.Series(fecha_s)
    hora_s = hora_s if isinstance(hora_s, pd.Series) else pd.Series(hora_s)
    if len(fecha_s) != len(hora_s):
        hora_s = hora_s.reindex(fecha_s.index)

    for f, h in zip(fecha_s.fillna("").astype(str), hora_s.fillna("").astype(str)):
        fh = f"{f.strip()} {h.strip()}".strip()
        if not fh:
            out.append(pd.NaT)
            continue
        if is_utcish(fh):
            try:
                dt = pd.to_datetime(fh, errors="raise", utc=True)
                dt_local = dt.tz_convert("America/Argentina/Buenos_Aires").tz_localize(None)
                out.append(dt_local)
                continue
            except Exception:
                pass
        dt_local2 = pd.to_datetime(fh, dayfirst=True, errors="coerce")
        out.append(dt_local2)
    return pd.to_datetime(pd.Series(out), errors="coerce")

# ---------- PDF: helper reporte Diario/Semanal ----------
def _fmt_int_or_blank(x):
    try:
        if pd.isna(x) or str(x).strip() == "":
            return ""
        return str(int(float(x)))
    except:
        return str(x) if x is not None else ""

def generar_pdf_reporte_datos(df_tab: pd.DataFrame, modo: str, inicio: pd.Timestamp, fin: pd.Timestamp) -> bytes:
    titulo = "Aforos - Reporte " + ("Diario" if modo == "Día" else "Semanal")
    if modo == "Día":
        subtitulo = f"Fecha: {inicio.strftime('%d/%m/%Y')}"
    else:
        subtitulo = f"Semana: {inicio.strftime('%d/%m/%Y')} a {(fin - pd.Timedelta(days=1)).strftime('%d/%m/%Y')}"

    core_cols = ["Orden", "Aforador", "Fecha"]
    pairs = []
    for i in [1, 2, 3]:
        if f"Hora_{i}" in df_tab.columns and f"Caudal_{i} (l/s)" in df_tab.columns:
            pairs.append((f"Hora_{i}", f"Caudal_{i} (l/s)"))

    headers = core_cols[:]
    for _ in pairs:
        headers += ["Hora", "Caudal (l/s)"]

    data = [headers]
    lecturas_totales = 0
    for _, row in df_tab.iterrows():
        lecturas_totales += sum(
            1 for _, c in pairs if pd.notna(row.get(c)) and str(row.get(c)).strip() != ""
        )
        fila = [row.get("Orden", ""), row.get("Aforador", ""), row.get("Fecha", "")]
        for h_col, c_col in pairs:
            fila.append(row.get(h_col, "") or "")
            fila.append(_fmt_int_or_blank(row.get(c_col, "")))
        data.append(fila)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=12*mm, rightMargin=12*mm, topMargin=12*mm, bottomMargin=12*mm
    )
    styles = getSampleStyleSheet()
    elems = []

    # Encabezado con logo (si existe)
    # ===== Encabezado con logo (a la izquierda, sin deformar) =====
    # ===== Encabezado con logo (a la izquierda, sin deformar) + línea separadora =====
    try:
        if os.path.exists(LOGO_PATH):
            # 1) Escalado proporcional por altura (no deforma)
            img_reader = ImageReader(LOGO_PATH)
            iw, ih = img_reader.getSize()  # tamaño real en px
            target_h = 22*mm               # <-- altura objetivo del logo (ajustá si querés)
            scale = target_h / ih
            logo_w = iw * scale
            logo_h = target_h

            logo_flow = RLImage(LOGO_PATH, width=logo_w, height=logo_h)

            # 2) Título + subtítulo a la derecha, logo a la izquierda
            header_tbl = RLTable(
                data=[
                    [logo_flow, Paragraph(f"<b>{titulo}</b>", styles["Title"])],
                    ["",         Paragraph(subtitulo,           styles["Normal"])]
                ],
                colWidths=[logo_w + 6*mm, None]
            )
            header_tbl.setStyle(TableStyle([
                ('SPAN', (0,0), (0,1)),                # Logo ocupa ambas filas
                ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),  # Alineación vertical prolija
                ('ALIGN',  (0,0), (0,1), 'LEFT'),
                ('LEFTPADDING',  (0,0), (-1,-1), 0),
                ('RIGHTPADDING', (0,0), (-1,-1), 6),
                ('TOPPADDING',   (0,0), (-1,-1), 0),
                ('BOTTOMPADDING',(0,0), (-1,-1), 2),
            ]))

            elems.append(header_tbl)
        else:
            elems.append(Paragraph(f"<b>{titulo}</b>", styles["Title"]))
            elems.append(Paragraph(subtitulo, styles["Normal"]))

        # 3) Separador (línea horizontal) debajo del encabezado
        elems.append(Spacer(1, 4))
        elems.append(HRFlowable(
            width="100%",           # ancho total
            thickness=0.8,          # grosor de la línea
            color=colors.Color(0.12, 0.22, 0.54),  # azul institucional
            lineCap='round',
            spaceBefore=2,
            spaceAfter=10
        ))
    except Exception:
        elems.append(Paragraph(f"<b>{titulo}</b>", styles["Title"]))
        elems.append(Paragraph(subtitulo, styles["Normal"]))
        elems.append(Spacer(1, 4))
        elems.append(HRFlowable(width="100%", thickness=0.8, color=colors.Color(0.12, 0.22, 0.54), lineCap='round', spaceBefore=2, spaceAfter=6))

    table = Table(data, repeatRows=1)
    widths = [18*mm, 68*mm, 24*mm]
    for _ in pairs: widths += [20*mm, 26*mm]
    table._argW = widths

    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.Color(0.12, 0.22, 0.54)),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("ALIGN", (0,0), (-1,0), "CENTER"),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 10),

        ("ALIGN", (0,1), (0,-1), "RIGHT"),
        ("ALIGN", (2,1), (2,-1), "CENTER"),
        ("ALIGN", (3,1), (-1,-1), "CENTER"),

        ("FONTSIZE", (0,1), (-1,-1), 9),
        ("BOTTOMPADDING", (0,0), (-1,0), 8),
        ("TOPPADDING", (0,0), (-1,0), 6),

        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.whitesmoke, colors.Color(0.97,0.97,1.0)]),
        ("GRID", (0,0), (-1,-1), 0.3, colors.grey),
    ]))

    elems.append(table)
    elems.append(Spacer(1, 6))
    elems.append(Paragraph(
        f"Generado: {pd.Timestamp.now().strftime('%d/%m/%Y %H:%M')}  •  Filas: {len(df_tab)}  •  Lecturas (no vacías): {lecturas_totales}",
        styles["Italic"]
    ))

    doc.build(elems)
    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes

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
        if df_m.empty:
            return pd.DataFrame(), pd.DataFrame()

        df_m.columns = [c.strip() for c in df_m.columns]
        col_sistema = next((c for c in df_m.columns if 'sistema' in c.lower()), None)
        df_m['Sistema_Interno'] = df_m[col_sistema] if col_sistema else "General"

        if 'Codigo_del_aforador_texto' in df_m.columns:
            df_m['id_aforador'] = df_m['Codigo_del_aforador_texto'].astype(str).str.strip()
        else:
            df_m['id_aforador'] = df_m.iloc[:, 0].astype(str).str.strip()

        df_m['Tipo'] = df_m['Tipo'].astype(str).str.strip() if 'Tipo' in df_m.columns else 'N/D'
        mapa_tipo = {'a50x210': 'Aforador Flume 50x210 cm', 'a20x90':  'Aforador Flume 20x90 cm'}
        df_m['Tipo_fmt'] = df_m['Tipo'].map(mapa_tipo).fillna(df_m['Tipo'])

        def ext_coords(v):
            try:
                p = str(v).strip().split()
                lat = float(p[0].replace(',', '.'))
                lon = float(p[1].replace(',', '.'))
                return lat, lon
            except:
                return None, None

        if 'Ubicaci_n' in df_m.columns:
            df_m['lat'], df_m['lon'] = zip(*df_m['Ubicaci_n'].apply(ext_coords))
        else:
            df_m['lat'], df_m['lon'] = (np.nan, np.nan)
        df_m = df_m.dropna(subset=['lat', 'lon'])

        if not df_a.empty:
            df_a['af_actual'] = df_a.get('af_actual', '').astype(str).str.strip()

            fecha_s = df_a.get('Fecha', pd.Series(index=df_a.index, dtype=str))
            hora_s  = df_a.get('Hora',  pd.Series(index=df_a.index, dtype=str))
            df_a['fecha_dt'] = parse_datetime_kobo(fecha_s, hora_s)

            df_a['caudal'] = pd.to_numeric(df_a.get('q_final', 0), errors='coerce').fillna(0).astype(int)
            df_a['fecha_format'] = df_a['fecha_dt'].dt.strftime('%d/%m/%Y')
            df_a['hora_format']  = df_a['fecha_dt'].dt.strftime('%H:%M')

        return df_a, df_m
    except Exception:
        return pd.DataFrame(), pd.DataFrame()

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
    except:
        return None, None
    return None, None

@st.cache_data(ttl=10800)
def cargar_pluviometros_INTAlike():
    try:
        r_p = requests.get(URL_PRECIPITACIONES, headers=HEADERS_INTA, timeout=25)
        r_c = requests.get(URL_MAPA_P,        headers=HEADERS_INTA, timeout=25)
        df_p, df_c = pd.DataFrame(r_p.json()), pd.DataFrame(r_c.json())
        if df_p.empty or df_c.empty:
            return pd.DataFrame(), pd.DataFrame()

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

        if col_nombre and col_nombre in df_meta_2.columns:
            df_meta_2['nombre_visible'] = df_meta_2[col_nombre]
        else:
            mapa_nombres = {'aurelia': 'La Aurelia', 'lasceibas': 'Finca Las Ceibas'}
            df_meta_2['nombre_visible'] = df_meta_2['cod'].map(mapa_nombres)

        return df_meta_2[['cod', 'lat', 'lon', 'nombre_visible']], df_p[['cod', 'fecha_dt', 'mm']]
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

# --- LÓGICA GEOMÉTRICA y selección (en METROS para líneas) ---
def _point_in_polygon(pt, polygon):
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
    def poly_contains(pt, poly):
        exterior = poly[0]
        if not _point_in_polygon(pt, exterior):
            return False
        for hole in poly[1:]:
            if _point_in_polygon(pt, hole):
                return False
        return True
    if len(coords) > 0 and isinstance(coords[0][0][0], (float, int)):
        return poly_contains(pt, coords)  # Polygon
    else:
        for poly in coords:
            if poly_contains(pt, poly):
                return True
        return False

def _squared_distance_point_to_segment_m(px, py, x1, y1, x2, y2):
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

def _min_squared_distance_to_lines_m(coords, px_deg, py_deg, m_per_deg_lon, m_per_deg_lat):
    px_m = float(px_deg) * m_per_deg_lon
    py_m = float(py_deg) * m_per_deg_lat

    def line_dist(line):
        dmin = float('inf')
        for i in range(len(line) - 1):
            x1_m = float(line[i][0]) * m_per_deg_lon
            y1_m = float(line[i][1]) * m_per_deg_lat
            x2_m = float(line[i+1][0]) * m_per_deg_lon
            y2_m = float(line[i+1][1]) * m_per_deg_lat
            d = _squared_distance_point_to_segment_m(px_m, py_m, x1_m, y1_m, x2_m, y2_m)
            if d < dmin:
                dmin = d
        return dmin

    if len(coords) > 0 and isinstance(coords[0][0], (float, int)):
        return line_dist(coords)  # LineString
    else:
        dmin = float('inf')
        for line in coords:
            d = line_dist(line)
            if d < dmin:
                dmin = d
        return dmin

def _buscar_marker_por_click(lat, lon, df_pts, tol_metros=30):
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

def _buscar_feature_por_click(lat, lon, canales_all, catastro_all,
                              tol_metros=35, considerar_canales=True, considerar_catastro=True, priorizar_canal=True):
    lat_abs = abs(float(lat))
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(np.cos(np.radians(lat_abs)), 1e-6)
    tol2_m = float(tol_metros) ** 2

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
        best = (None, None, float('inf'))
        for feat in canales_all.get('features', []):
            g = feat.get('geometry', {})
            if g.get('type') in ('LineString', 'MultiLineString'):
                d2_m = _min_squared_distance_to_lines_m(
                    g.get('coordinates', []),
                    px_deg=float(lon), py_deg=float(lat),
                    m_per_deg_lon=m_per_deg_lon,
                    m_per_deg_lat=m_per_deg_lat
                )
                if d2_m < best[2]:
                    p = feat.get('properties', {})
                    best = (p.get('fid'), p, d2_m)
        if best[0] is not None and np.isfinite(best[2]) and best[2] <= tol2_m:
            return best[0], best[1]
        return None

    if priorizar_canal:
        cand = pick_canal()
        if cand:
            return cand
        cand = pick_parcela()
        if cand:
            return cand
    else:
        cand = pick_parcela()
        if cand:
            return cand
        cand = pick_canal()
        if cand:
            return cand
    return None, None

# --- UTILIDADES ANALISIS ---
def _naive_series(s: pd.Series) -> pd.Series:
    if s is None or getattr(s, "dtype", None) is None:
        return s
    if is_datetime64tz_dtype(s.dtype):
        try:
            return s.dt.tz_convert(None)
        except Exception:
            return s.dt.tz_localize(None)
    return s

def _semana_bounds(d: date):
    inicio = pd.Timestamp(d) - pd.Timedelta(days=d.weekday())
    fin = inicio + pd.Timedelta(days=7)
    return inicio, fin

def _mes_bounds(d: date):
    inicio = pd.Timestamp(d.replace(day=1))
    fin = (inicio + pd.offsets.MonthBegin(1))
    return inicio, fin

def _nearest_pluv_for(lat, lon, df_pluv_meta: pd.DataFrame):
    if df_pluv_meta is None or df_pluv_meta.empty:
        return None
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(np.cos(np.radians(abs(lat))), 1e-6)
    dx = (df_pluv_meta['lon'].astype(float) - float(lon)) * m_per_deg_lon
    dy = (df_pluv_meta['lat'].astype(float) - float(lat)) * m_per_deg_lat
    dist2 = dx**2 + dy**2
    idx = dist2.idxmin()
    return df_pluv_meta.loc[idx].to_dict()

def _iqr_outliers(x: pd.Series):
    x = pd.to_numeric(x, errors="coerce").dropna()
    if x.empty:
        return pd.Series(dtype=bool), None
    q1, q3 = np.percentile(x, [25, 75])
    iqr = q3 - q1
    low = q1 - 1.5 * iqr
    high = q3 + 1.5 * iqr
    mask = (x < low) | (x > high)
    return mask, {"q1": q1, "q3": q3, "low": low, "high": high}

# --- ESTADO DE SELECCIÓN ---
if "sel_type" not in st.session_state:
    st.session_state.sel_type = None
if "sel_data" not in st.session_state:
    st.session_state.sel_data = None
if "base_layer" not in st.session_state:
    st.session_state.base_layer = "OSM"
if "_clear_once" not in st.session_state:
    st.session_state._clear_once = False

# Cargar datos
df_historial, df_maestro = cargar_datos_kobo()
df_pluv_meta, df_pluv_pp = cargar_pluviometros_INTAlike()

# 4) TÍTULO — Desktop: logo + título alineados • Móvil: oculta logo y centra el título
TITLE_TEXT = "Gestión de Aforos"

# CSS: oculta el logo en móviles y centra el título como antes (evita que se “caiga”)
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
    .logo-chip {
        background: #ffffff;
        border-radius: 8px;
        padding: 6px;
        box-shadow: 0 0 0 1px rgba(0,0,0,.08);
        display: inline-block;
    }
    .leaflet-interactive { cursor: pointer !important; }
</style>
""", unsafe_allow_html=True)

# Construcción del header
st.markdown(f'<div class="titulo-responsive"><span class="emoji">🌊</span>{TITLE_TEXT}</div>', unsafe_allow_html=True)



if df_maestro.empty:
    st.error("No se pudieron cargar los datos de Kobo.")
    st.stop()

# Asegurar fechas naive en historial
if not df_historial.empty and 'fecha_dt' in df_historial.columns:
    df_historial['fecha_dt'] = _naive_series(df_historial['fecha_dt'])

with st.sidebar:
    if os.path.exists(LOGO_PATH):
        st.image(LOGO_PATH, use_container_width=True)
        st.markdown("<hr>", unsafe_allow_html=True)
    


# ===================
# PESTAÑAS: MAPA / DATOS / ANALISIS
# ===================
tab_mapa, tab_datos, tab_analisis = st.tabs(["🗺️ Mapa", "📄 Datos", "📊 Análisis"])

# ===================
# TAB: MAPA
# ===================
with tab_mapa:
    with st.sidebar:
        # ... aquí ya tenés tu logo arriba ...

        # Creamos un bloque colapsable para el Mapa Base
        with st.expander("🗺️ Mapa Base", expanded=False): 
            st.session_state.base_layer = st.radio(
                label="Seleccione:", 
                options=["Satélite", "OSM"],
                index=0 if st.session_state.base_layer == "Satélite" else 1,
                horizontal=True
            )
            
        # Creamos otro bloque colapsable para las Capas
        with st.expander("📚 Capas", expanded=False):
            show_canales = st.checkbox("Canales", value=True)
            show_catastro = st.checkbox("Catastro", value=False)
            show_aforadores = st.checkbox("Aforadores", value=True)
            show_pluviometros = st.checkbox("Pluviómetros (INTA)", value=True)

    canales_all = obtener_geojson_local("canales.geojson", "canales")
    catastro_all = obtener_geojson_local("catastro.geojson", "catastro")

    m = folium.Map(
        location=[df_maestro['lat'].mean(), df_maestro['lon'].mean()],
        zoom_start=13,
        tiles=None
    )

    if st.session_state.base_layer == "Satélite":
        folium.TileLayer('https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}', attr='Google', name='Satélite').add_to(m)
    else:
        folium.TileLayer('OpenStreetMap', name='OSM').add_to(m)

    folium.map.CustomPane('pane_catastro', z_index=300).add_to(m)
    folium.map.CustomPane('pane_canales',  z_index=400).add_to(m)
    folium.map.CustomPane('pane_markers',  z_index=650).add_to(m)

    if show_catastro and catastro_all:
        folium.GeoJson(
            catastro_all,
            style_function=lambda x: {'color': '#E67E22','weight': 1,'fillColor': '#F39C12','fillOpacity': 0.12},
            highlight_function=lambda x: {'color': '#D35400','weight': 2,'fillOpacity': 0.20},
            pane='pane_catastro',
            name='Catastro'
        ).add_to(m)

    if show_canales and canales_all:
        folium.GeoJson(
            canales_all,
            style_function=lambda f: {'color': get_color_sistema(f['properties'].get('sistema')),'weight': 4, 'opacity': 0.85},
            highlight_function=lambda x: {'weight': 8, 'color': 'yellow', 'opacity': 1},
            pane='pane_canales',
            name='Canales'
        ).add_to(m)

    if show_aforadores:
        markers_fg = folium.FeatureGroup(name='Aforadores', overlay=True, pane='pane_markers')
        for _, p in df_maestro.iterrows():
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
                ) for _, r in u3.iterrows()
            ])
            pop_html = (
                f"<div style='width:220px; font-family:sans-serif;'>"
                f"<div style='background:#1E3A8A; color:white; padding:5px; text-align:center; font-weight:bold;'>{p['Aforador']}</div>"
                f"<table style='width:100%; font-size:11px; margin-top:5px; border-collapse:collapse;'>"
                f"<thead><tr><th style='text-align:left; padding:2px 4px;'>Fecha</th>"
                f"<th style='text-align:left; padding:2px 4px;'>Hora</th>"
                f"<th style='text-align:right; padding:2px 4px;'>Caudal</th></tr></thead>"
                f"<tbody>{filas_html}</tbody></table></div>"
            )
            folium.Marker(
                [p['lat'], p['lon']],
                popup=folium.Popup(pop_html, max_width=240),
                tooltip=p['Aforador'],
                icon=folium.Icon(color='blue', icon='tint', prefix='fa')
            ).add_to(markers_fg)
        markers_fg.add_to(m)

    if show_pluviometros and not df_pluv_meta.empty:
        fg_pluv = folium.FeatureGroup(name='Pluviómetros INTA', overlay=True, pane='pane_markers')
        for _, row in df_pluv_meta.iterrows():
            u3 = (
                df_pluv_pp[df_pluv_pp['cod'] == row['cod']]
                .sort_values('fecha_dt', ascending=False)
                .head(3)
            )
            filas = "".join([
                (
                    f"<tr>"
                    f"<td style='padding:2px 4px;'>{r['fecha_dt'].strftime('%d/%m/%Y')}</td>"
                    f"<td style='padding:2px 4px; text-align:right;'><b>{round(float(r['mm']), 1)} mm</b></td>"
                    f"</tr>"
                ) for _, r in u3.iterrows()
            ])
            pop_html = (
                f"<div style='width:260px; font-family:sans-serif;'>"
                f"<div style='background:#0E7490; color:white; padding:6px; text-align:center; font-weight:bold;'>🌧️ {row['nombre_visible']}</div>"
                f"<div style='margin-top:6px;'>"
                f"<table style='width:100%; font-size:11px; border-collapse:collapse;'>"
                f"<thead><tr><th style='text-align:left; padding:2px 4px;'>Fecha</th>"
                f"<th style='text-align:right; padding:2px 4px;'>Lluvia</th></tr></thead>"
                f"<tbody>{filas}</tbody></table></div></div>"
            )
            folium.Marker(
                [row['lat'], row['lon']],
                popup=folium.Popup(pop_html, max_width=300),
                tooltip=f"Pluviómetro - {row['nombre_visible']}",
                icon=folium.Icon(color='green', icon='cloud', prefix='fa')
            ).add_to(fg_pluv)
        fg_pluv.add_to(m)

    LocateControl(position='topleft').add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)

    salida = st_folium(
        m, width="100%", height=600, key="mapa_estatico",
        returned_objects=["last_clicked", "last_object_clicked"],
        use_container_width=True
    )

    if st.session_state._clear_once:
        st.session_state._clear_once = False
        clic = None
    else:
        clic = salida.get("last_clicked") or salida.get("last_object_clicked")

    if clic and isinstance(clic, dict) and {'lat', 'lng'} <= clic.keys():
        lat, lon = clic['lat'], clic['lng']

        match_m = _buscar_marker_por_click(
            lat, lon,
            df_maestro if show_aforadores else df_maestro.iloc[0:0],
            tol_metros=30
        )
        if match_m is not None:
            st.session_state.sel_type = 'marker'
            st.session_state.sel_data = match_m.to_dict()
        else:
            fid, props = _buscar_feature_por_click(
                lat, lon,
                canales_all if show_canales else None,
                catastro_all if show_catastro else None,
                tol_metros=35,
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
            st.write(f"**Tipo:** {sel.get('Tipo_fmt', sel.get('Tipo', 'N/D'))}")
            st.markdown("---")
            st.write("**Últimos datos:**")
            u5 = df_historial[df_historial['af_actual'] == sel.get('id_aforador', '')].sort_values('fecha_dt', ascending=False).head(3)
            if not u5.empty:
                for _, r in u5.iterrows():
                    st.markdown(
                        f'<div class="metric-box">📅 {r["fecha_format"]} {r["hora_format"]}<br><b>Caudal: {r["caudal"]} l/s</b></div>',
                        unsafe_allow_html=True
                    )
            else:
                st.info("Sin registros de caudal.")
        elif st.session_state.sel_type == 'geojson' and st.session_state.sel_data:
            p = st.session_state.sel_data['props']
            if st.session_state.sel_data['fid'].startswith("canal_"):
                longi_raw = p.get('longi', 0)
                try:
                    longi_int = int(round(float(longi_raw)))
                except:
                    longi_int = 0
                st.subheader(f"🌊 {p.get('tipo', 'N/A')}: {p.get('nombre', 'Sin nombre')}")
                st.markdown(
                    f"- **Sistema:** {p.get('sistema', 'N/A')}\n"
                    f"- **Longitud:** {longi_int} m\n"
                    f"- **Tipo:** {p.get('tipo', 'N/A')}\n"
                    f"- **Ref.:** {p.get('pu_priv', 'N/A')}\n"
                    f"- **Estado.:** {p.get('reves', 'N/A')}\n"
                    f"- **Ancho Inf.:** {p.get('ancho_inf', 'N/A')} m\n"
                    f"- **Ancho Sup.:** {p.get('ancho_sup', 'N/A')} m\n"
                    f"- **Talud:** {p.get('talud', 'N/A')} m\n"
                    f"- **Altura:** {p.get('Altura', 'N/A')} m"
                )
            else:
                area_raw = p.get('shape_area', 0) or 0
                try:
                    area_ha = round(float(area_raw) / 10000, 2)
                except:
                    area_ha = 0.0
                st.subheader(f"🚜 Parcela: {p.get('finca', 'S/N')}")
                st.markdown(f"- **Catastro:** {p.get('catastro', 'N/A')}\n- **Área:** {area_ha} Ha")
        else:
            st.info("💡 Haz clic en un elemento del mapa.")

# ===================
# TAB: DATOS (listado rápido)
# ===================
with tab_datos:
    st.markdown("### 📄 Datos de aforadores")
    col1, col2 = st.columns([1, 2])
    with col1:
        modo = st.radio("Ver por:", ["Día", "Semana"], horizontal=True, key="modo_datos")
    with col2:
        fecha_default = date.today()
        if not df_historial.empty and pd.notna(df_historial['fecha_dt'].max()):
            fmax = df_historial['fecha_dt']
            if is_datetime64tz_dtype(getattr(fmax, "dtype", None)):
                fmax = fmax.dt.tz_localize(None)
            fecha_max = fmax.max()
            if pd.notna(fecha_max):
                fecha_default = fecha_max.date()
        fecha_sel = st.date_input("Seleccioná fecha", value=fecha_default, format="DD/MM/YYYY", key="fecha_datos")

    if modo == "Día":
        inicio = pd.Timestamp(fecha_sel)
        fin = inicio + pd.Timedelta(days=1)
        subt = f"📅 Día: {inicio.strftime('%d/%m/%Y')}"
    else:
        fin = pd.Timestamp(fecha_sel) + pd.Timedelta(days=1)
        inicio = pd.Timestamp(fecha_sel) - pd.Timedelta(days=6)
        subt = f"📅 Semana: {inicio.strftime('%d/%m/%Y')} a {(fin - pd.Timedelta(days=1)).strftime('%d/%m/%Y')}"
    st.caption(subt)

    ser_dt = df_historial['fecha_dt']
    if is_datetime64tz_dtype(getattr(ser_dt, "dtype", None)):
        ser_dt = ser_dt.dt.tz_localize(None)

    mask = (ser_dt >= inicio) & (ser_dt < fin)
    df_rango = df_historial[mask].copy()

    if 'Observaciones' not in df_rango.columns:
        df_rango['Observaciones'] = ""

    col_orden = next((c for c in df_maestro.columns if c.lower() == 'orden' or 'orden' in c.lower()), None)
    if col_orden is None:
        df_maestro['orden'] = np.inf
        col_orden = 'orden'
    df_maestro[col_orden] = pd.to_numeric(df_maestro[col_orden], errors='coerce')

    filas = []

    df_maestro_sorted = df_maestro.sort_values(
        by=[col_orden],
        ascending=[True],
        na_position='last'
    )

    if modo == "Día":
        df_dia_sorted = df_rango.sort_values('fecha_dt', ascending=True)
        fecha_unica_grilla = inicio.strftime('%d/%m/%Y')

        for _, m in df_maestro_sorted.iterrows():
            af_id = m['id_aforador']
            nombre = m.get('Aforador', '')
            orden_val = m.get(col_orden, np.inf)

            rows_af = df_dia_sorted[df_dia_sorted['af_actual'] == af_id]
            rows_af = rows_af.tail(3).sort_values('fecha_dt', ascending=True).copy()

            horas = list(rows_af['hora_format']) + ["", "", ""]
            caudales = list(rows_af['caudal']) + ["", "", ""]

            filas.append({
                "Orden": orden_val,
                "Aforador": nombre,
                "Fecha": fecha_unica_grilla,
                "Hora_1": horas[0], "Caudal_1 (l/s)": caudales[0],
                "Hora_2": horas[1], "Caudal_2 (l/s)": caudales[1],
                "Hora_3": horas[2], "Caudal_3 (l/s)": caudales[2],
                "_Fecha_dia": pd.Timestamp(fecha_sel)
            })

    else:
        if not df_rango.empty:
            df_rango['fecha_dia'] = df_rango['fecha_dt'].dt.normalize()

        for _, m in df_maestro_sorted.iterrows():
            af_id = m['id_aforador']
            nombre = m.get('Aforador', '')
            orden_val = m.get(col_orden, np.inf)

            df_af = df_rango[df_rango['af_actual'] == af_id]
            if df_af.empty:
                continue

            dias = sorted(df_af['fecha_dia'].dropna().unique(), reverse=True)
            for d0 in dias:
                df_day = df_af[df_af['fecha_dia'] == d0].sort_values('fecha_dt', ascending=True).copy()
                df_day = df_day.tail(3).sort_values('fecha_dt', ascending=True)

                horas = list(df_day['hora_format']) + ["", "", ""]
                caudales = list(df_day['caudal']) + ["", "", ""]

                filas.append({
                    "Orden": orden_val,
                    "Aforador": nombre,
                    "Fecha": pd.Timestamp(d0).strftime('%d/%m/%Y'),
                    "Hora_1": horas[0], "Caudal_1 (l/s)": caudales[0],
                    "Hora_2": horas[1], "Caudal_2 (l/s)": caudales[1],
                    "Hora_3": horas[2], "Caudal_3 (l/s)": caudales[2],
                    "_Fecha_dia": pd.Timestamp(d0)
                })

    df_tab = pd.DataFrame(filas)

    if not df_tab.empty:
        df_tab = df_tab.sort_values(
            by=["Orden", "_Fecha_dia"],
            ascending=[True, False],
            na_position='last'
        ).drop(columns=["_Fecha_dia"], errors='ignore')

    if not df_tab.empty:
        for col in ["Caudal_1 (l/s)", "Caudal_2 (l/s)", "Caudal_3 (l/s)"]:
            if col in df_tab.columns:
                df_tab[col] = pd.to_numeric(df_tab[col], errors="coerce")

        column_order = [
            "Orden", "Aforador", "Fecha",
            "Hora_1", "Caudal_1 (l/s)",
            "Hora_2", "Caudal_2 (l/s)",
            "Hora_3", "Caudal_3 (l/s)"
        ]
        column_order = [c for c in column_order if c in df_tab.columns]

        st.dataframe(
            df_tab,
            hide_index=True,
            use_container_width=True,
            column_order=column_order,
            column_config={
                "Orden": st.column_config.NumberColumn("Orden", width="small"),
                "Aforador": st.column_config.TextColumn("Aforador", width="large"),
                "Fecha": st.column_config.TextColumn("Fecha", width=110),

                "Hora_1": st.column_config.TextColumn("Hora", width=90),
                "Caudal_1 (l/s)": st.column_config.NumberColumn("Caudal (l/s)", format="%.0f", width=120),

                "Hora_2": st.column_config.TextColumn("Hora", width=90),
                "Caudal_2 (l/s)": st.column_config.NumberColumn("Caudal (l/s)", format="%.0f", width=120),

                "Hora_3": st.column_config.TextColumn("Hora", width=90),
                "Caudal_3 (l/s)": st.column_config.NumberColumn("Caudal (l/s)", format="%.0f", width=120),
            }
        )

        csv = df_tab.to_csv(index=False).encode('utf-8')
        colcsv, colpdf = st.columns([1,1])
        with colcsv:
            st.download_button(
                "⬇️ Descargar CSV",
                data=csv,
                file_name=f"aforadores_{'dia' if modo=='Día' else 'semana'}_{pd.Timestamp(fecha_sel).strftime('%Y%m%d')}.csv",
                mime="text/csv"
            )
        with colpdf:
            try:
                pdf_bytes = generar_pdf_reporte_datos(df_tab, modo, inicio, fin)
                st.download_button(
                    "⬇️ Descargar PDF",
                    data=pdf_bytes,
                    file_name=f"reporte_{'dia' if modo=='Día' else 'semana'}_{pd.Timestamp(fecha_sel).strftime('%Y%m%d')}.pdf",
                    mime="application/pdf"
                )
            except Exception as e:
                st.warning("No se pudo generar el PDF. Avisame si persiste y lo vemos.")
    else:
        st.dataframe(
            df_tab,
            hide_index=True,
            use_container_width=True
        )

# ===================
# TAB: ANALISIS (nueva)
# ===================
with tab_analisis:
    st.markdown("### 📊 Análisis y gráficos")

    sub_sem, sub_comp, sub_mes = st.tabs(["📅 Semanal (1 aforador)", "🟰 Comparación (2 aforadores)", "📈 Dashboard mensual"])

    # -----------------------
    # SUBTAB: SEMANAL (1 aforador)
    # -----------------------
    with sub_sem:
        colA, colB, colC = st.columns([1.2, 1, 1])
        aforadores_list = sorted(df_maestro["Aforador"].unique())
        with colA:
            af_sel = st.selectbox("Aforador", aforadores_list, index=0, key="anal_aforador")
        with colB:
            fecha_base = st.date_input("Semana a analizar", value=date.today(), format="DD/MM/YYYY", key="anal_semana")
        with colC:
            mostrar_lluvia = st.checkbox("Cruce lluvia–caudal", value=True)

        inicio_sem, fin_sem = _semana_bounds(fecha_base)
        st.caption(f"📅 Semana: {inicio_sem.strftime('%d/%m/%Y')} → {(fin_sem - pd.Timedelta(days=1)).strftime('%d/%m/%Y')}")

        if df_historial.empty:
            st.info("Sin historial de aforos.")
        else:
            af_id_series = df_maestro.loc[df_maestro["Aforador"] == af_sel, "id_aforador"]
            if af_id_series.empty:
                st.warning("No se encontró el ID del aforador seleccionado.")
            else:
                af_id = af_id_series.iloc[0]
                df_sem = df_historial[
                    (df_historial["af_actual"] == af_id) &
                    (df_historial["fecha_dt"] >= inicio_sem) &
                    (df_historial["fecha_dt"] < fin_sem)
                ].sort_values("fecha_dt").copy()

                st.subheader("📄 Datos de la semana")
                if df_sem.empty:
                    st.info("No hay datos en la semana seleccionada.")
                else:
                    df_sem_view = df_sem[["fecha_format", "hora_format", "caudal"]].copy()
                    df_sem_view["caudal"] = pd.to_numeric(df_sem_view["caudal"], errors="coerce")
                    st.dataframe(
                        df_sem_view,
                        hide_index=True,
                        use_container_width=True,
                        column_order=["fecha_format", "hora_format", "caudal"],
                        column_config={
                            "fecha_format": st.column_config.TextColumn("Fecha", width=110),
                            "hora_format": st.column_config.TextColumn("Hora", width=90),
                            "caudal": st.column_config.NumberColumn("Caudal (l/s)", format="%.0f", width=120),
                        }
                    )

                    csv_sem = df_sem.to_csv(index=False).encode("utf-8")
                    st.download_button(
                        "⬇️ Descargar CSV semanal",
                        data=csv_sem,
                        file_name=f"analisis_{af_sel}_{inicio_sem.strftime('%Y%m%d')}.csv",
                        mime="text/csv"
                    )

                    st.subheader("📈 Caudal vs tiempo (semanal)")
                    chart_df = df_sem[["fecha_dt", "caudal"]].rename(columns={"fecha_dt":"FechaHora","caudal":"Caudal (l/s)"})
                    st.line_chart(chart_df.set_index("FechaHora"))

                    st.subheader("📊 Resumen diario")
                    df_sem["dia"] = df_sem["fecha_dt"].dt.strftime("%d/%m")
                    df_dias = df_sem.groupby("dia").agg(
                        promedio=("caudal", "mean"),
                        maximo=("caudal", "max"),
                        minimo=("caudal", "min"),
                        lecturas=("caudal", "count"),
                        acumulado=("caudal", "sum")
                    ).reset_index()

                    col1, col2 = st.columns(2)
                    with col1:
                        st.markdown("**Promedio diario (l/s)**")
                        st.bar_chart(df_dias.set_index("dia")["promedio"])
                    with col2:
                        st.markdown("**Acumulado de lecturas (l/s)**")
                        st.bar_chart(df_dias.set_index("dia")["acumulado"])

                    st.subheader("🚨 Alertas")
                    mask_out, stats = _iqr_outliers(df_sem["caudal"])
                    out_count = int(mask_out.sum()) if stats else 0
                    zeros = int((df_sem["caudal"] == 0).sum())
                    if out_count == 0 and zeros == 0:
                        st.success("Sin outliers ni caudales cero en la semana.")
                    else:
                        if out_count > 0:
                            st.warning(f"Valores atípicos (IQR): {out_count} lecturas fuera de [{stats['low']:.1f}, {stats['high']:.1f}] l/s.")
                        if zeros > 0:
                            st.warning(f"Caudal cero detectado en {zeros} lecturas.")

                    if mostrar_lluvia and not df_pluv_meta.empty and not df_pluv_pp.empty:
                        af_row = df_maestro[df_maestro["Aforador"] == af_sel].iloc[0]
                        pluv_info = _nearest_pluv_for(af_row["lat"], af_row["lon"], df_pluv_meta)
                        if pluv_info:
                            cod_pluv = pluv_info["cod"]
                            nombre_pluv = pluv_info.get("nombre_visible", cod_pluv)
                            df_ll = df_pluv_pp[(df_pluv_pp["cod"] == cod_pluv) &
                                               (df_pluv_pp["fecha_dt"] >= inicio_sem) &
                                               (df_pluv_pp["fecha_dt"] < fin_sem)].copy()
                            if not df_ll.empty:
                                df_ll["fecha"] = df_ll["fecha_dt"].dt.date
                                df_sem["fecha"] = df_sem["fecha_dt"].dt.date
                                df_caudal_dia = df_sem.groupby("fecha")["caudal"].mean().reset_index(name="Caudal promedio (l/s)")
                                df_lluvia_dia = df_ll.groupby("fecha")["mm"].sum().reset_index(name="Lluvia (mm)")

                                df_merge = pd.merge(df_caudal_dia, df_lluvia_dia, on="fecha", how="outer").sort_values("fecha")
                                df_merge["fecha_str"] = pd.to_datetime(df_merge["fecha"]).dt.strftime("%d/%m")

                                st.markdown(f"**Cruce con pluviómetro cercano:** {nombre_pluv}")
                                try:
                                    base = alt.Chart(df_merge).encode(x=alt.X('fecha:T', title='Fecha'))
                                    line = base.mark_line(color='#1E3A8A').encode(y=alt.Y('Caudal promedio (l/s):Q', title='Caudal (l/s)'))
                                    bars = base.mark_bar(color='#0E7490', opacity=0.5).encode(y=alt.Y('Lluvia (mm):Q', title='Lluvia (mm)'))
                                    st.altair_chart(alt.layer(bars, line).resolve_scale(y='independent').properties(height=300), use_container_width=True)
                                except Exception:
                                    st.info("No se pudo renderizar eje dual; muestro gráficos separados.")
                                    st.line_chart(df_merge.set_index(pd.to_datetime(df_merge["fecha"]))["Caudal promedio (l/s)"])
                                    st.bar_chart(df_merge.set_index(pd.to_datetime(df_merge["fecha"]))["Lluvia (mm)"])
                            else:
                                st.info("No hay datos de lluvia para esa semana en el pluviómetro más cercano.")
                        else:
                            st.info("No se pudo determinar un pluviómetro cercano.")

    # -----------------------
    # SUBTAB: COMPARACIÓN (2 aforadores)
    # -----------------------
    with sub_comp:
        st.markdown("### 🟰 Comparación semanal entre 2 aforadores")

        col1, col2, col3 = st.columns([1.2, 1.2, 1])
        afs_all = sorted(df_maestro["Aforador"].unique())

        with col1:
            afA = st.selectbox("Aforador A", afs_all, key="cmp_a")

        with col2:
            afs_B = [a for a in afs_all if a != afA]
            afB = st.selectbox("Aforador B (no puede ser el mismo)", afs_B, key="cmp_b")

        with col3:
            fecha_cmp = st.date_input("Semana", value=date.today(), format="DD/MM/YYYY", key="cmp_sem")

        ini_cmp, fin_cmp = _semana_bounds(fecha_cmp)
        st.caption(f"📅 Semana: {ini_cmp.strftime('%d/%m/%Y')} → {(fin_cmp - pd.Timedelta(days=1)).strftime('%d/%m/%Y')}")

        if df_historial.empty:
            st.info("Sin historial de aforos.")
        else:
            def serie_af(af_name, alias):
                s = df_maestro.loc[df_maestro["Aforador"] == af_name, "id_aforador"]
                if s.empty:
                    return pd.DataFrame(columns=["fecha_dt", alias])
                af_id = s.iloc[0]
                d = df_historial[
                    (df_historial["af_actual"] == af_id) &
                    (df_historial["fecha_dt"] >= ini_cmp) &
                    (df_historial["fecha_dt"] < fin_cmp)
                ][["fecha_dt", "caudal"]].copy()
                d = d.sort_values("fecha_dt").rename(columns={"caudal": alias})
                return d

            sa = serie_af(afA, "A")
            sb = serie_af(afB, "B")

            if sa.empty and sb.empty:
                st.info("No hay datos para esa semana en ninguno de los dos aforadores.")
            else:
                df_cmp = pd.merge(sa, sb, on="fecha_dt", how="outer").sort_values("fecha_dt")
                if "A" not in df_cmp.columns: df_cmp["A"] = np.nan
                if "B" not in df_cmp.columns: df_cmp["B"] = np.nan

                st.subheader(f"📈 {afA} vs {afB} (semanal)")
                st.line_chart(df_cmp.set_index("fecha_dt")[["A", "B"]])

                colm1, colm2, colm3, colm4 = st.columns(4)
                colA = pd.to_numeric(df_cmp["A"], errors="coerce")
                colB = pd.to_numeric(df_cmp["B"], errors="coerce")
                with colm1:
                    st.metric(f"Promedio {afA}", f"{colA.mean():.1f} l/s" if colA.notna().any() else "—")
                with colm2:
                    st.metric(f"Máximo {afA}", f"{colA.max():.1f} l/s" if colA.notna().any() else "—")
                with colm3:
                    st.metric(f"Promedio {afB}", f"{colB.mean():.1f} l/s" if colB.notna().any() else "—")
                with colm4:
                    st.metric(f"Máximo {afB}", f"{colB.max():.1f} l/s" if colB.notna().any() else "—")

                df_out = df_cmp.rename(columns={"A": afA, "B": afB})
                st.download_button(
                    "⬇️ Descargar CSV comparación",
                    data=df_out.to_csv(index=False).encode("utf-8"),
                    file_name=f"comparacion_{afA}_{afB}_{ini_cmp.strftime('%Y%m%d')}.csv",
                    mime="text/csv"
                )

    # -----------------------
    # SUBTAB: DASHBOARD MENSUAL
    # -----------------------
    with sub_mes:
        colmA, colmB = st.columns([1.2, 1])
        with colmA:
            af_m = st.selectbox("Aforador", sorted(df_maestro["Aforador"].unique()), key="mes_af")
        with colmB:
            fecha_mes = st.date_input("Mes a analizar", value=date.today(), format="DD/MM/YYYY", key="mes_fecha")

        ini_mes, fin_mes = _mes_bounds(fecha_mes)
        st.caption(f"📅 Mes: {ini_mes.strftime('%m/%Y')}")

        if df_historial.empty:
            st.info("Sin historial de aforos.")
        else:
            s = df_maestro.loc[df_maestro["Aforador"] == af_m, "id_aforador"]
            if s.empty:
                st.warning("No se encontró el ID del aforador seleccionado.")
            else:
                af_id = s.iloc[0]
                df_m = df_historial[
                    (df_historial["af_actual"] == af_id) &
                    (df_historial["fecha_dt"] >= ini_mes) &
                    (df_historial["fecha_dt"] < fin_mes)
                ].copy()
                if df_m.empty:
                    st.info("Sin datos en el mes seleccionado.")
                else:
                    df_m = df_m.sort_values("fecha_dt")
                    df_m["fecha"] = df_m["fecha_dt"].dt.date

                    prom = float(pd.to_numeric(df_m["caudal"], errors="coerce").mean())
                    mx = float(pd.to_numeric(df_m["caudal"], errors="coerce").max())
                    mn = float(pd.to_numeric(df_m["caudal"], errors="coerce").min())
                    dias_medidos = int(df_m["fecha"].nunique())
                    lecturas = int(len(df_m))
                    zeros = int((df_m["caudal"] == 0).sum())
                    out_mask, stats = _iqr_outliers(df_m["caudal"])
                    out_c = int(out_mask.sum()) if stats else 0

                    k1, k2, k3, k4, k5 = st.columns(5)
                    k1.metric("Promedio (l/s)", f"{prom:.1f}")
                    k2.metric("Máximo (l/s)", f"{mx:.1f}")
                    k3.metric("Mínimo (l/s)", f"{mn:.1f}")
                    k4.metric("Días con medición", f"{dias_medidos}")
                    k5.metric("Lecturas totales", f"{lecturas}")

                    if zeros > 0 or out_c > 0:
                        st.warning(f"Alertas: {zeros} lecturas con caudal 0 • {out_c} outliers (IQR).")

                    df_diario = df_m.groupby("fecha")["caudal"].mean().reset_index(name="Promedio diario (l/s)")
                    st.subheader("📊 Promedio diario (mes)")
                    st.bar_chart(df_diario.set_index(pd.to_datetime(df_diario["fecha"]))["Promedio diario (l/s)"])

                    st.subheader("📈 Caudal vs tiempo (mensual)")
                    st.line_chart(df_m.set_index("fecha_dt")["caudal"])

                    st.download_button(
                        "⬇️ Descargar CSV mensual",
                        data=df_m.to_csv(index=False).encode("utf-8"),
                        file_name=f"mensual_{af_m}_{ini_mes.strftime('%Y%m')}.csv",
                        mime="text/csv"
                    )
