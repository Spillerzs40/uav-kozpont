import streamlit as st
import requests
import math
import folium
import json
import os
import io
import time
import sqlite3
from datetime import datetime
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim

# --- GOOGLE DRIVE IMPORTOK ---
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# --- DRIVE BEÁLLÍTÁSOK ---
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
FOLDER_ID = '12Vst1DGW9S095jfyJmsCQgUQpjM_PiKj'
FAJL_NEV = "uav_flight_data.db"  # Ezt a fájlt fogja letölteni a felhőből

def letoltes_drive_rol():
    try:
        # Okos hitelesítés: Ha Streamlit Cloudban vagyunk, st.secrets-et használ,
        # ha helyben teszteljük a gépen, akkor a credentials.json fájlt olvassa.
        try:
            creds_dict = json.loads(st.secrets["gcp_credentials"])
            creds = service_account.Credentials.from_service_account_info(
                creds_dict, scopes=SCOPES)
        except:
            creds = service_account.Credentials.from_service_account_file(
                "credentials.json", scopes=SCOPES)

        service = build('drive', 'v3', credentials=creds)

        # Fájl keresése a mappában
        query = f"'{FOLDER_ID}' in parents and name='{FAJL_NEV}' and trashed=false"
        results = service.files().list(q=query, fields="files(id, name)", orderBy="createdTime desc").execute()
        items = results.get('files', [])

        if not items:
            st.error("Nem található az adatbázis a Drive-on! Töltsd fel a terepi laptopról.")
            return False

        file_id = items[0]['id']
        
        # Fájl letöltése
        request = service.files().get_media(fileId=file_id)
        fh = io.FileIO(FAJL_NEV, 'wb')
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        with st.spinner('Friss adatok letöltése a központból...'):
            while done is False:
                status, done = downloader.next_chunk()
        return True
    except Exception as e:
        st.error(f"Hiba a letöltés során: {e}")
        return False

def local_css(file_name):
    if os.path.exists(file_name):
        with open(file_name, "r", encoding="utf-8") as f:
            st.markdown(f'<style>{f.read()}</style>', unsafe_allow_html=True)

# Csak olvasás az adatbázisból (Írás nincs a felhőben!)
def get_paginated_data(table, limit, offset):
    if not os.path.exists(FAJL_NEV):
        return [], 0
    conn = sqlite3.connect(FAJL_NEV)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        c.execute(f'SELECT * FROM {table} ORDER BY id DESC LIMIT ? OFFSET ?', (limit, offset))
        rows = c.fetchall()
        c.execute(f'SELECT COUNT(*) FROM {table}')
        total = c.fetchone()[0]
    except:
        rows, total = [], 0
    conn.close()
    return [dict(r) for r in rows], total

if 'page_tel' not in st.session_state: st.session_state['page_tel'] = 0
if 'page_ble' not in st.session_state: st.session_state['page_ble'] = 0

# --- TÁVOLSÁGSZÁMÍTÁS ÉS GEOJSON ---
def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def load_airspace_data(filepath="airspace.geojson"):
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def get_closest_zone(lat, lon, geojson_data):
    min_dist = float('inf')
    closest_name = "Nincs adat"
    if not geojson_data: return min_dist, closest_name
        
    for feature in geojson_data.get('features', []):
        name = feature.get('properties', {}).get('name', 'Ismeretlen Zóna')
        geom_type = feature.get('geometry', {}).get('type')
        coords = feature.get('geometry', {}).get('coordinates', [])
        
        flat_coords = []
        if geom_type == 'Point': flat_coords = [coords]
        elif geom_type == 'Polygon': flat_coords = coords[0]
            
        for pt in flat_coords:
            if len(pt) >= 2:
                d = calculate_distance(lat, lon, pt[1], pt[0])
                if d < min_dist:
                    min_dist = d
                    closest_name = name
    return min_dist, closest_name

# --- AZ ALGORITMUS ---
def calculate_flight_index(wind, gust, temp, visibility, kp, rain, zone_dist, dew_point, is_daylight):
    if zone_dist < 100: return "🔴 PIROS (NO-GO)", "Kritikusan közel egy tiltott légtérhez!", "red"
    if gust > 40 or wind > 30: return "🔴 PIROS (NO-GO)", "Túl erős szél vagy széllökés!", "red"
    if temp < -10 or temp > 40: return "🔴 PIROS (NO-GO)", "Extrém hőmérséklet (Akku károsodás)!", "red"
    if visibility < 1.0: return "🔴 PIROS (NO-GO)", "Kritikusan alacsony látótávolság!", "red"
    if kp >= 5: return "🔴 PIROS (NO-GO)", "Magas Kp-index, műholdas GPS zavar veszély!", "red"
    if rain > 0: return "🔴 PIROS (NO-GO)", "Csapadékos időjárás! Zárlatveszély.", "red"
    if temp <= 3 and (temp - dew_point) < 2.5: return "🔴 PIROS (NO-GO)", "Kritikus jegesedésveszély (Icing Risk) a propellereken!", "red"

    warnings = []
    if gust > 25: warnings.append("Erős széllökések.")
    if kp == 4: warnings.append("Közepes naptevékenység (Kp=4).")
    if zone_dist < 300: warnings.append("Légtérhatár a közelben.")
    if visibility < 3.0: warnings.append("Csökkent látótávolság.")
    if temp < 5: warnings.append("Alacsony hőmérséklet (gyorsabb akku merülés).")
    if not is_daylight: warnings.append("Éjszakai repülés! Csak VLOS engedéllyel.")
    
    if warnings: return "🟡 SÁRGA (FIGYELMEZTETÉS)", " | ".join(warnings), "orange"
    return "🟢 ZÖLD (GO)", "Biztonságos repülési feltételek.", "green"

# --- API LEKÉRDEZÉSEK ---
def get_live_weather(lat, lon):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m,dew_point_2m&daily=sunrise,sunset&timezone=auto"
    try:
        r = requests.get(url)
        if r.status_code == 200: return r.json()
    except: pass
    return None

def get_live_kp_index():
    url = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200: return float(r.json()[-1][1])
    except: pass
    return 0.0

def get_address_from_coords(lat, lon):
    geolocator = Nominatim(user_agent="uav_szakdoga_app")
    try:
        location = geolocator.reverse((lat, lon), exactly_one=True, timeout=5)
        if location: return location.address
    except: pass
    return "Pontos cím nem azonosítható"

def geocode_address(address):
    geolocator = Nominatim(user_agent="uav_szakdoga_app")
    try:
        location = geolocator.geocode(address)
        if location: return location.latitude, location.longitude
    except: pass
    return None, None

# --- FELÜLET ---
st.set_page_config(page_title="UAV Felhős Kliens", page_icon="☁️", layout="wide")
local_css("style.css")
st.title("☁️ UAV Központi Felhős Megjelenítő")

# --- SZINKRONIZÁLÓ GOMB AZ OLDALSÁVBAN ---
st.sidebar.header("🔄 Adatbázis Szinkronizáció")
if st.sidebar.button("📥 Friss adatok letöltése a terepről"):
    if letoltes_drive_rol():
        st.sidebar.success("Adatbázis sikeresen frissítve a Google Drive-ról!")
        st.rerun()
st.sidebar.divider()

airspace_data = load_airspace_data("airspace.geojson")

st.sidebar.header("📍 Célterület vizsgálata")
hely_mod = st.sidebar.radio("Válaszd ki a forrást:", ["🏠 Cím keresése", "📌 Manuális GPS koordináta"])

lat, lon = 47.4979, 19.0402 

if hely_mod == "🏠 Cím keresése":
    cim_input = st.sidebar.text_input("Írj be egy címet:", "Budapest, Dologház utca 1")
    if cim_input:
        talalt_lat, talalt_lon = geocode_address(cim_input)
        if talalt_lat and talalt_lon:
            lat, lon = talalt_lat, talalt_lon
            st.sidebar.success("**Sikeres azonosítás!**")
            st.sidebar.info(f"📍 **GPS:** {lat:.5f}, {lon:.5f}")
elif hely_mod == "📌 Manuális GPS koordináta":
    lat = st.sidebar.number_input("Szélesség (Latitude):", value=47.4996, format="%.6f")
    lon = st.sidebar.number_input("Hosszúság (Longitude):", value=19.0746, format="%.6f")

visibility_km = st.sidebar.slider("Látótávolság (km) szimulált", 0.0, 20.0, 10.0)
min_tavolsag, legkozelebbi_zona = get_closest_zone(lat, lon, airspace_data)
weather_data = get_live_weather(lat, lon)
kp_index_live = get_live_kp_index()

if weather_data:
    current = weather_data['current']
    daily = weather_data.get('daily', {})
    
    temp = current['temperature_2m']
    wind_kmh = current['wind_speed_10m']
    gust_kmh = current['wind_gusts_10m']
    rain = current['precipitation']
    dew_point = current.get('dew_point_2m', temp)
    elevation = weather_data.get('elevation', 0)
    
    is_daylight = True
    if daily.get('sunset', [None])[0] and daily.get('sunrise', [None])[0]:
        most_iso = datetime.now().strftime("%Y-%m-%dT%H:%M")
        if most_iso > daily['sunset'][0] or most_iso < daily['sunrise'][0]:
            is_daylight = False
        
    status, message, color = calculate_flight_index(wind_kmh, gust_kmh, temp, visibility_km, kp_index_live, rain, min_tavolsag, dew_point, is_daylight)
        
    st.header(f"📍 Tervezett repülési terület feltételei")
    st.subheader(f"{status}")
    st.write(f"**Indoklás:** {message}")
    st.divider()
        
    col_map, col_data = st.columns([1.5, 1]) 
    with col_map:
        m = folium.Map(location=[lat, lon], zoom_start=12)
        if airspace_data:
            folium.GeoJson(airspace_data, style_function=lambda x: {'fillColor': '#ff0000', 'color': '#ff0000', 'weight': 2, 'fillOpacity': 0.3}).add_to(m)
        folium.Marker([lat, lon], tooltip="Tervezett UAV Pozíció", icon=folium.Icon(color="blue", icon="plane")).add_to(m)
        st_folium(m, height=480, use_container_width=True)

    with col_data:
        st.markdown("### ☁️ Környezeti Adatok")
        zona_szin = "🟢" if min_tavolsag > 300 else "🔴"
        st.metric("Legközelebbi Zóna", f"{legkozelebbi_zona}")
        if min_tavolsag != float('inf'): st.metric("Távolság a határtól", f"{int(min_tavolsag)} m {zona_szin}")
        st.metric("Hőmérséklet / Csapadék", f"{temp} °C / {rain} mm")
        st.metric("Szél / Lökés", f"{wind_kmh} / {gust_kmh} km/h")
        st.metric("Kp-index (GPS)", f"{kp_index_live}")

# --- ADATBÁZIS MEGJELENÍTÉSE ---
st.divider()
st.header("🗄️ Szinkronizált Terepi Adatbázis (Google Drive-ról)")
LIMIT = 10

st.subheader("🌍 Beküldött Telemetriai Állapotok")
tel_data, tel_total = get_paginated_data("telemetry", LIMIT, st.session_state['page_tel'] * LIMIT)
if tel_data:
    st.dataframe(tel_data, use_container_width=True)
else:
    st.warning("Még nem töltöttél le adatbázist, vagy az adatbázis üres. Kattints az oldalsávban a Frissítés gombra!")

st.subheader("📡 Beküldött BLE Rádiófrekvenciás Napló")
ble_data, ble_total = get_paginated_data("ble_logs", LIMIT, st.session_state['page_ble'] * LIMIT)
if ble_data:
    st.dataframe(ble_data, use_container_width=True)
