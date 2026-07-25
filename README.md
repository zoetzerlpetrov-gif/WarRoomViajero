# CLIMA TÁCTICO · Centro de Riesgos

Mapa interactivo de estética táctica que muestra **riesgos meteorológicos y geológicos**
en México y el mundo: sismos, ciclones/huracanes, incendios, inundaciones, volcanes,
alertas y **pronóstico a 7 días** (lluvia y viento). Se aloja **gratis en GitHub Pages**.

## Cómo funciona

Hay dos tipos de capas, por una razón de diseño:

| Tipo | Capas | Actualización | De dónde sale |
|------|-------|---------------|---------------|
| **En vivo** | Sismos, Alertas NWS (EE.UU.), Clima espacial* | Cada **60 s** / al cargar | USGS, weather.gov, NOAA SWPC |
| **Horneadas** | Ciclones, Incendios, Inundaciones (GDACS), Pronóstico 7d, Calidad del aire, **Seguridad (noticias)**, **Avisos de viaje**, Volcanes | **2×/día** (6am y 6pm CDMX) | NOAA NHC, NASA FIRMS, GDACS, Open-Meteo, GDELT, travel-advisory.info |

\* El clima espacial (llamaradas solares / tormentas geomagnéticas) se hornea 2×/día y además
intenta refrescarse en vivo desde la NOAA al abrir el mapa.

Además hay un módulo **Valle de México** (Hoy No Circula + calidad del aire), un **Feed de
seguridad** (titulares de bloqueos, asaltos, crimen organizado, etc. de Google News y GDELT) y
una **Consola de zona**: escribes una ciudad, estado o país y obtienes su **nivel de riesgo
oficial** (avisos de viaje de varios gobiernos), cuántas **señales de seguridad** hay cerca en
las noticias de las últimas 24 h, y enlaces para verificar en la fuente.

Los eventos urgentes (un sismo, un aviso de tsunami) no pueden esperar al horario de las
6 pm, así que el navegador los consulta directo y en tiempo real. El resto (pronóstico,
incendios, ciclones) se regenera dos veces al día con GitHub Actions y se guarda como
GeoJSON estático, lo que mantiene el sitio rápido y dentro de los límites gratuitos.

## Puesta en marcha (≈10 min)

1. **Crea un repositorio** en GitHub y sube estos archivos (puedes arrastrarlos en la web).
2. **Consigue una clave gratuita de NASA FIRMS** (para incendios):
   https://firms.modaps.eosdis.nasa.gov/api/map_key/ → te llega por correo al instante.
3. **Guárdala como secret:** repo → *Settings → Secrets and variables → Actions → New
   repository secret*. Nombre exacto: `FIRMS_MAP_KEY`.
4. **Permite que el bot escriba:** *Settings → Actions → General → Workflow permissions*
   → marca **Read and write permissions** → *Save*.
5. **Activa GitHub Pages:** *Settings → Pages → Source: Deploy from a branch* →
   rama `main`, carpeta `/ (root)` → *Save*.
6. **Primera carga de datos:** pestaña *Actions → "Actualizar datos de riesgo" → Run workflow*.
   En ~1 min commitea los datos reales.

Tu mapa quedará en: `https://TU-USUARIO.github.io/TU-REPO/`

> El sitio funciona desde el primer momento aunque no hayas corrido el workflow: los sismos
> en vivo cargan solos y las demás capas muestran "—" hasta la primera actualización.

## Horario automático

El workflow corre con cron en **UTC**. CDMX es UTC-6 todo el año, así que:
`0 0,12 * * *` → **06:00 y 18:00 hora de México**. Cámbialo en
`.github/workflows/update.yml` si quieres otro horario.

## Probar en tu computadora

**Leaflet ya viene incluido** en la carpeta `vendor/`, así que el mapa carga sin depender de
ningún CDN (por eso desaparece el error `L is not defined`). Lo que **sí** necesita internet son
los mosaicos del mapa y las fuentes de datos (sismos, noticias, etc.), y las capas locales
(`./data/*.geojson`) que los navegadores bloquean con `file://`. Por eso, para probar en tu
compu, no abras el archivo con doble clic: levanta un servidor local:

```bash
python3 -m http.server 8000
# abre http://localhost:8000
```

Para regenerar datos localmente:
```bash
export FIRMS_MAP_KEY=tu_clave
python3 scripts/fetch_data.py
```

## Personalizar

- **Ciudades del pronóstico:** edita la lista `GRID` en `scripts/fetch_data.py`.
- **Umbrales de riesgo** (cuándo algo es "severo"): función `risk_level` en el mismo archivo.
- **Volcanes vigilados:** lista `VOLCANOES`.
- **Colores / estética:** variables CSS `:root` al inicio de `index.html`.

## Fuentes y límites honestos

- **Sismos:** [USGS](https://earthquake.usgs.gov) (mundo, incluye México, tiempo real).
  El "aviso de tsunami" usa la bandera `tsunami` de USGS como indicador.
- **Ciclones/huracanes:** [NOAA NHC](https://www.nhc.noaa.gov) (Atlántico y Pacífico oriental).
- **Incendios:** [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov) (focos de calor VIIRS, 24 h).
- **Inundaciones/sequías/multiamenaza:** [GDACS](https://www.gdacs.org).
- **Pronóstico (lluvia, viento, nortes, tormentas):** [Open-Meteo](https://open-meteo.com) (sin API key).
- **Calidad del aire (US AQI, PM2.5, PM10, ozono…):** [Open-Meteo Air Quality](https://open-meteo.com/en/docs/air-quality-api) (sin API key).
- **Clima espacial (llamaradas R, radiación S, geomagnético G, Kp):** [NOAA SWPC](https://www.swpc.noaa.gov) (sin API key).
- **Seguridad (señales de noticias):** [GDELT](https://www.gdeltproject.org) (GEO + DOC, 100+ idiomas, sin key) y Google News RSS.
- **Avisos / nivel de riesgo por país:** [travel-advisory.info](https://www.travel-advisory.info) (varios gobiernos, sin key).
- **Volcanes:** lista curada con enlaces a CENAPRED y al Global Volcanism Program.

**Lo que NO se incluye y por qué:**
- **SASSLA** no expone una API pública: es una app ciudadana que retransmite la señal oficial
  del SASMEX a celulares. Para sismos se usa USGS (y opcionalmente el SSN de la UNAM, que
  requiere *scraping* porque tampoco tiene API limpia).
- **Bloqueos de carreteras e inseguridad:** ahora aparecen como **señales de noticias** en la
  capa de Seguridad y el Feed (GDELT + Google News), no como un registro oficial. Son un
  indicador para investigar, no una confirmación.
- **X/Twitter y TikTok:** no se pueden integrar de forma gratuita, estable ni conforme a sus
  términos. La API de X es de paga; TikTok no ofrece API pública para esto; y raspar ambas
  rompe sus reglas y deja de funcionar seguido. En su lugar, la capa de seguridad usa **GDELT**
  (que ya monitorea noticias y su amplificación en 100+ idiomas cada 15 min) más **Google News**.
- **Las señales de seguridad NO son incidentes confirmados.** Son menciones en cobertura
  noticiosa, geolocalizadas de forma aproximada. El mapa las etiqueta como *"señal, verifica"* y
  siempre enlaza a la fuente. La parte autoritativa (nivel de riesgo por país) viene de avisos
  de gobiernos. **No** hay una fuente abierta y verificada de "modus operandi de banda X" o de
  colusión policial; eso vive en rumores y no se presenta como hecho.
- **Contingencia ambiental / Doble No Circula:** la declaración oficial la hace la CAMe y no
  tiene API limpia; el mapa calcula el Hoy No Circula normal y usa la calidad del aire como
  señal de "verifica en la fuente oficial".

## Estructura

```
index.html                      la app (mapa + interfaz táctica)
vendor/leaflet.*                Leaflet incluido (sin CDN)
scripts/fetch_data.py           obtiene y normaliza los datos horneados
.github/workflows/update.yml    corre el script 2×/día y commitea /data
data/*.geojson                  datos generados (incluye airquality.geojson, security.geojson)
data/advisories.json            nivel de riesgo por país (avisos de viaje)
data/security_feed.json         titulares de seguridad (Google News + GDELT)
data/space.json                 estado del clima espacial (R/S/G, Kp, llamarada)
data/manifest.json              marca de tiempo y conteos por capa
```

## Aviso

Herramienta informativa de orientación general. **No sustituye** a Protección Civil,
al SASMEX ni a los avisos oficiales. Ante una emergencia, sigue siempre las fuentes oficiales.
