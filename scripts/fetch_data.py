#!/usr/bin/env python3
"""
CLIMA TACTICO - Obtencion de datos para capas "horneadas" (2x/dia).

Genera archivos GeoJSON en ./data que el mapa lee de forma estatica.
NO incluye las capas "en vivo" (sismos USGS y alertas NWS), que el navegador
consulta directamente cada minuto.

Disenado para correr dentro de GitHub Actions. Sin dependencias externas:
solo biblioteca estandar de Python 3. Cada fuente esta aislada en try/except,
de modo que si una falla, el resto se genera igual y el sitio nunca queda roto.

Variables de entorno:
  FIRMS_MAP_KEY   Clave gratuita de NASA FIRMS (requerida para incendios).
                  Conseguir en: https://firms.modaps.eosdis.nasa.gov/api/map_key/
  FIRMS_WORLD     "1" (default) para incluir incendios globales (limitados).
"""

import csv
import io
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
import re
import hashlib
import unicodedata
from datetime import datetime, timezone; import calendar; from email.utils import parsedate_tz, mktime_tz; _parse_epoch = lambda s: (calendar.timegm(time.strptime(s.strip().rstrip("Z"), "%Y%m%dT%H%M%S")) if (s and re.match(r"^\d{8}T\d{6}Z?$", s.strip())) else (mktime_tz(parsedate_tz(s)) if (s and parsedate_tz(s)) else None)); _is_recent = lambda date_str, max_days=5: (lambda ep: True if ep is None else ((time.time()-ep) <= max_days*86400))(_parse_epoch(date_str))

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
USER_AGENT = "clima-tactico/1.0 (mapa de riesgos; GitHub Pages)"
TIMEOUT = 45

# -------------------------------------------------------------------------
# Utilidades
# -------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_get(url, headers=None, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
    return raw if binary else raw.decode("utf-8", errors="replace")


def write_geojson(name, features, extra_meta=None, preserve_if_empty=False):
    fc = {
        "type": "FeatureCollection",
        "metadata": {"generated": now_iso(), "count": len(features), **(extra_meta or {})},
        "features": features,
    }
    path = os.path.join(DATA_DIR, name)
    if preserve_if_empty and not features and os.path.exists(path):
        print(f" [{name}] sin datos nuevos: se preserva archivo existente")
        return 0
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False)
    return len(features)


def feature(lon, lat, props):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(float(lon), 4), round(float(lat), 4)]},
        "properties": props,
    }


def _norm_lon(lon):
    """Normaliza la longitud al rango [-180, 180].
    Evita que trayectorias con lon < -180 (p. ej. Pacífico occidental visto
    desde el este) generen franjas horizontales en Leaflet al cruzar el antimeridiano."""
    lon = float(lon)
    while lon > 180:
        lon -= 360
    while lon < -180:
        lon += 360
    return round(lon, 4)


def line_feature(coords, props):
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[_norm_lon(lo), round(float(la), 4)] for lo, la in coords]},
        "properties": props,
    }


def polygon_feature(ring, props):
    coords = [[round(float(lo), 4), round(float(la), 4)] for lo, la in ring]
    if coords and coords[0] != coords[-1]:
        coords.append(coords[0])
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [coords]},
        "properties": props,
    }

def _tc_category(wind_kt):
    try:
        w = float(wind_kt)
    except Exception:
        return "Desconocida"
    if w >= 137: return "Categoria 5"
    if w >= 113: return "Categoria 4"
    if w >= 96:  return "Categoria 3"
    if w >= 83:  return "Categoria 2"
    if w >= 64:  return "Categoria 1 (huracan)"
    if w >= 34:  return "Tormenta tropical"
    return "Depresion tropical"


def _kmz_to_kml_text(url):
    raw = http_get(url, binary=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".kml")]
        if not names:
            return ""
        return z.read(names[0]).decode("utf-8", errors="replace")


def _parse_wind_radii_kml(text):
    out = []
    for block in re.findall(r"<Placemark>(.*?)</Placemark>", text, re.S):
        nm = re.search(r"<name>\s*(\d+)\s*</name>", block)
        if not nm:
            continue
        cm = re.search(r"<coordinates>\s*([\s\S]*?)\s*</coordinates>", block)
        if not cm:
            continue
        ring = []
        for pair in cm.group(1).split():
            parts = pair.split(",")
            if len(parts) < 2:
                continue
            try:
                ring.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
        if len(ring) >= 3:
            out.append({"kt": int(nm.group(1)), "ring": ring})
    return out

def _parse_forecast_kml(text):
    pts = []
    for block in re.findall(r"<Placemark>(.*?)</Placemark>", text, re.S):
        if "<Point>" not in block:
            continue
        cm = re.search(r"<Point>\s*<coordinates>\s*([\-0-9.]+),([\-0-9.]+)", block)
        if not cm:
            continue
        lon, lat = float(cm.group(1)), float(cm.group(2))
        sm = re.search(r"<styleUrl>#(\w+)</styleUrl>", block)
        style = sm.group(1) if sm else ""
        is_initial = style == "initial_point"
        hm = re.search(r"(\d+)\s*hr Forecast", block)
        hours = 0 if is_initial else (int(hm.group(1)) if hm else None)
        wm = re.search(r"Maximum Wind:\s*([0-9]+)\s*knots", block)
        wind = int(wm.group(1)) if wm else None
        vm = re.search(r"Valid at:\s*([^<\n]+?)\s*(?:</td>|\n)", block)
        valid_txt = vm.group(1).strip() if vm else ""
        pts.append({
            "lon": lon, "lat": lat, "hours": hours,
            "label": "Ahora" if is_initial else (f"+{hours}h" if hours is not None else "?"),
            "wind_kt": wind, "category": _tc_category(wind) if wind is not None else "Desconocida",
            "valid_text": valid_txt,
        })
    return pts


def _parse_besttrack_kml(text, hours_limit=72):
    pts = []
    now = datetime.now(timezone.utc)
    for block in re.findall(r"<Placemark>(.*?)</Placemark>", text, re.S):
        dm = re.search(r"<atcfdtg>(\d{10})</atcfdtg>", block)
        lam = re.search(r"<lat>([\-0-9.]+)</lat>", block)
        lom = re.search(r"<lon>([\-0-9.]+)</lon>", block)
        if not (dm and lam and lom):
            continue
        dtg = dm.group(1)
        try:
            tt = datetime(int(dtg[0:4]), int(dtg[4:6]), int(dtg[6:8]), int(dtg[8:10]), tzinfo=timezone.utc)
        except Exception:
            continue
        age_h = (now - tt).total_seconds() / 3600.0
        if age_h < 0 or age_h > hours_limit:
            continue
        wm = re.search(r"<intensity>([0-9]+)</intensity>", block)
        wind = int(wm.group(1)) if wm else None
        pts.append({
            "lon": float(lom.group(1)), "lat": float(lam.group(1)),
            "time_iso": tt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hours_ago": round(age_h, 1),
            "wind_kt": wind, "category": _tc_category(wind) if wind is not None else "Desconocida",
        })
    pts.sort(key=lambda p: p["time_iso"])
    return pts


def fetch_ash_sigmets():
    try:
        raw = http_get("https://aviationweather.gov/api/data/isigmet?format=json&hazard=va")
        data = json.loads(raw) if raw else []
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"  [ceniza] fallo: {e}")
        return []


def _match_ash(name, ash_list):
    n = re.sub(r"\(.*?\)", "", name).strip().upper()
    n = n.replace("VOLCAN DE ", "").replace("NEVADO DE ", "")
    first_word = n.split()[0] if n.split() else n
    for a in ash_list:
        q = (a.get("qualifier") or "").strip().upper()
        if not q:
            continue
        if q in n or n in q or q == first_word or first_word in q:
            return a
    return None


# -------------------------------------------------------------------------
# 1) INCENDIOS  - NASA FIRMS (VIIRS NRT, ultimas 24 h)
# -------------------------------------------------------------------------

def fetch_fires():
    key = os.environ.get("FIRMS_MAP_KEY", "").strip()
    if not key:
        print("  [incendios] sin FIRMS_MAP_KEY: capa vacia")
        return write_geojson("fires.geojson", [], {"note": "Falta FIRMS_MAP_KEY"})

    base = "https://firms.modaps.eosdis.nasa.gov/api/country/csv"
    src = "VIIRS_SNPP_NRT"
    feats = []

    def parse_csv(text, scope):
        out = []
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            try:
                lat = float(row["latitude"]); lon = float(row["longitude"])
            except (KeyError, ValueError):
                continue
            frp = float(row.get("frp") or 0)
            out.append((frp, feature(lon, lat, {
                "layer": "fire",
                "frp": frp,
                "confidence": row.get("confidence", ""),
                "acq_date": row.get("acq_date", ""),
                "acq_time": row.get("acq_time", ""),
                "satellite": row.get("satellite", ""),
                "daynight": row.get("daynight", ""),
                "scope": scope,
            })))
        return out

    # Mexico (completo, cap 3000 por FRP)
    try:
        text = http_get(f"{base}/{key}/{src}/MEX/1")
        mx = parse_csv(text, "mx")
        mx.sort(key=lambda t: t[0], reverse=True)
        feats += [f for _, f in mx[:3000]]
        print(f"  [incendios] Mexico: {len(mx)} -> {min(len(mx),3000)}")
    except Exception as e:
        print(f"  [incendios] Mexico fallo: {e}")

    # Global (limitado a los mas intensos, cap 1500)
    if os.environ.get("FIRMS_WORLD", "1") == "1":
        try:
            text = http_get(f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{src}/world/1")
            wd = parse_csv(text, "world")
            wd.sort(key=lambda t: t[0], reverse=True)
            feats += [f for _, f in wd[:1500]]
            print(f"  [incendios] Mundo: {len(wd)} -> {min(len(wd),1500)}")
        except Exception as e:
            print(f"  [incendios] Mundo fallo: {e}")

    return write_geojson("fires.geojson", feats)


# -------------------------------------------------------------------------
# 2) CICLONES / HURACANES  - NOAA National Hurricane Center
# -------------------------------------------------------------------------

CLASS_MAP = {
    "HU": "Huracan", "TS": "Tormenta tropical", "TD": "Depresion tropical",
    "PTC": "Ciclon potencial", "STS": "Tormenta subtropical", "SD": "Depresion subtropical",
}

def fetch_storms():
    feats = []
    try:
        data = json.loads(http_get("https://www.nhc.noaa.gov/CurrentStorms.json"))
        for s in data.get("activeStorms", []):
            lat = s.get("latitudeNumeric") or s.get("latitude")
            lon = s.get("longitudeNumeric") or s.get("longitude")
            if lat is None or lon is None:
                continue
            cls = s.get("classification", "")
            feats.append(feature(lon, lat, {
                "layer": "storm",
                "name": s.get("name", "Sin nombre"),
                "classification": cls,
                "class_label": CLASS_MAP.get(cls, cls),
                "intensity_kt": s.get("intensity", ""),
                "pressure_mb": s.get("pressure", ""),
                "movement": f"{s.get('movementDir','?')} {s.get('movementSpeed','?')} kt",
                "basin": s.get("id", "")[:2],
                "last_update": s.get("lastUpdate", ""),
            }))
        print(f"  [ciclones] activos: {len(feats)}")
    except Exception as e:
        print(f"  [ciclones] fallo: {e}")
    return write_geojson("storms.geojson", feats)


def fetch_storm_tracks():
    feats = []
    try:
        data = json.loads(http_get("https://www.nhc.noaa.gov/CurrentStorms.json"))
    except Exception as e:
        print(f"  [trayectorias] fallo lista de tormentas: {e}")
        return write_geojson("storm_tracks.geojson", feats)

    for s in data.get("activeStorms", []):
        sid = s.get("id", "")
        sname = s.get("name", "Sin nombre")
        fpts = []

        try:
            ft = (s.get("forecastTrack") or {}).get("kmzFile")
            if ft:
                fpts = _parse_forecast_kml(_kmz_to_kml_text(ft))
                if fpts:
                    feats.append(line_feature([(p["lon"], p["lat"]) for p in fpts], {
                        "layer": "storm_track", "kind": "forecast", "storm_id": sid, "storm_name": sname,
                    }))
                    for p in fpts:
                        feats.append(feature(p["lon"], p["lat"], {
                            "layer": "storm_track", "kind": "forecast_point",
                            "storm_id": sid, "storm_name": sname,
                            "label": p["label"], "wind_kt": p["wind_kt"], "category": p["category"],
                            "valid_text": p["valid_text"],
                        }))
        except Exception as e:
            print(f"  [trayectorias] fallo pronostico {sname}: {e}")

        try:
            iw = (s.get("initialWindExtent") or {}).get("kmzFile")
            if iw:
                for rg in _parse_wind_radii_kml(_kmz_to_kml_text(iw)):
                    feats.append(polygon_feature(rg["ring"], {
                        "layer": "storm_track", "kind": "wind_radii_current",
                        "storm_id": sid, "storm_name": sname, "wind_kt": rg["kt"],
                    }))
        except Exception as e:
            print(f"  [trayectorias] fallo radios de viento actuales {sname}: {e}")

        try:
            fw = (s.get("forecastWindRadiiGIS") or {}).get("kmzFile")
            if fw and fpts:
                groups = _parse_wind_radii_kml(_kmz_to_kml_text(fw))
                per_period = [groups[i:i+3] for i in range(0, len(groups), 3)]
                future_pts = [p for p in fpts if p.get("hours")]
                for i, grp in enumerate(per_period):
                    if i >= len(future_pts):
                        break
                    target = future_pts[i]
                    for rg in grp:
                        feats.append(polygon_feature(rg["ring"], {
                            "layer": "storm_track", "kind": "wind_radii_forecast",
                            "storm_id": sid, "storm_name": sname, "wind_kt": rg["kt"],
                            "label": target.get("label"), "valid_text": target.get("valid_text"),
                        }))
        except Exception as e:
            print(f"  [trayectorias] fallo radios de viento pronostico {sname}: {e}")

        try:
            bt = (s.get("bestTrackGIS") or {}).get("kmzFile")
            if bt:
                bpts = _parse_besttrack_kml(_kmz_to_kml_text(bt), hours_limit=72)
                if bpts:
                    feats.append(line_feature([(p["lon"], p["lat"]) for p in bpts], {
                        "layer": "storm_track", "kind": "past", "storm_id": sid, "storm_name": sname,
                    }))
                    for p in bpts:
                        feats.append(feature(p["lon"], p["lat"], {
                            "layer": "storm_track", "kind": "past_point",
                            "storm_id": sid, "storm_name": sname,
                            "time_iso": p["time_iso"], "hours_ago": p["hours_ago"],
                            "wind_kt": p["wind_kt"], "category": p["category"],
                        }))
        except Exception as e:
            print(f"  [trayectorias] fallo historico {sname}: {e}")

    print(f"  [trayectorias] features: {len(feats)}")
    return write_geojson("storm_tracks.geojson", feats)


# -------------------------------------------------------------------------
# 3) MULTI-AMENAZA GLOBAL  - GDACS (inundaciones, ciclones, sequia, etc.)
# -------------------------------------------------------------------------

GDACS_TYPE = {
    "EQ": "Sismo", "TC": "Ciclon tropical", "FL": "Inundacion",
    "VO": "Volcan", "DR": "Sequia", "WF": "Incendio forestal", "TS": "Tsunami",
    # tipos adicionales que GDACS puede reportar
    "LS": "Deslizamiento",    # Landslide
    "AV": "Avalancha",        # Avalanche / snow avalanche
    "FF": "Inundacion rapida",# Flash Flood
    "MS": "Movimiento de masa",# Mass movement / rockfall / debris
    "SS": "Marejada",         # Storm Surge / swell
    "GL": "Desprendimiento glaciar",  # Glacial Lake Outburst Flood / glaciar
}

def fetch_gdacs():
    feats = []
    try:
        raw = json.loads(http_get("https://www.gdacs.org/gdacsapi/api/events/geteventlist/MAP"))
        items = raw.get("features", raw if isinstance(raw, list) else [])
        for it in [x for x in items if _is_recent(x.get("date"))]:
            props = it.get("properties", it)
            geom = it.get("geometry") or {}
            coords = geom.get("coordinates")
            if not coords or len(coords) < 2:
                lat = props.get("latitude"); lon = props.get("longitude")
                if lat is None or lon is None:
                    continue
                coords = [lon, lat]
            et = props.get("eventtype", "")
            feats.append(feature(coords[0], coords[1], {
                "layer": "gdacs",
                "eventtype": et,
                "type_label": GDACS_TYPE.get(et, et),
                "alertlevel": props.get("alertlevel", "Green"),
                "name": props.get("name") or props.get("eventname") or props.get("htmldescription", ""),
                "country": props.get("country", ""),
                "fromdate": props.get("fromdate", ""),
                "url": props.get("url", {}).get("report", "") if isinstance(props.get("url"), dict) else "",
            }))
        print(f"  [gdacs] eventos: {len(feats)}")
    except Exception as e:
        print(f"  [gdacs] fallo: {e}")
    return write_geojson("gdacs.geojson", feats)


# -------------------------------------------------------------------------
# 4) PRONOSTICO 7 DIAS  - Open-Meteo (lluvia, viento, nortes, tormentas)
# -------------------------------------------------------------------------

# Puntos de interes: ciudades / destinos MX + capitales y destinos globales.
GRID = [
    # --- Mexico ---
    ("Ciudad de Mexico", 19.43, -99.13), ("Guadalajara", 20.67, -103.35),
    ("Monterrey", 25.69, -100.32), ("Cancun", 21.16, -86.85),
    ("Tijuana", 32.51, -117.04), ("Merida", 20.97, -89.62),
    ("Acapulco", 16.86, -99.88), ("Puerto Vallarta", 20.65, -105.22),
    ("Los Cabos", 22.89, -109.91), ("Oaxaca", 17.07, -96.72),
    ("Veracruz", 19.17, -96.13), ("Tuxtla Gutierrez", 16.75, -93.12),
    ("Chihuahua", 28.63, -106.08), ("Hermosillo", 29.07, -110.96),
    ("Tampico", 22.25, -97.87), ("Mazatlan", 23.25, -106.41),
    ("Queretaro", 20.59, -100.39), ("Puebla", 19.04, -98.21),
    ("La Paz", 24.14, -110.31), ("Villahermosa", 17.99, -92.93),
    # --- Global ---
    ("Madrid", 40.42, -3.70), ("Londres", 51.51, -0.13), ("Paris", 48.85, 2.35),
    ("Nueva York", 40.71, -74.01), ("Los Angeles", 34.05, -118.24),
    ("Miami", 25.76, -80.19), ("Tokio", 35.68, 139.69), ("Pekin", 39.90, 116.41),
    ("Sao Paulo", -23.55, -46.63), ("Buenos Aires", -34.60, -58.38),
    ("Bogota", 4.71, -74.07), ("Lima", -12.05, -77.04), ("Santiago", -33.45, -70.67),
    ("Manila", 14.60, 120.98), ("Mumbai", 19.08, 72.88), ("El Cairo", 30.04, 31.24),
    ("Sidney", -33.87, 151.21), ("Roma", 41.90, 12.50), ("Berlin", 52.52, 13.40),
    ("Toronto", 43.65, -79.38), ("Houston", 29.76, -95.37), ("Nueva Orleans", 29.95, -90.07),
    ("Yakarta", -6.21, 106.85), ("Bangkok", 13.76, 100.50),
]

def risk_level(max_rain, max_gust):
    # Devuelve (nivel 0-4, etiqueta) estilo alerta MX (Verde/Amarilla/Naranja/Roja/Morada)
    lvl = 0
    if max_rain >= 70 or max_gust >= 110:
        lvl = 4
    elif max_rain >= 50 or max_gust >= 90:
        lvl = 3
    elif max_rain >= 25 or max_gust >= 65:
        lvl = 2
    elif max_rain >= 10 or max_gust >= 45:
        lvl = 1
    return lvl, ["Verde (sin riesgo relevante)", "Amarilla (vigilancia)", "Naranja (riesgo)", "Roja (peligro)", "Morada (extraordinario)"][lvl]

def temp_risk(max_tmax, min_tmin):
    # Clasifica alerta de temperatura (calor/frio) por umbrales fijos en grados C
    heat_lvl, heat_label = 0, None
    if max_tmax is not None:
        if max_tmax >= 45:
            heat_lvl, heat_label = 3, "Severo (calor extremo)"
        elif max_tmax >= 40:
            heat_lvl, heat_label = 2, "Riesgo (calor intenso)"
        elif max_tmax >= 35:
            heat_lvl, heat_label = 1, "Vigilancia (calor)"
    cold_lvl, cold_label = 0, None
    if min_tmin is not None:
        if min_tmin <= -10:
            cold_lvl, cold_label = 3, "Severo (frio extremo)"
        elif min_tmin <= 0:
            cold_lvl, cold_label = 2, "Riesgo (helada)"
        elif min_tmin <= 5:
            cold_lvl, cold_label = 1, "Vigilancia (frio)"
    if heat_lvl >= cold_lvl and heat_lvl > 0:
        return heat_lvl, heat_label, "calor"
    if cold_lvl > 0:
        return cold_lvl, cold_label, "frio"
    return 0, None, None

def fetch_forecast():
    feats = []
    try:
        lats = ",".join(f"{p[1]}" for p in GRID)
        lons = ",".join(f"{p[2]}" for p in GRID)
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lats}&longitude={lons}"
            "&daily=precipitation_sum,precipitation_probability_max,"
            "wind_speed_10m_max,wind_gusts_10m_max,weather_code,temperature_2m_max,temperature_2m_min"
            "&forecast_days=7&timezone=auto"
        )
        res = json.loads(http_get(url))
        if isinstance(res, dict):
            res = [res]
        for i, point in enumerate(res):
            if i >= len(GRID):
                break
            name, lat, lon = GRID[i]
            daily = point.get("daily", {})
            rain = daily.get("precipitation_sum", []) or []
            pop = daily.get("precipitation_probability_max", []) or []
            gust = daily.get("wind_gusts_10m_max", []) or []
            wind = daily.get("wind_speed_10m_max", []) or []
            tmax = daily.get("temperature_2m_max", []) or []
            tmin = daily.get("temperature_2m_min", []) or []
            wcode = daily.get("weather_code", []) or []
            dates = daily.get("time", []) or []
            max_rain = max(rain) if rain else 0
            max_gust = max(gust) if gust else 0
            max_tmax = max(tmax) if tmax else None
            min_tmin = min(tmin) if tmin else None
            temp_lvl, temp_label, temp_kind = temp_risk(max_tmax, min_tmin)
            lvl, label = risk_level(max_rain, max_gust)
            days = []
            for d in range(len(dates)):
                days.append({
                    "date": dates[d],
                    "rain": rain[d] if d < len(rain) else None,
                    "pop": pop[d] if d < len(pop) else None,
                    "wind": wind[d] if d < len(wind) else None,
                    "gust": gust[d] if d < len(gust) else None,
                    "tmax": tmax[d] if d < len(tmax) else None,
                    "tmin": tmin[d] if d < len(tmin) else None,
                    "code": wcode[d] if d < len(wcode) else None,
                })
            feats.append(feature(lon, lat, {
                "layer": "forecast",
                "name": name,
                "level": lvl,
                "level_label": label,
                "max_rain_mm": round(max_rain, 1),
                "max_gust_kmh": round(max_gust, 1),
                "max_tmax_c": round(max_tmax, 1) if max_tmax is not None else None,
                "min_tmin_c": round(min_tmin, 1) if min_tmin is not None else None,
                "temp_level": temp_lvl,
                "temp_label": temp_label,
                "temp_kind": temp_kind,
                "days": days,
            }))
        print(f"  [pronostico] puntos: {len(feats)}")
    except Exception as e:
        print(f"  [pronostico] fallo: {e}")
    return write_geojson("forecast.geojson", feats)


# -------------------------------------------------------------------------
# 5) VOLCANES  - lista curada de volcanes activos notables
#    (GVP/CENAPRED no exponen API limpia; se incluye base estable + enlaces)
# -------------------------------------------------------------------------

VOLCANOES = [
    # ── México: activos / con actividad histórica reciente / monitoreados por CENAPRED ──
    ("Popocatepetl", 19.0222, -98.6222, "Mexico", "https://www.gob.mx/cenapred"),
    ("Colima (Volcan de Fuego)", 19.5144, -103.6167, "Mexico", "https://volcano.si.edu"),
    ("El Chichon", 17.3600, -93.2275, "Mexico", "https://volcano.si.edu"),
    ("Tacana", 15.1322, -92.1103, "Mexico", "https://volcano.si.edu"),
    ("Ceboruco", 21.1250, -104.5079, "Mexico", "https://www.gob.mx/cenapred"),
    ("Las Tres Virgenes", 27.4736, -112.5928, "Mexico", "https://volcano.si.edu"),
    ("San Martin Tuxtla", 18.5667, -95.2000, "Mexico", "https://volcano.si.edu"),
    ("Pico de Orizaba (Citlaltepetl)", 19.0253, -97.2686, "Mexico", "https://www.gob.mx/cenapred"),
    ("El Jorullo", 18.9747, -101.7172, "Mexico", "https://volcano.si.edu"),
    ("Paricutin", 19.4889, -102.2508, "Mexico", "https://volcano.si.edu"),
    # ── América Central ──
    ("Fuego", 14.473, -90.880, "Guatemala", "https://volcano.si.edu"),
    ("Pacaya", 14.382, -90.601, "Guatemala", "https://volcano.si.edu"),
    # ── EE.UU. ──
    ("Kilauea", 19.421, -155.287, "EE.UU.", "https://volcanoes.usgs.gov"),
    ("Mauna Loa", 19.475, -155.608, "EE.UU.", "https://volcanoes.usgs.gov"),
    # ── Europa ──
    ("Etna", 37.748, 14.999, "Italia", "https://volcano.si.edu"),
    ("Stromboli", 38.789, 15.213, "Italia", "https://volcano.si.edu"),
    ("Reykjanes / Fagradalsfjall", 63.900, -22.270, "Islandia", "https://volcano.si.edu"),
    # ── Asia / Oceanía ──
    ("Sakurajima", 31.585, 130.657, "Japon", "https://volcano.si.edu"),
    ("Merapi", -7.540, 110.446, "Indonesia", "https://volcano.si.edu"),
    # ── América del Sur ──
    ("Villarrica", -39.420, -71.930, "Chile", "https://volcano.si.edu"),
    ("Cotopaxi", -0.677, -78.436, "Ecuador", "https://volcano.si.edu"),
    # ── África ──
    ("Nyiragongo", -1.520, 29.250, "RD Congo", "https://volcano.si.edu"),
]

VOLCANOES_INACTIVOS = [
    # ── México: inactivos / dormidos ──
    ("Iztaccihuatl", 19.1789, -98.6422, "Mexico", "https://www.gob.mx/cenapred"),
    ("Nevado de Toluca (Xinantecatl)", 19.1081, -99.7578, "Mexico", "https://www.gob.mx/cenapred"),
    ("La Malinche (Matlalcueitl)", 19.2325, -98.0164, "Mexico", "https://www.gob.mx/cenapred"),
    ("Cofre de Perote (Nauhcampatepetl)", 19.4922, -97.1533, "Mexico", "https://volcano.si.edu"),
    ("Nevado de Colima", 19.5422, -103.6069, "Mexico", "https://volcano.si.edu"),
    ("Ajusco", 19.2075, -99.2622, "Mexico", "https://www.gob.mx/cenapred"),
    ("Sanganguey", 22.0489, -104.7347, "Mexico", "https://volcano.si.edu"),
    ("Volcan Tequila", 20.7444, -103.8514, "Mexico", "https://volcano.si.edu"),
    ("Sierra Chichinautzin", 19.0800, -99.1000, "Mexico", "https://volcano.si.edu"),
]

def fetch_volcanoes():
    feats = []
    ash_list = fetch_ash_sigmets()
    combined = [(n, la, lo, c, u, "activo") for (n, la, lo, c, u) in VOLCANOES]
    combined += [(n, la, lo, c, u, "inactivo") for (n, la, lo, c, u) in VOLCANOES_INACTIVOS]
    for name, lat, lon, country, url, status in combined:
        props = {
            "layer": "volcano",
            "name": name,
            "country": country,
            "url": url,
            "status": status,
            "note": ("Nivel de actividad: consultar fuente oficial (CENAPRED/GVP)." if status == "activo"
                     else "Volcan inactivo/dormido: sin monitoreo de erupcion en tiempo real."),
        }
        ash = _match_ash(name, ash_list) if status == "activo" else None
        if ash:
            top_ft = ash.get("top")
            fl = round((top_ft or 0) / 100)
            valid_to = ash.get("validTimeTo")
            valid_iso = ""
            try:
                valid_iso = datetime.fromtimestamp(int(valid_to), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                pass
            props["ash_active"] = True
            props["ash_top_ft"] = top_ft
            props["ash_fl"] = fl
            props["ash_dir"] = ash.get("dir")
            props["ash_spd"] = ash.get("spd")
            props["ash_valid_iso"] = valid_iso
            props["ash_note"] = (f"Aviso de ceniza (SIGMET) vigente: tope aprox. FL{fl} "
                                  f"({top_ft} ft), se desplaza hacia {ash.get('dir') or '?'} "
                                  f"a {ash.get('spd') or '?'} kt.")
        else:
            props["ash_active"] = False
            props["ash_note"] = "Sin ceniza reportada actualmente (SIGMET)."
        feats.append(feature(lon, lat, props))
    print(f"  [volcanes] puntos: {len(feats)}")
    return write_geojson("volcanoes.geojson", feats)


# -------------------------------------------------------------------------
# 6) CALIDAD DEL AIRE  - Open-Meteo Air Quality (US AQI, PM2.5, PM10, O3...)
# -------------------------------------------------------------------------

def aqi_cat(aqi):
    if aqi is None:
        return (0, "Sin dato", "#6f8a82")
    if aqi <= 50:   return (0, "Buena", "#46e0a0")
    if aqi <= 100:  return (1, "Moderada", "#ffd23f")
    if aqi <= 150:  return (2, "Danina (sensibles)", "#ff8c2b")
    if aqi <= 200:  return (3, "Danina", "#ff4242")
    if aqi <= 300:  return (4, "Muy danina", "#b56cff")
    return (5, "Peligrosa", "#8b1a1a")

def fetch_airquality():
    feats = []
    try:
        lats = ",".join(f"{p[1]}" for p in GRID)
        lons = ",".join(f"{p[2]}" for p in GRID)
        url = (
            "https://air-quality-api.open-meteo.com/v1/air-quality"
            f"?latitude={lats}&longitude={lons}"
            "&current=us_aqi,pm2_5,pm10,ozone,nitrogen_dioxide,sulphur_dioxide,carbon_monoxide"
            "&timezone=auto"
        )
        res = json.loads(http_get(url))
        if isinstance(res, dict):
            res = [res]
        for i, pt in enumerate(res):
            if i >= len(GRID):
                break
            name, lat, lon = GRID[i]
            cur = pt.get("current", {}) or {}
            aqi = cur.get("us_aqi")
            lvl, label, color = aqi_cat(aqi)
            feats.append(feature(lon, lat, {
                "layer": "air", "name": name, "us_aqi": aqi,
                "level": lvl, "level_label": label, "color": color,
                "pm2_5": cur.get("pm2_5"), "pm10": cur.get("pm10"),
                "ozone": cur.get("ozone"), "no2": cur.get("nitrogen_dioxide"),
                "so2": cur.get("sulphur_dioxide"), "co": cur.get("carbon_monoxide"),
            }))
        print(f"  [aire] puntos: {len(feats)}")
    except Exception as e:
        print(f"  [aire] fallo: {e}")
    return write_geojson("airquality.geojson", feats)


# -------------------------------------------------------------------------
# 7) CLIMA ESPACIAL  - NOAA SWPC (llamaradas/radio R, radiacion S, geomag. G)
# -------------------------------------------------------------------------

def fetch_space():
    out = {"generated": now_iso(),
           "R": {"scale": 0, "text": "none"}, "S": {"scale": 0, "text": "none"},
           "G": {"scale": 0, "text": "none"}, "kp": None, "flare": None, "predicted": []}

    # Escalas NOAA R/S/G (actual = clave "0"; pronostico = "1".."3")
    try:
        sc = json.loads(http_get("https://services.swpc.noaa.gov/products/noaa-scales.json"))
        cur = sc.get("0", {}) or {}
        for k in ("R", "S", "G"):
            d = cur.get(k, {}) or {}
            out[k] = {"scale": int(d.get("Scale") or 0), "text": d.get("Text") or "none"}
        for day in ("1", "2", "3"):
            d = sc.get(day)
            if d:
                out["predicted"].append({
                    "date": d.get("DateStamp"),
                    "R": int((d.get("R") or {}).get("Scale") or 0),
                    "S": int((d.get("S") or {}).get("Scale") or 0),
                    "G": int((d.get("G") or {}).get("Scale") or 0),
                })
    except Exception as e:
        print(f"  [espacial] escalas fallo: {e}")

    # Indice planetario Kp (tormenta geomagnetica: Kp>=5)
    try:
        kp = json.loads(http_get("https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"))
        if isinstance(kp, list) and len(kp) > 1:
            last = kp[-1]
            out["kp"] = {"value": float(last[1]), "time": last[0]}
    except Exception as e:
        print(f"  [espacial] kp fallo: {e}")

    # Ultima llamarada de rayos X (clase C/M/X)
    try:
        fl = json.loads(http_get("https://services.swpc.noaa.gov/json/goes/primary/xray-flares-latest.json"))
        rec = fl[0] if isinstance(fl, list) and fl else (fl if isinstance(fl, dict) else None)
        if rec:
            out["flare"] = {"class": rec.get("max_class") or rec.get("current_class"),
                            "time": rec.get("max_time") or rec.get("time_tag")}
    except Exception as e:
        print(f"  [espacial] llamarada fallo: {e}")

    with open(os.path.join(DATA_DIR, "space.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"  [espacial] R{out['R']['scale']} S{out['S']['scale']} G{out['G']['scale']} kp={out['kp']}")
    return max(out["R"]["scale"], out["S"]["scale"], out["G"]["scale"])


# -------------------------------------------------------------------------
# 8) AVISOS DE VIAJE  - travel-advisory.info (riesgo por pais 0-5, multi-gob.)
# -------------------------------------------------------------------------

def advisory_level(score):
    if score is None: return 0
    if score < 2.5: return 1     # precaucion normal
    if score < 3.5: return 2     # mayor precaucion
    if score < 4.5: return 3     # reconsiderar viaje
    return 4                     # no viajar

def fetch_advisories():
    data = {}
    try:
        raw = json.loads(http_get("https://www.travel-advisory.info/api"))
        for iso, info in (raw.get("data") or {}).items():
            adv = info.get("advisory", {}) or {}
            score = adv.get("score")
            data[iso] = {
                "name": info.get("name"), "score": score,
                "level": advisory_level(score),
                "message": (adv.get("message") or "")[:300],
                "source": adv.get("source"), "updated": adv.get("updated"),
            }
        print(f"  [avisos] paises: {len(data)}")
    except Exception as e:
        print(f"  [avisos] fallo: {e}")
    with open(os.path.join(DATA_DIR, "advisories.json"), "w", encoding="utf-8") as f:
        json.dump({"generated": now_iso(), "data": data}, f, ensure_ascii=False)
    return len(data)


# -------------------------------------------------------------------------
# 9) SEGURIDAD (SENALES DE NOTICIAS)  - GDELT GEO + feed Google News / GDELT
#    NOTA: son senales de cobertura noticiosa, NO incidentes confirmados.
# -------------------------------------------------------------------------

SEC_QUERY = ('(cartel OR kidnapping OR "armed robbery" OR shooting OR extortion OR '
             'roadblock OR blockade OR homicide OR carjacking OR protest OR "drug violence")')

def fetch_security():
    feats = []
    try:
        q = urllib.parse.quote(SEC_QUERY)
        url = f"https://api.gdeltproject.org/api/v2/geo/geo?query={q}&format=GeoJSON&mode=PointData&timespan=1440"
        gj = json.loads(http_get(url))
        pts = []
        for f in gj.get("features", []):
            g = f.get("geometry") or {}
            c = g.get("coordinates")
            if not c or len(c) < 2:
                continue
            p = f.get("properties", {}) or {}
            cnt = int(p.get("count") or 1)
            name = p.get("name") or p.get("location") or ""
            pts.append((cnt, feature(c[0], c[1], {"layer": "security", "name": name, "count": cnt})))
        pts.sort(key=lambda t: t[0], reverse=True)
        feats = [f for _, f in pts[:400]]
        print(f"  [seguridad] puntos: {len(gj.get('features', []))} -> {len(feats)}")
    except Exception as e:
        print(f"  [seguridad] geo fallo: {e}")
    write_geojson("security.geojson", feats, preserve_if_empty=True)
    fetch_security_feed()
    return len(feats)


MX_STATE_CENTROIDS = {
    "Aguascalientes": (21.8853, -102.2916),
    "Baja California Sur": (26.0444, -111.6661),
    "Baja California": (30.8406, -115.2838),
    "Campeche": (19.8301, -90.5349),
    "Chiapas": (16.7569, -93.1292),
    "Chihuahua": (28.6329, -106.0691),
    "Ciudad de Mexico": (19.4326, -99.1332),
    "Coahuila": (27.0587, -101.7068),
    "Colima": (19.2452, -103.7241),
    "Durango": (24.5593, -104.6588),
    "Guanajuato": (21.0190, -101.2574),
    "Guerrero": (17.4392, -99.5451),
    "Hidalgo": (20.0911, -98.7624),
    "Jalisco": (20.6595, -103.3494),
    "Estado de Mexico": (19.4969, -99.7233),
    "Michoacan": (19.5665, -101.7068),
    "Morelos": (18.6813, -99.1013),
    "Nayarit": (21.7514, -104.8455),
    "Nuevo Leon": (25.5922, -99.9962),
    "Oaxaca": (17.0732, -96.7266),
    "Puebla": (19.0414, -98.2063),
    "Queretaro": (20.5888, -100.3899),
    "Quintana Roo": (19.1817, -88.4791),
    "San Luis Potosi": (22.1565, -100.9855),
    "Sinaloa": (25.1721, -107.4795),
    "Sonora": (29.2972, -110.3309),
    "Tabasco": (17.8409, -92.6189),
    "Tamaulipas": (24.2669, -98.8363),
    "Tlaxcala": (19.3139, -98.2404),
    "Veracruz": (19.1738, -96.1342),
    "Yucatan": (20.7099, -89.0943),
    "Zacatecas": (22.7709, -102.5832),
}

MX_CITY_TO_STATE = {
    "tijuana": "Baja California", "mexicali": "Baja California", "ensenada": "Baja California",
    "la paz": "Baja California Sur", "los cabos": "Baja California Sur",
    "campeche": "Campeche",
    "tuxtla gutierrez": "Chiapas", "san cristobal de las casas": "Chiapas", "tapachula": "Chiapas",
    "ciudad juarez": "Chihuahua", "juarez": "Chihuahua", "chihuahua": "Chihuahua",
    "saltillo": "Coahuila", "torreon": "Coahuila", "piedras negras": "Coahuila",
    "colima": "Colima", "manzanillo": "Colima",
    "durango": "Durango", "gomez palacio": "Durango",
    "leon": "Guanajuato", "irapuato": "Guanajuato", "celaya": "Guanajuato", "salamanca": "Guanajuato",
    "acapulco": "Guerrero", "chilpancingo": "Guerrero", "iguala": "Guerrero", "taxco": "Guerrero", "zihuatanejo": "Guerrero",
    "pachuca": "Hidalgo", "tulancingo": "Hidalgo",
    "guadalajara": "Jalisco", "zapopan": "Jalisco", "puerto vallarta": "Jalisco", "tlaquepaque": "Jalisco",
    "toluca": "Estado de Mexico", "ecatepec": "Estado de Mexico", "naucalpan": "Estado de Mexico", "tlalnepantla": "Estado de Mexico", "nezahualcoyotl": "Estado de Mexico",
    "morelia": "Michoacan", "uruapan": "Michoacan", "zamora": "Michoacan", "jacona": "Michoacan",
    "los reyes": "Michoacan", "lazaro cardenas": "Michoacan", "apatzingan": "Michoacan", "zitacuaro": "Michoacan",
    "cuernavaca": "Morelos", "cuautla": "Morelos",
    "tepic": "Nayarit",
    "monterrey": "Nuevo Leon", "san pedro garza garcia": "Nuevo Leon", "guadalupe": "Nuevo Leon", "apodaca": "Nuevo Leon",
    "oaxaca": "Oaxaca", "juchitan": "Oaxaca", "salina cruz": "Oaxaca",
    "puebla": "Puebla", "cholula": "Puebla", "tehuacan": "Puebla",
    "queretaro": "Queretaro",
    "cancun": "Quintana Roo", "playa del carmen": "Quintana Roo", "chetumal": "Quintana Roo", "tulum": "Quintana Roo", "cozumel": "Quintana Roo",
    "san luis potosi": "San Luis Potosi",
    "culiacan": "Sinaloa", "mazatlan": "Sinaloa", "los mochis": "Sinaloa",
    "hermosillo": "Sonora", "cajeme": "Sonora", "ciudad obregon": "Sonora", "nogales": "Sonora", "san luis rio colorado": "Sonora",
    "villahermosa": "Tabasco",
    "reynosa": "Tamaulipas", "matamoros": "Tamaulipas", "tampico": "Tamaulipas", "nuevo laredo": "Tamaulipas", "ciudad victoria": "Tamaulipas",
    "tlaxcala": "Tlaxcala",
    "veracruz": "Veracruz", "xalapa": "Veracruz", "coatzacoalcos": "Veracruz", "orizaba": "Veracruz", "cordoba": "Veracruz", "poza rica": "Veracruz", "minatitlan": "Veracruz",
    "merida": "Yucatan", "valladolid": "Yucatan",
    "zacatecas": "Zacatecas", "fresnillo": "Zacatecas",
    "aguascalientes": "Aguascalientes",
}

INCIDENT_TYPES = [
    ("BLOQUEO", ["bloque", "narcobloqueo", "cierran", "cierre", "carretera cerrada", "obstru", "toma de caseta", "toman la caseta"]),
    ("SECUESTRO", ["secuestr", "plagio"]),
    ("VIOLENCIA", ["balacera", "tiroteo", "enfrentamiento", "disparos", "homicidio", "asesinat", "ejecutad", "ejecucion", "masacre"]),
    ("ASALTO", ["asalto", "asaltan", "asaltar", " robo ", "roban", "atraco"]),
    ("EXTORSION", ["extorsion"]),
]


def _normalize_text(s):
    s = (s or "").lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return s


def _detect_mx_state(title):
    n = " " + _normalize_text(title) + " "
    for city, state in MX_CITY_TO_STATE.items():
        if city in n:
            return state
    for state in MX_STATE_CENTROIDS:
        if _normalize_text(state) in n:
            return state
    return None


def _classify_incident(title):
    n = _normalize_text(title)
    for kind, kws in INCIDENT_TYPES:
        for kw in kws:
            if kw in n:
                return kind
    return "OTRO"


def _jitter(lat, lon, seed_text):
    h = int(hashlib.md5(seed_text.encode("utf-8")).hexdigest(), 16)
    dlat = ((h % 1000) / 1000.0 - 0.5) * 0.5
    dlon = (((h // 1000) % 1000) / 1000.0 - 0.5) * 0.5
    return lat + dlat, lon + dlon


def fetch_security_feed():
    items = []
    # Titulares Mexico en espanol (Google News RSS)
    try:
        gq = urllib.parse.quote('bloqueo carretera OR balacera OR secuestro OR asalto OR '
                                 'narcobloqueo OR "crimen organizado" OR extorsion')
        rss = http_get(f"https://news.google.com/rss/search?q={gq}&hl=es-419&gl=MX&ceid=MX:es")
        root = ET.fromstring(rss)
        for it in root.iter("item"):
            src_el = it.find("source")
            items.append({
                "title": it.findtext("title") or "",
                "url": it.findtext("link") or "",
                "date": it.findtext("pubDate") or "",
                "source": (src_el.text if src_el is not None else "") or "Google News",
                "region": "MX",
            })
        print(f"  [feed] google news MX: {len(items)}")
    except Exception as e:
        print(f"  [feed] google news fallo: {e}")
    mx_items = [it for it in items if _is_recent(it.get("date"))]
    # Titulares globales (GDELT DOC)
    try:
        q = urllib.parse.quote(SEC_QUERY)
        doc = json.loads(http_get(
            f"https://api.gdeltproject.org/api/v2/doc/doc?query={q}"
            "&mode=artlist&format=json&timespan=1d&maxrecords=20&sort=datedesc"))
        for a in doc.get("articles", []):
            items.append({
                "title": a.get("title", ""), "url": a.get("url", ""),
                "date": a.get("seendate", ""), "source": a.get("domain", ""),
                "region": a.get("sourcecountry", ""),
            })
        print(f"  [feed] gdelt: {len(doc.get('articles', []))}")
    except Exception as e:
        print(f"  [feed] gdelt fallo: {e}")
    with open(os.path.join(DATA_DIR, "security_feed.json"), "w", encoding="utf-8") as f:
        json.dump({"generated": now_iso(), "items": [it for it in items if _is_recent(it.get("date"))][:40]}, f, ensure_ascii=False)

    # Mapa aproximado de incidentes (solo notas de Mexico; ubicacion a nivel
    # estado detectada por texto del titular; NO es la ubicacion exacta del hecho).
    map_feats = []
    try:
        for it in mx_items:
            title = it.get("title") or ""
            state = _detect_mx_state(title)
            if not state:
                continue
            base_lat, base_lon = MX_STATE_CENTROIDS[state]
            lat, lon = _jitter(base_lat, base_lon, title)
            kind = _classify_incident(title)
            map_feats.append(feature(lon, lat, {
                "title": title, "url": it.get("url") or "", "date": it.get("date") or "",
                "source": it.get("source") or "", "state": state, "kind": kind,
            }))
        print(f"  [feed] mapa seguridad MX: {len(map_feats)}/{len(mx_items)} ubicados")
    except Exception as e:
        print(f"  [feed] mapa seguridad fallo: {e}")
    write_geojson("security_map.geojson", map_feats, {"note": "Ubicacion aproximada a nivel estado, detectada por texto del titular. No es la ubicacion exacta del incidente."}, preserve_if_empty=True)

# -------------------------------------------------------------------------
# 10) CLIMA SEVERO (GRANIZO / TORNADO) - senales de noticias (GDELT + Google News)
#     NOTA: son senales de cobertura noticiosa, NO eventos confirmados ni una
#     medicion oficial de tamano de granizo. El SMN / Proteccion Civil son la
#     fuente oficial para confirmar cualquier evento.
# -------------------------------------------------------------------------

HAIL_QUERY = ('(granizo OR granizada OR pedrisco OR "tormenta de granizo" OR '
              '"lluvia de granizo" OR hailstorm OR "hail storm")')
TORNADO_QUERY = '(tornado OR "tromba marina" OR waterspout OR torbellino)'

SEVERE_SIZE_KEYWORDS = [
    "tamano de", "pelota de golf", "pelota de tenis", "bola de billar",
    "del tamano", "huevo", "gran tamano", "granizo grande", "grande como",
    "destrozo", "danos por granizo", "fuerte granizada", "intensa granizada",
    "granizo de gran tamano", "cm de diametro", "centimetro", "milimetro",
    "alerta roja", "arboles caidos", "arboles derribados", "historica granizada",
    "granizada historica", "fuerte tormenta con granizo", "impactante granizada",
    "diametro",
]


def _fetch_gdelt_geo(query, layer):
    feats = []
    try:
        q = urllib.parse.quote(query)
        url = f"https://api.gdeltproject.org/api/v2/geo/geo?query={q}&format=GeoJSON&mode=PointData&timespan=1440"
        gj = json.loads(http_get(url))
        for f in gj.get("features", []):
            g = f.get("geometry") or {}
            c = g.get("coordinates")
            if not c or len(c) < 2:
                continue
            p = f.get("properties", {}) or {}
            cnt = int(p.get("count") or 1)
            name = p.get("name") or p.get("location") or ""
            feats.append((cnt, feature(c[0], c[1], {"layer": layer, "name": name, "count": cnt})))
        print(f"  [{layer}] puntos: {len(gj.get('features', []))}")
    except Exception as e:
        print(f"  [{layer}] geo fallo: {e}")
    return feats


def fetch_severe_weather():
    # 1) Puntos globales aproximados (GDELT GEO) - granizo + tornado en un solo mapa
    pts = []
    pts += _fetch_gdelt_geo(HAIL_QUERY, "hail")
    pts += _fetch_gdelt_geo(TORNADO_QUERY, "tornado")
    pts.sort(key=lambda t: t[0], reverse=True)
    feats = [f for _, f in pts[:300]]
    write_geojson("severe_weather.geojson", feats, preserve_if_empty=True)

    # 2) Titulares MX (Google News) con geocodificacion a nivel estado + bandera de severidad
    map_feats = []
    for kind, gnews_q in (
        ("GRANIZO", "granizo OR granizada OR pedrisco"),
        ("TORNADO", 'tornado OR "tromba marina"'),
    ):
        items = []
        try:
            gq = urllib.parse.quote(gnews_q)
            rss = http_get(f"https://news.google.com/rss/search?q={gq}&hl=es-419&gl=MX&ceid=MX:es")
            root = ET.fromstring(rss)
            for it in root.iter("item"):
                src_el = it.find("source")
                items.append({
                    "title": it.findtext("title") or "",
                    "url": it.findtext("link") or "",
                    "date": it.findtext("pubDate") or "",
                    "source": (src_el.text if src_el is not None else "") or "Google News",
                })
            print(f"  [severo] google news {kind}: {len(items)}")
        except Exception as e:
            print(f"  [severo] google news {kind} fallo: {e}")
        for it in [x for x in items if _is_recent(x.get("date"))]:
            title = it.get("title") or ""
            state = _detect_mx_state(title)
            if not state:
                continue
            base_lat, base_lon = MX_STATE_CENTROIDS[state]
            lat, lon = _jitter(base_lat, base_lon, title)
            nt = _normalize_text(title)
            severe = any(kw in nt for kw in SEVERE_SIZE_KEYWORDS)
            map_feats.append(feature(lon, lat, {
                "title": title, "url": it.get("url") or "", "date": it.get("date") or "",
                "source": it.get("source") or "", "state": state, "kind": kind,
                "severe": severe,
            }))
    print(f"  [severo] mapa MX: {len(map_feats)} ubicados")
    write_geojson("severe_weather_map.geojson", map_feats, {
        "note": "Ubicacion aproximada a nivel estado, detectada por texto del titular. No es el punto exacto del evento. 'severe' es una deteccion heuristica de palabras clave (tamano/danos), no una medicion oficial.",
    }, preserve_if_empty=True)
    return len(feats) + len(map_feats)




# -------------------------------------------------------------------------
# 11) MOVIMIENTOS DE MASA (deslaves, avalanchas, aludes, desbordamientos,
#     desprendimientos glaciares, mar de fondo)  - señales GDELT 24 h
#     NOTA: son menciones en cobertura noticiosa geolocalizada, NO eventos
#     confirmados ni mediciones oficiales. Verifica siempre en Proteccion Civil.
# -------------------------------------------------------------------------

MASS_QUERIES = [
    ("landslide",  '(deslave OR deslizamiento OR derrumbe OR landslide OR "deslizamiento de tierra" '
                   'OR "corrimiento de tierra" OR "alud terrestre" OR "desprendimiento de tierra")'),
    ("avalanche",  '(avalancha OR "avalancha de nieve" OR "alud de nieve" OR "alud nevado" '
                   'OR avalanche OR snowslide)'),
    ("mudslide",   '(lahar OR "avalancha de lodo" OR "flujo de lodo" OR "flujo de detritos" '
                   'OR mudslide OR "mud flow" OR "debris flow" OR lahars)'),
    ("rockfall",   '(derrumbe OR "caida de rocas" OR "desprendimiento de rocas" '
                   'OR rockfall OR rockslide OR "caida de piedras")'),
    ("flood",      '(inundacion OR "desbordamiento de rio" OR "rio desbordado" '
                   'OR "desbordamiento" OR flooding OR "flash flood" OR "inundacion repentina")'),
    ("glacier",    '(glaciar OR "desprendimiento glaciar" OR "colapso glaciar" OR "alud glaciar" '
                   'OR glacier OR "glacial lake" OR GLOF OR "glaciar nepal" OR "seracs")'),
    ("surge",      '("mar de fondo" OR marejada OR "oleaje extremo" OR "swell" '
                   'OR "marejada ciclonica" OR "marea de tormenta" OR "storm surge")'),
    ("lava",       '("flujo de lava" OR "colada de lava" OR "corriente de lava" '
                   'OR "lava flow" OR "efusion de lava" OR "emanacion de lava" '
                   'OR lahars OR "rio de lava")'),
    ("talud",      '("deslizamiento de talud" OR "inestabilidad de talud" OR "falla de talud" '
                   'OR "colapso de talud" OR "derrumbe de talud" OR "talud inestable" '
                   'OR "slope failure" OR "slope collapse")'),
]


def fetch_mass_movements():
    """Obtiene senales GDELT de movimientos de masa en las ultimas 24 h."""
    pts = []
    for layer, query in MASS_QUERIES:
        pts += _fetch_gdelt_geo(query, layer)
    pts.sort(key=lambda t: t[0], reverse=True)
    feats = [f for _, f in pts[:400]]
    print(f"  [mass] total puntos: {len(feats)}")
    return write_geojson("mass_movements.geojson", feats, {
        "note": ("Señales de cobertura noticiosa GDELT 24 h. Ubicacion aproximada. "
                 "No son eventos confirmados. Fuentes oficiales: Proteccion Civil, "
                 "CENAPRED, SMN, servicios geologicos nacionales."),
    }, preserve_if_empty=True)

# -------------------------------------------------------------------------
# Orquestacion
# -------------------------------------------------------------------------

def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    print(f"== CLIMA TACTICO :: actualizacion {now_iso()} ==")
    manifest = {"generated": now_iso(), "sources": {}}

    jobs = [
        ("fires", fetch_fires),
        ("storms", fetch_storms),
        ("storm_tracks", fetch_storm_tracks),
        ("gdacs", fetch_gdacs),
        ("forecast", fetch_forecast),
        ("airquality", fetch_airquality),
        ("space", fetch_space),
        ("security", fetch_security),
        ("severe_weather", fetch_severe_weather),
        ("mass_movements", fetch_mass_movements),
        ("advisories", fetch_advisories),
        ("volcanoes", fetch_volcanoes),
    ]
    for key, fn in jobs:
        t0 = time.time()
        try:
            count = fn()
        except Exception as e:
            print(f"  [{key}] ERROR no controlado: {e}")
            count = 0
        manifest["sources"][key] = {"count": count, "updated": now_iso(), "seconds": round(time.time() - t0, 1)}

    with open(os.path.join(DATA_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("== manifest.json escrito ==")
    print(json.dumps(manifest["sources"], indent=2, ensure_ascii=False))


# -------------------------------------------------------------------------
# 10) DESTINOS ADICIONALES - ampliacion de cobertura (Mexico, EE.UU., Europa,
#     Asia, Oriente Medio y Norte de Africa). Coordenadas aproximadas de
#     referencia; validar antes de uso productivo.
# -------------------------------------------------------------------------

destinos_adicionales = [
    # --- Mexico ---
    ("Aguascalientes", 21.88, -102.29), ("Durango", 24.03, -104.65),
    ("Saltillo", 25.42, -101.00), ("San Luis Potosi", 22.16, -100.99),
    ("Leon Guanajuato", 21.12, -101.68), ("Irapuato", 20.67, -101.35),
    ("Celaya", 20.52, -100.81), ("Tlaxcala", 19.31, -98.24),
    ("Cuernavaca", 18.92, -99.23), ("Toluca", 19.28, -99.66),
    ("Xalapa", 19.54, -96.91), ("Orizaba", 18.85, -97.10),
    ("Cordoba Veracruz", 18.89, -96.93), ("Boca del Rio", 19.11, -96.10),
    ("Coatepec Veracruz", 19.45, -96.96), ("Papantla", 20.45, -97.32),
    ("Poza Rica", 20.53, -97.46), ("Comitan de Dominguez", 16.25, -92.13),
    ("Chiapa de Corzo", 16.71, -93.02), ("Tapachula", 14.91, -92.26),
    ("Puerto Escondido", 15.86, -97.07), ("Mazunte", 15.67, -96.55),
    ("Zipolite", 15.66, -96.52), ("Mitla", 16.92, -96.36),
    ("Tehuacan", 18.46, -97.39), ("Atlixco", 18.91, -98.43),
    ("Cholula", 19.06, -98.31), ("Bernal Queretaro", 20.74, -99.94),
    ("Tequisquiapan", 20.52, -99.89), ("Dolores Hidalgo", 21.16, -100.93),
    ("Patzcuaro", 19.51, -101.61), ("Uruapan", 19.42, -102.06),
    ("Tzintzuntzan", 19.63, -101.58), ("Janitzio", 19.57, -101.65),
    ("Manzanillo", 19.05, -104.32), ("Colima", 19.24, -103.72),
    ("Comala", 19.32, -103.76), ("Tepic", 21.51, -104.89),
    ("San Blas Nayarit", 21.54, -105.28), ("Guaymas", 27.92, -110.90),
    ("San Carlos Sonora", 27.96, -111.03), ("Puerto Penasco", 31.32, -113.53),
    ("Alamos Sonora", 27.02, -108.94), ("Creel", 27.75, -107.63),
    ("Barrancas del Cobre", 27.52, -107.76), ("Parras de la Fuente", 25.44, -102.18),
    ("Cuatro Cienegas", 26.99, -102.07), ("Real de Catorce", 23.69, -100.89),
    ("Xilitla", 21.39, -98.99), ("Ciudad Valles", 21.98, -99.02),
    ("El Tajin", 20.45, -97.38), ("Chichen Itza", 20.68, -88.57),
    ("Uxmal", 20.36, -89.77), ("Calakmul", 18.10, -89.81),
    ("Edzna", 19.60, -90.23), ("Monte Alban", 17.04, -96.77),
    ("Hierve el Agua", 16.87, -96.28), ("Canon del Sumidero", 16.82, -93.09),
    ("Cascadas de Agua Azul", 17.26, -92.12), ("Reserva Mariposa Monarca", 19.57, -100.26),
    ("Nevado de Toluca", 19.11, -99.76), ("Grutas de Cacahuamilpa", 18.68, -99.51),
    ("Las Coloradas Yucatan", 21.61, -87.99), ("Celestun", 20.86, -90.40),
    ("Progreso Yucatan", 21.28, -89.66), ("Izamal", 20.93, -89.02),
    ("Valladolid Yucatan", 20.69, -88.20), ("Rio Lagartos", 21.60, -88.16),
    ("Ek Balam", 20.89, -88.14),
    # --- Estados Unidos ---
    ("Washington DC", 38.91, -77.04), ("Boston", 42.36, -71.06),
    ("Philadelphia", 39.95, -75.17), ("Chicago", 41.88, -87.63),
    ("San Francisco", 37.77, -122.42), ("San Diego", 32.72, -117.16),
    ("Las Vegas", 36.17, -115.14), ("Orlando", 28.54, -81.38),
    ("Seattle", 47.61, -122.33), ("Portland Oregon", 45.52, -122.68),
    ("Denver", 39.74, -104.99), ("Phoenix", 33.45, -112.07),
    ("Austin", 30.27, -97.74), ("San Antonio", 29.42, -98.49),
    ("Dallas", 32.78, -96.80), ("Nashville", 36.16, -86.78),
    ("Memphis", 35.15, -90.05), ("Savannah", 32.08, -81.09),
    ("Charleston South Carolina", 32.78, -79.93), ("Atlanta", 33.75, -84.39),
    ("Honolulu", 21.31, -157.86), ("Anchorage", 61.22, -149.90),
    ("Salt Lake City", 40.76, -111.89), ("Santa Fe", 35.69, -105.94),
    ("Albuquerque", 35.08, -106.65), ("Sedona", 34.87, -111.76),
    ("Flagstaff", 35.20, -111.65), ("Palm Springs", 33.83, -116.55),
    ("Monterey California", 36.60, -121.89), ("Santa Barbara", 34.42, -119.70),
    ("Napa", 38.30, -122.29), ("Sonoma", 38.29, -122.46),
    ("Key West", 24.56, -81.78), ("Naples Florida", 26.14, -81.79),
    ("Tampa", 27.95, -82.46), ("Fort Lauderdale", 26.12, -80.14),
    ("St Augustine", 29.90, -81.31), ("Asheville", 35.60, -82.55),
    ("Jackson Hole", 43.48, -110.76), ("Moab", 38.57, -109.55),
    ("Bar Harbor", 44.39, -68.20), ("Newport Rhode Island", 41.49, -71.31),
    ("Salem Massachusetts", 42.52, -70.90), ("Cape Cod", 41.67, -70.30),
    ("Marthas Vineyard", 41.39, -70.62), ("Yellowstone National Park", 44.43, -110.59),
    ("Yosemite Valley", 37.75, -119.59), ("Grand Canyon South Rim", 36.06, -112.14),
    ("Zion National Park", 37.30, -113.03), ("Bryce Canyon National Park", 37.63, -112.17),
    ("Arches National Park", 38.73, -109.59), ("Monument Valley", 36.99, -110.10),
    ("Antelope Canyon", 36.86, -111.37), ("Death Valley National Park", 36.53, -116.93),
    ("Joshua Tree National Park", 33.87, -115.90), ("Great Smoky Mountains", 35.61, -83.51),
    ("Niagara Falls USA", 43.08, -79.07), ("Lake Tahoe", 39.10, -120.03),
    ("Mount Rushmore", 43.88, -103.46), ("Glacier National Park", 48.76, -113.79),
    ("Rocky Mountain National Park", 40.34, -105.68), ("Everglades National Park", 25.29, -80.90),
    ("Hawaii Volcanoes National Park", 19.42, -155.29), ("Denali National Park", 63.11, -151.19),
    ("Acadia National Park", 44.35, -68.21), ("Olympic National Park", 47.80, -123.60),
    ("Grand Teton National Park", 43.79, -110.68), ("Big Sur", 36.27, -121.81),
    ("Santa Monica", 34.02, -118.49), ("Anaheim", 33.84, -117.91),
    # --- Europa ---
    ("Barcelona", 41.39, 2.17), ("Sevilla", 37.39, -5.99),
    ("Granada", 37.18, -3.60), ("Cordoba Espana", 37.89, -4.78),
    ("Valencia", 39.47, -0.38), ("Bilbao", 43.26, -2.93),
    ("San Sebastian", 43.32, -1.98), ("Malaga", 36.72, -4.42),
    ("Toledo", 39.86, -4.03), ("Salamanca", 40.97, -5.66),
    ("Santiago de Compostela", 42.88, -8.54), ("Palma de Mallorca", 39.57, 2.65),
    ("Ibiza", 38.91, 1.43), ("Oporto", 41.15, -8.61),
    ("Sintra", 38.80, -9.38), ("Coimbra", 40.21, -8.43),
    ("Faro", 37.02, -7.93), ("Funchal", 32.65, -16.91),
    ("Ponta Delgada", 37.74, -25.67), ("Lyon", 45.76, 4.84),
    ("Niza", 43.71, 7.26), ("Marsella", 43.30, 5.37),
    ("Burdeos", 44.84, -0.58), ("Estrasburgo", 48.58, 7.75),
    ("Toulouse", 43.60, 1.44), ("Avinon", 43.95, 4.81),
    ("Cannes", 43.55, 7.02), ("Mont Saint Michel", 48.64, -1.51),
    ("Chamonix", 45.92, 6.87), ("Annecy", 45.90, 6.13),
    ("Milan", 45.46, 9.19), ("Florencia", 43.77, 11.26),
    ("Venecia", 45.44, 12.32), ("Napoles", 40.85, 14.27),
    ("Bolonia", 44.49, 11.34), ("Turin", 45.07, 7.69),
    ("Palermo", 38.12, 13.36), ("Catania", 37.51, 15.09),
    ("Verona", 45.44, 10.99), ("Siena", 43.32, 11.33),
    ("Pisa", 43.72, 10.40), ("Amalfi", 40.63, 14.60),
    ("Sorrento", 40.63, 14.38), ("Matera", 40.67, 16.60),
    ("Cinque Terre", 44.15, 9.65), ("Edimburgo", 55.95, -3.19),
    ("Glasgow", 55.86, -4.25), ("Manchester", 53.48, -2.24),
    ("Liverpool", 53.41, -2.99), ("Bath", 51.38, -2.36),
    ("Oxford", 51.75, -1.26), ("Cambridge", 52.21, 0.12),
    ("York", 53.96, -1.08), ("Belfast", 54.60, -5.93),
    ("Cardiff", 51.48, -3.18), ("Inverness", 57.48, -4.22),
    ("Isla de Skye", 57.27, -6.22), ("Munich", 48.14, 11.58),
    ("Hamburgo", 53.55, 9.99), ("Colonia", 50.94, 6.96),
    ("Frankfurt", 50.11, 8.68), ("Dresde", 51.05, 13.74),
    ("Heidelberg", 49.40, 8.69), ("Nuremberg", 49.45, 11.08),
    ("Rothenburg ob der Tauber", 49.38, 10.18), ("Fussen", 47.57, 10.70),
    ("Castillo de Neuschwanstein", 47.56, 10.75), ("Rotterdam", 51.92, 4.48),
    ("La Haya", 52.07, 4.30), ("Utrecht", 52.09, 5.12),
    ("Maastricht", 50.85, 5.69), ("Giethoorn", 52.74, 6.08),
    ("Brujas", 51.21, 3.22), ("Gante", 51.05, 3.72),
    ("Amberes", 51.22, 4.40), ("Lovaina", 50.88, 4.70),
    ("Salzburgo", 47.81, 13.05), ("Innsbruck", 47.27, 11.39),
    ("Hallstatt", 47.56, 13.65), ("Graz", 47.07, 15.44),
    ("Ginebra", 46.20, 6.15), ("Lucerna", 47.05, 8.31),
    ("Berna", 46.95, 7.45), ("Interlaken", 46.69, 7.86),
    ("Zermatt", 46.02, 7.75), ("Lausana", 46.52, 6.63),
    ("St Moritz", 46.50, 9.84), ("Salonika", 40.64, 22.94),
    ("Santorini", 36.39, 25.46), ("Mykonos", 37.45, 25.33),
    ("Rodas", 36.43, 28.22), ("Corfu", 39.62, 19.92),
    ("Meteora", 39.72, 21.63), ("Delfos", 38.48, 22.50),
    ("Heraklion", 35.34, 25.13), ("Cork", 51.90, -8.47),
    ("Galway", 53.27, -9.05), ("Killarney", 52.06, -9.51),
    ("Kilkenny", 52.65, -7.25), ("Acantilados de Moher", 52.97, -9.43),
    ("Reikiavik", 64.15, -21.94), ("Akureyri", 65.68, -18.09),
    ("Gotemburgo", 57.71, 11.97), ("Malmo", 55.61, 13.00),
    ("Bergen", 60.39, 5.32), ("Tromso", 69.65, 18.96),
    ("Stavanger", 58.97, 5.73), ("Trondheim", 63.43, 10.39),
    ("Rovaniemi", 66.50, 25.73), ("Turku", 60.45, 22.27),
    ("Tallin", 59.44, 24.75), ("Riga", 56.95, 24.11),
    ("Vilna", 54.69, 25.28), ("Cracovia", 50.06, 19.94),
    ("Gdansk", 54.35, 18.65), ("Breslavia", 51.11, 17.03),
    ("Poznan", 52.41, 16.93), ("Zakopane", 49.30, 19.95),
    ("Cesky Krumlov", 48.81, 14.32), ("Karlovy Vary", 50.23, 12.87),
    ("Bratislava", 48.15, 17.11), ("Kosice", 48.72, 21.26),
    ("Liubliana", 46.06, 14.51), ("Lago Bled", 46.37, 14.09),
    ("Piran", 45.53, 13.57), ("Zagreb", 45.81, 15.98),
    ("Dubrovnik", 42.65, 18.09), ("Split", 43.51, 16.44),
    ("Zadar", 44.12, 15.23), ("Lagos de Plitvice", 44.88, 15.62),
    ("Sarajevo", 43.86, 18.41), ("Mostar", 43.34, 17.81),
    ("Belgrado", 44.82, 20.46), ("Novi Sad", 45.26, 19.83),
    ("Kotor", 42.42, 18.77), ("Budva", 42.29, 18.84),
    ("Tirana", 41.33, 19.82), ("Berat", 40.71, 19.95),
    ("Skopie", 42.00, 21.43), ("Ohrid", 41.12, 20.80),
    ("Sofia", 42.70, 23.32), ("Plovdiv", 42.14, 24.75),
    ("Veliko Tarnovo", 43.08, 25.63), ("Bucarest", 44.43, 26.10),
    ("Brasov", 45.66, 25.61), ("Sibiu", 45.80, 24.15),
    ("Sighisoara", 46.22, 24.79), ("Chisinau", 47.01, 28.86),
    ("Nicosia", 35.19, 33.38), ("Pafos", 34.78, 32.42),
    ("Limassol", 34.68, 33.04), ("La Valeta", 35.90, 14.51),
    ("Mdina", 35.89, 14.40), ("Luxemburgo", 49.61, 6.13),
    ("Monaco", 43.74, 7.42), ("Andorra la Vella", 42.51, 1.52),
    ("San Marino", 43.94, 12.45), ("Vaduz", 47.14, 9.52),
    # --- Asia ---
    ("Kioto", 35.01, 135.77), ("Osaka", 34.69, 135.50),
    ("Nara", 34.69, 135.80), ("Hiroshima", 34.39, 132.46),
    ("Sapporo", 43.06, 141.35), ("Fukuoka", 33.59, 130.40),
    ("Nagoya", 35.18, 136.91), ("Kanazawa", 36.56, 136.66),
    ("Takayama", 36.14, 137.25), ("Nikko", 36.75, 139.60),
    ("Hakone", 35.23, 139.11), ("Naha Okinawa", 26.21, 127.68),
    ("Xian", 34.34, 108.94), ("Chengdu", 30.57, 104.07),
    ("Guangzhou", 23.13, 113.26), ("Shenzhen", 22.54, 114.06),
    ("Hangzhou", 30.27, 120.15), ("Suzhou", 31.30, 120.58),
    ("Guilin", 25.27, 110.29), ("Lijiang", 26.87, 100.23),
    ("Kunming", 25.04, 102.71), ("Chongqing", 29.56, 106.55),
    ("Zhangjiajie", 29.12, 110.48), ("Harbin", 45.80, 126.54),
    ("Macao", 22.20, 113.54), ("Taipei", 25.03, 121.57),
    ("Kaohsiung", 22.63, 120.30), ("Tainan", 22.99, 120.20),
    ("Taichung", 24.15, 120.68), ("Hualien", 23.99, 121.61),
    ("Busan", 35.18, 129.08), ("Isla de Jeju", 33.50, 126.53),
    ("Gyeongju", 35.86, 129.22), ("Incheon", 37.46, 126.71),
    ("Agra", 27.18, 78.01), ("Jaipur", 26.91, 75.79),
    ("Varanasi", 25.32, 82.97), ("Udaipur", 24.59, 73.69),
    ("Jodhpur", 26.24, 73.02), ("Kochi", 9.93, 76.27),
    ("Goa", 15.30, 74.12), ("Chennai", 13.08, 80.27),
    ("Calcuta", 22.57, 88.36), ("Bangalore", 12.97, 77.59),
    ("Hyderabad India", 17.39, 78.49), ("Amritsar", 31.63, 74.87),
    ("Rishikesh", 30.09, 78.27), ("Chiang Mai", 18.79, 98.99),
    ("Chiang Rai", 19.91, 99.83), ("Phuket", 7.88, 98.39),
    ("Krabi", 8.09, 98.91), ("Ayutthaya", 14.35, 100.57),
    ("Koh Samui", 9.51, 100.01), ("Pattaya", 12.92, 100.88),
    ("Hoi An", 15.88, 108.33), ("Hue", 16.46, 107.59),
    ("Da Nang", 16.05, 108.20), ("Bahia de Ha Long", 20.91, 107.18),
    ("Ninh Binh", 20.25, 105.97), ("Sapa", 22.34, 103.84),
    ("Phu Quoc", 10.23, 103.96), ("Bali", -8.34, 115.09),
    ("Ubud", -8.51, 115.26), ("Yogyakarta", -7.80, 110.37),
    ("Bandung", -6.92, 107.62), ("Surabaya", -7.26, 112.75),
    ("Lombok", -8.65, 116.32), ("Labuan Bajo", -8.50, 119.88),
    ("Parque Nacional de Komodo", -8.55, 119.48), ("George Town Penang", 5.41, 100.34),
    ("Malaca", 2.19, 102.25), ("Langkawi", 6.35, 99.80),
    ("Kota Kinabalu", 5.98, 116.07), ("Cebu", 10.32, 123.89),
    ("Boracay", 11.97, 121.92), ("El Nido Palawan", 11.20, 119.41),
    ("Puerto Princesa", 9.74, 118.74), ("Bohol", 9.85, 124.14),
    ("Baguio", 16.40, 120.60), ("Siem Reap", 13.36, 103.86),
    ("Phnom Penh", 11.56, 104.93), ("Luang Prabang", 19.89, 102.14),
    ("Vientian", 17.98, 102.63), ("Yangon", 16.84, 96.17),
    ("Bagan", 21.17, 94.86), ("Mandalay", 21.96, 96.09),
    ("Katmandu", 27.72, 85.32), ("Pokhara", 28.21, 83.99),
    ("Thimphu", 27.47, 89.64), ("Paro", 27.43, 89.42),
    ("Colombo", 6.93, 79.86), ("Kandy", 7.29, 80.63),
    ("Galle", 6.03, 80.22), ("Sigiriya", 7.96, 80.76),
    ("Ella Sri Lanka", 6.87, 81.05), ("Almaty", 43.24, 76.95),
    ("Astana", 51.17, 71.45), ("Samarcanda", 39.65, 66.96),
    ("Bujara", 39.77, 64.42), ("Taskent", 41.30, 69.24),
    ("Jiva", 41.38, 60.36), ("Biskek", 42.87, 74.60),
    ("Osh", 40.53, 72.80), ("Dusambe", 38.56, 68.79),
    ("Asjabad", 37.96, 58.33), ("Ulan Bator", 47.92, 106.92),
    ("Tiflis", 41.72, 44.79), ("Batumi", 41.64, 41.64),
    ("Erevan", 40.18, 44.51), ("Baku", 40.41, 49.87),
    # --- Oriente Medio y Norte de Africa ---
    ("Sharjah", 25.35, 55.39), ("Al Ain", 24.21, 55.74),
    ("Ras Al Khaimah", 25.79, 55.94), ("Fujairah", 25.13, 56.33),
    ("Yeda", 21.54, 39.17), ("La Meca", 21.42, 39.83),
    ("Medina", 24.47, 39.61), ("AlUla", 26.61, 37.92),
    ("Abha", 18.22, 42.51), ("Taif", 21.27, 40.42),
    ("Diriyah", 24.74, 46.58), ("Al Wakrah", 25.17, 51.60),
    ("Al Khor", 25.68, 51.50), ("Al Zubarah", 25.98, 51.04),
    ("Manama", 26.23, 50.59), ("Muharraq", 26.26, 50.61),
    ("Petra", 30.33, 35.44), ("Wadi Rum", 29.58, 35.42),
    ("Aqaba", 29.53, 35.01), ("Jerash", 32.27, 35.89),
    ("Madaba", 31.72, 35.79), ("Mar Muerto Jordania", 31.56, 35.47),
    ("Haifa", 32.79, 34.99), ("Acre", 32.93, 35.08),
    ("Nazaret", 32.70, 35.30), ("Eilat", 29.56, 34.95),
    ("Masada", 31.32, 35.35), ("Cesarea", 32.50, 34.89),
    ("Mar de Galilea", 32.83, 35.58), ("Belen", 31.71, 35.20),
    ("Ramala", 31.90, 35.20), ("Jerico", 31.86, 35.46),
    ("Hebron", 31.53, 35.10), ("Biblos", 34.12, 35.65),
    ("Baalbek", 34.01, 36.21), ("Tripoli Libano", 34.44, 35.85),
    ("Sidon", 33.56, 35.37), ("Tiro", 33.27, 35.20),
    ("Bcharre", 34.25, 36.01), ("Nizwa", 22.93, 57.53),
    ("Salalah", 17.02, 54.09), ("Sur Oman", 22.57, 59.53),
    ("Khasab", 26.18, 56.25), ("Jebel Akhdar", 23.07, 57.67),
    ("Wahiba Sands", 22.43, 58.80), ("Erbil", 36.19, 44.01),
    ("Mosul", 36.35, 43.16), ("Najaf", 32.00, 44.33),
    ("Karbala", 32.62, 44.03), ("Basora", 30.51, 47.78),
    ("Babilonia", 32.54, 44.42), ("Isfahan", 32.65, 51.67),
    ("Shiraz", 29.59, 52.58), ("Yazd", 31.90, 54.37),
    ("Kashan", 33.99, 51.44), ("Tabriz", 38.08, 46.29),
    ("Mashhad", 36.30, 59.61), ("Qom", 34.64, 50.88),
    ("Persepolis", 29.94, 52.89), ("Kerman", 30.28, 57.08),
    ("Isla Qeshm", 26.96, 56.27), ("Isla Kish", 26.53, 53.98),
    ("Capadocia", 38.64, 34.83), ("Antalya", 36.90, 30.70),
    ("Esmirna", 38.42, 27.14), ("Efeso", 37.94, 27.34),
    ("Pamukkale", 37.92, 29.12), ("Ankara", 39.93, 32.86),
    ("Bodrum", 37.03, 27.43), ("Fethiye", 36.65, 29.12),
    ("Mardin", 37.31, 40.74), ("Gaziantep", 37.07, 37.38),
    ("Trabzon", 41.00, 39.73), ("Konya", 37.87, 32.49),
    ("Damasco", 33.51, 36.29), ("Alepo", 36.20, 37.16),
    ("Palmira", 34.56, 38.27), ("Latakia", 35.52, 35.79),
    ("Sanaa", 15.37, 44.19), ("Aden", 12.79, 45.03),
    ("Isla de Socotra", 12.46, 53.82), ("Shibam", 15.93, 48.63),
]

GRID.extend(destinos_adicionales)


if __name__ == "__main__":
    main()
