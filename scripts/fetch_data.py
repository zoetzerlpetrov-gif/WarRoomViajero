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
from datetime import datetime, timezone

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


def write_geojson(name, features, extra_meta=None):
    fc = {
        "type": "FeatureCollection",
        "metadata": {"generated": now_iso(), "count": len(features), **(extra_meta or {})},
        "features": features,
    }
    path = os.path.join(DATA_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False)
    return len(features)


def feature(lon, lat, props):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(float(lon), 4), round(float(lat), 4)]},
        "properties": props,
    }


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


# -------------------------------------------------------------------------
# 3) MULTI-AMENAZA GLOBAL  - GDACS (inundaciones, ciclones, sequia, etc.)
# -------------------------------------------------------------------------

GDACS_TYPE = {
    "EQ": "Sismo", "TC": "Ciclon tropical", "FL": "Inundacion",
    "VO": "Volcan", "DR": "Sequia", "WF": "Incendio forestal", "TS": "Tsunami",
}

def fetch_gdacs():
    feats = []
    try:
        raw = json.loads(http_get("https://www.gdacs.org/gdacsapi/api/events/geteventlist/MAP"))
        items = raw.get("features", raw if isinstance(raw, list) else [])
        for it in items:
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
    # Devuelve (nivel 0-3, etiqueta)
    lvl = 0
    if max_rain >= 50 or max_gust >= 90:
        lvl = 3
    elif max_rain >= 25 or max_gust >= 65:
        lvl = 2
    elif max_rain >= 10 or max_gust >= 45:
        lvl = 1
    return lvl, ["Tranquilo", "Vigilancia", "Riesgo", "Severo"][lvl]

def fetch_forecast():
    feats = []
    try:
        lats = ",".join(f"{p[1]}" for p in GRID)
        lons = ",".join(f"{p[2]}" for p in GRID)
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lats}&longitude={lons}"
            "&daily=precipitation_sum,precipitation_probability_max,"
            "wind_speed_10m_max,wind_gusts_10m_max,weather_code,temperature_2m_max"
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
            wcode = daily.get("weather_code", []) or []
            dates = daily.get("time", []) or []
            max_rain = max(rain) if rain else 0
            max_gust = max(gust) if gust else 0
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
                    "code": wcode[d] if d < len(wcode) else None,
                })
            feats.append(feature(lon, lat, {
                "layer": "forecast",
                "name": name,
                "level": lvl,
                "level_label": label,
                "max_rain_mm": round(max_rain, 1),
                "max_gust_kmh": round(max_gust, 1),
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
    ("Popocatepetl", 19.023, -98.622, "Mexico", "https://www.gob.mx/cenapred"),
    ("Colima (Volcan de Fuego)", 19.514, -103.617, "Mexico", "https://volcano.si.edu"),
    ("El Chichon", 17.360, -93.228, "Mexico", "https://volcano.si.edu"),
    ("Fuego", 14.473, -90.880, "Guatemala", "https://volcano.si.edu"),
    ("Pacaya", 14.382, -90.601, "Guatemala", "https://volcano.si.edu"),
    ("Kilauea", 19.421, -155.287, "EE.UU.", "https://volcanoes.usgs.gov"),
    ("Mauna Loa", 19.475, -155.608, "EE.UU.", "https://volcanoes.usgs.gov"),
    ("Etna", 37.748, 14.999, "Italia", "https://volcano.si.edu"),
    ("Stromboli", 38.789, 15.213, "Italia", "https://volcano.si.edu"),
    ("Sakurajima", 31.585, 130.657, "Japon", "https://volcano.si.edu"),
    ("Merapi", -7.540, 110.446, "Indonesia", "https://volcano.si.edu"),
    ("Reykjanes / Fagradalsfjall", 63.900, -22.270, "Islandia", "https://volcano.si.edu"),
    ("Villarrica", -39.420, -71.930, "Chile", "https://volcano.si.edu"),
    ("Cotopaxi", -0.677, -78.436, "Ecuador", "https://volcano.si.edu"),
    ("Nyiragongo", -1.520, 29.250, "RD Congo", "https://volcano.si.edu"),
]

def fetch_volcanoes():
    feats = []
    for name, lat, lon, country, url in VOLCANOES:
        feats.append(feature(lon, lat, {
            "layer": "volcano",
            "name": name,
            "country": country,
            "url": url,
            "note": "Nivel de actividad: consultar fuente oficial (CENAPRED/GVP).",
        }))
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
    write_geojson("security.geojson", feats)
    fetch_security_feed()
    return len(feats)


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
        json.dump({"generated": now_iso(), "items": items[:40]}, f, ensure_ascii=False)


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
        ("gdacs", fetch_gdacs),
        ("forecast", fetch_forecast),
        ("airquality", fetch_airquality),
        ("space", fetch_space),
        ("security", fetch_security),
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


if __name__ == "__main__":
    main()
