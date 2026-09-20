#!/usr/bin/env python3
"""
Génère data/<slug>.json pour chaque station de la liste STATIONS, à partir
du modèle ICON-CH1 (précis, ~33h) complété par ICON-CH2 (jusqu'à 120h) via
Open-Meteo / MétéoSuisse, avec correction nocturne du "trou à froid" apprise
automatiquement à partir des observations de chaque station.

Deux sources d'observation possibles par station (champ "source") :
  - "datacake"   : secrets DATACAKE_TOKEN_<SUFFIX>, DATACAKE_DEVICE_ID_<SUFFIX>,
                   DATACAKE_TEMP_FIELD_<SUFFIX> (un compte Datacake par station,
                   éventuellement différents comptes).
  - "infoclimat" : un seul secret partagé INFOCLIMAT_API_KEY pour toutes les
                   stations Infoclimat, chacune identifiée par son propre
                   "infoclimat_id" (ex: "STATIC0213", "000UF").

Toute la logique de correction (nébulosité effective, profil non-linéaire par
case horaire, apprentissage par moyenne mobile, lissage entre cases voisines)
est indépendante de la source d'observation - fetch_datacake_series et
fetch_infoclimat_series ont le même contrat de retour :
  - liste de (datetime, température) en cas de succès (peut être vide)
  - None en cas d'échec de la requête elle-même (jamais traité comme "nuit
    sans données exploitables", pour permettre un nouvel essai au prochain
    passage plutôt que d'abandonner définitivement).
"""
import json
import math
import os
import sys
from datetime import datetime, timedelta, date as date_cls
from zoneinfo import ZoneInfo

import urllib.request
import urllib.parse
import urllib.error

TZ = ZoneInfo("Europe/Paris")

# --- Registre des stations --------------------------------------------------
STATIONS = [
    {
        "slug": "greolieres",
        "name": "Gréolières-les-Neiges",
        "lat": 43.8318,
        "lon": 6.9617,
        "elevation_m": 1388,
        "source": "datacake",
        "datacake_url": "https://app.datacake.de/pd/8edbfefb-584e-4866-a684-0b84928b85c9",
        "env_suffix": "GREOLIERES",
    },
    {
        "slug": "chaud-clapier",
        "name": "Doline de Chaud Clapier",
        "lat": 44.915705,
        "lon": 5.335803,
        "elevation_m": 1378,
        "source": "datacake",
        "datacake_url": "https://app.datacake.de/pd/d4b23fdc-eca0-4658-9d0b-66829a0b9890",
        "env_suffix": "CHAUD_CLAPIER",
    },
    {
        "slug": "la-pesse",
        "name": "La Pesse - Le Cernétrou",
        "lat": 46.272,
        "lon": 5.834,
        "elevation_m": 1175,
        "source": "datacake",
        "datacake_url": "https://app.datacake.de/pd/96a819b1-c90c-4353-8a65-ba13f4fd0276",
        "env_suffix": "LA_PESSE",
    },
    {
        "slug": "tignes",
        "name": "Tignes - Pramécou",
        "lat": 45.439,
        "lon": 6.872,
        "elevation_m": 2684,
        "source": "datacake",
        "datacake_url": "https://app.datacake.de/pd/39371b61-daed-40b2-b329-d1e9db559c49",
        "env_suffix": "TIGNES",
    },
    {
        "slug": "beuil",
        "name": "Beuil - Cumba Clava",
        "lat": 44.090,
        "lon": 6.957,
        "elevation_m": 1545,
        "source": "datacake",
        "datacake_url": "https://app.datacake.de/pd/6152f64c-a2bc-4678-b6d0-77836ae26eb8",
        "env_suffix": "BEUIL",
    },
    {
        "slug": "darbounouse",
        "name": "La Chapelle-en-Vercors - Combe de Darbounouse",
        "lat": 44.97,
        "lon": 5.48,
        "elevation_m": 1282,
        "source": "infoclimat",
        "infoclimat_id": "STATIC0213",
        "infoclimat_url": "https://www.infoclimat.fr/observations-meteo/temps-reel/la-chapelle-en-vercors-combe-de-darbounouse/STATIC0213.html",
    },
    {
        "slug": "oscence",
        "name": "La Chapelle-en-Vercors - Combe-de-l'Oscence",
        "lat": 44.97,
        "lon": 5.38,
        "elevation_m": 975,
        "source": "infoclimat",
        "infoclimat_id": "000UF",
        "infoclimat_url": "https://www.infoclimat.fr/observations-meteo/temps-reel/la-chapelle-en-vercors-combe-de-l-oscence/000UF.html",
    },
    {
        "slug": "moulin-de-courbet",
        "name": "Lachapelle-Graillouse - Moulin de Courbet",
        "lat": 44.79,
        "lon": 4.00,
        "elevation_m": 1134,
        "source": "infoclimat",
        "infoclimat_id": "STATIC0407",
        "infoclimat_url": "https://www.infoclimat.fr/observations-meteo/temps-reel/lachapelle-graillouse-moulin-de-courbet/STATIC0407.html",
    },
    {
        "slug": "saint-christol",
        "name": "Saint-Christol",
        "lat": 44.03,
        "lon": 5.49,
        "elevation_m": 824,
        "source": "infoclimat",
        "infoclimat_id": "STATIC0257",
        "infoclimat_url": "https://www.infoclimat.fr/observations-meteo/temps-reel/saint-christol/STATIC0257.html",
    },
    {
        "slug": "solaison",
        "name": "Brizon - Doline de Solaison",
        "lat": 46.03,
        "lon": 6.42,
        "elevation_m": 1479,
        "source": "infoclimat",
        "infoclimat_id": "STATIC0305",
        "infoclimat_url": "https://www.infoclimat.fr/observations-meteo/temps-reel/brizon-doline-de-solaison/STATIC0305.html",
    },
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")

CLEAR_CLOUD_THRESHOLD = 20
CLOUD_ZERO_THRESHOLD = 45
CALM_WIND_THRESHOLD = 10
WIND_ZERO_THRESHOLD = 22
HIGH_CLOUD_ATTENUATION = 0.1
SUNSET_LEAD_MINUTES = 15
DEFAULT_ALPHA = 0.25
MAX_HISTORY = 90
MAX_BUCKET_HOURS = 20
LEARN_CLARITY_MIN = 0.6
FORECAST_NIGHTS = 4
CH1_FORECAST_DAYS = 2
CH2_FORECAST_DAYS = 5

INFOCLIMAT_API_KEY = os.environ.get("INFOCLIMAT_API_KEY", "").strip()


def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_openmeteo(lat, lon, elevation_m=None, past_days=2, forecast_days=3, model="meteoswiss_icon_ch1"):
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join([
            "temperature_2m", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
            "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
            "precipitation", "rain", "snowfall", "relative_humidity_2m",
        ]),
        "daily": "sunrise,sunset",
        "models": model,
        "timezone": "Europe/Berlin",
        "wind_speed_unit": "kmh",
        "forecast_days": forecast_days,
        "past_days": past_days,
    }
    if elevation_m is not None:
        params["elevation"] = elevation_m
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    return http_get_json(url)


def merge_ch1_ch2(om1, om2):
    h1, h2 = om1["hourly"], om2["hourly"]
    idx1 = {t: i for i, t in enumerate(h1["time"])}
    idx2 = {t: i for i, t in enumerate(h2["time"])}
    all_times = sorted(set(h1["time"]) | set(h2["time"]))

    var_names = [
        "temperature_2m", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
        "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
        "precipitation", "rain", "snowfall", "relative_humidity_2m",
    ]
    merged_hourly = {"time": all_times}
    for name in var_names:
        vals = []
        for t in all_times:
            v = None
            if t in idx1 and h1.get(name, [None])[idx1[t]] is not None:
                v = h1[name][idx1[t]]
            elif t in idx2 and h2.get(name, [None])[idx2[t]] is not None:
                v = h2[name][idx2[t]]
            vals.append(v)
        merged_hourly[name] = vals

    merged_daily = {"time": [], "sunrise": [], "sunset": []}
    seen_dates = {}
    for om in (om1, om2):
        for i, d in enumerate(om["daily"]["time"]):
            if d not in seen_dates:
                seen_dates[d] = (om["daily"]["sunrise"][i], om["daily"]["sunset"][i])
    for d in sorted(seen_dates):
        merged_daily["time"].append(d)
        merged_daily["sunrise"].append(seen_dates[d][0])
        merged_daily["sunset"].append(seen_dates[d][1])

    return {"hourly": merged_hourly, "daily": merged_daily}


def parse_iso_local(s):
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=TZ)


def effective_cloud(cloud_low, cloud_mid, cloud_high):
    low_mid = max(cloud_low or 0, cloud_mid or 0)
    return min(100, low_mid + HIGH_CLOUD_ATTENUATION * (cloud_high or 0))


def clarity_factor(eff_cloud, wind_speed):
    def ramp(value, full_at, zero_at):
        if value <= full_at:
            return 1.0
        if value >= zero_at:
            return 0.0
        return 1.0 - (value - full_at) / (zero_at - full_at)

    cloud_c = ramp(eff_cloud, CLEAR_CLOUD_THRESHOLD, CLOUD_ZERO_THRESHOLD)
    wind_c = ramp(wind_speed, CALM_WIND_THRESHOLD, WIND_ZERO_THRESHOLD)
    return cloud_c * wind_c


def pick_picto(cloud_total, cloud_low, cloud_mid, cloud_high, precipitation, rain, snowfall, humidity, wind_speed):
    if snowfall and snowfall > 0.05:
        return "neige"
    if rain and rain > 4:
        return "pluie-forte"
    if precipitation and precipitation > 0.1:
        return "pluie-faible"
    if humidity is not None and humidity > 95 and wind_speed < 5 and cloud_total > 80:
        return "brouillard"
    voile_gate = (cloud_low or 0) + 0.5 * (cloud_mid or 0)
    if voile_gate < 25 and cloud_high is not None and cloud_high >= 40:
        return "voile"
    if cloud_total <= 20:
        return "clair"
    if cloud_total <= 50:
        return "peu-nuageux"
    if cloud_total <= 80:
        return "tres-nuageux"
    return "couvert"


def default_offset(bucket):
    return -(2.0 + 8.0 * (1 - math.exp(-bucket / 2.5)))


def smooth_profile(profile, neighbor_weight=0.15):
    keys = sorted(profile, key=int)
    if len(keys) < 3:
        return profile
    vals = [profile[k] for k in keys]
    smoothed = vals[:]
    for i in range(1, len(vals) - 1):
        smoothed[i] = (
            (1 - 2 * neighbor_weight) * vals[i]
            + neighbor_weight * vals[i - 1]
            + neighbor_weight * vals[i + 1]
        )
    return {k: round(v, 2) for k, v in zip(keys, smoothed)}


def load_bias_state(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("offset_profile", {})
        state.setdefault("alpha", DEFAULT_ALPHA)
        state.setdefault("last_processed_night", None)
        state.setdefault("last_night_error_c", None)
        state.setdefault("last_night_samples", 0)
        state.setdefault("history", [])
        return state
    return {
        "offset_profile": {},
        "alpha": DEFAULT_ALPHA,
        "last_processed_night": None,
        "last_night_error_c": None,
        "last_night_samples": 0,
        "history": [],
    }


def save_bias_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_datacake_series(token, device_id, temp_field, start_dt, end_dt):
    """Retourne une liste de (datetime, temperature), [] si succès sans
    donnée, ou None si la requête elle-même a échoué."""
    if not (token and device_id):
        return None
    params = {
        "fields": temp_field,
        "resolution": "raw",
        "timeframe_start": start_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeframe_end": end_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    url = f"https://api.datacake.co/v1/devices/{device_id}/historic_data/?" + urllib.parse.urlencode(params)
    try:
        data = http_get_json(url, headers={"Authorization": f"Token {token}"})
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body = "(impossible de lire le corps de la réponse)"
        print(
            f"[warn] Datacake indisponible: HTTP {e.code} {e.reason} — "
            f"device_id_len={len(device_id)} field='{temp_field}' resolution='raw' "
            f"url={url} — réponse: {body}",
            file=sys.stderr,
        )
        return None
    except (urllib.error.URLError, ValueError) as e:
        print(f"[warn] Datacake indisponible: {e}", file=sys.stderr)
        return None
    if not data:
        print(
            "[warn] Datacake a répondu mais sans aucune donnée sur cette période.",
            file=sys.stderr,
        )
        return []
    out = []
    for row in data:
        try:
            t = datetime.fromisoformat(row["time"].replace("Z", "+00:00")).astimezone(TZ)
            v = row.get(temp_field)
            if v is not None:
                out.append((t, float(v)))
        except Exception:
            continue
    if not out:
        available = sorted(k for k in data[0].keys() if k != "time")
        print(
            f"[warn] Aucune valeur trouvée pour le champ '{temp_field}'. "
            f"Champs disponibles sur ce device : {available}",
            file=sys.stderr,
        )
    return out


def fetch_infoclimat_series(api_key, station_id, start_dt, end_dt):
    """Retourne une liste de (datetime, temperature), [] si succès sans
    donnée, ou None si la requête elle-même a échoué.

    Format d'appel d'après le code source du wrapper officiel InfoClimatAPI
    (pip install info-climat-api) :
      GET https://www.infoclimat.fr/opendata/?method=get&format=json
          &start=YYYY-MM-DD&end=YYYY-MM-DD&token=<clé>&stations[]=<id>
    La forme exacte de la réponse JSON n'a pas pu être vérifiée en amont
    (l'API n'était pas testable depuis l'environnement de préparation) -
    ce parseur essaie plusieurs formes plausibles et journalise un extrait
    de la réponse si aucune ne correspond, pour un diagnostic immédiat.
    """
    if not (api_key and station_id):
        return None
    params = {
        "method": "get",
        "format": "json",
        "start": start_dt.astimezone(TZ).date().isoformat(),
        "end": end_dt.astimezone(TZ).date().isoformat(),
        "token": api_key,
    }
    url = "https://www.infoclimat.fr/opendata/?" + urllib.parse.urlencode(params) + f"&stations[]={station_id}"
    headers = {
        # Certains serveurs (dont, semble-t-il, celui d'Infoclimat) renvoient
        # silencieusement une page générique au lieu de l'API si la requête
        # n'a pas d'en-tête User-Agent "de navigateur" - Python envoie sinon
        # "Python-urllib/3.x", souvent filtré comme trafic robot.
        "User-Agent": "Mozilla/5.0 (compatible; TrousAFroidBot/1.0; +https://github.com/)",
        "Accept": "application/json",
    }
    raw_body = None
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw_body = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw_body)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body = "(impossible de lire le corps de la réponse)"
        print(
            f"[warn] Infoclimat indisponible: HTTP {e.code} {e.reason} — "
            f"station={station_id} — réponse: {body}",
            file=sys.stderr,
        )
        return None
    except urllib.error.URLError as e:
        print(f"[warn] Infoclimat indisponible: {e} (station={station_id})", file=sys.stderr)
        return None
    except ValueError as e:
        preview = (raw_body or "")[:500]
        print(
            f"[warn] Infoclimat: réponse non-JSON pour {station_id} ({e}). "
            f"Aperçu brut (500 premiers caractères) : {preview!r}",
            file=sys.stderr,
        )
        return None

    out = []
    try:
        # Forme plausible 1 (la plus probable d'après la doc GraphQL Infoclimat) :
        # {"stations": {"<id>": {"hourly"|"data": [ {"timestamp"|"dh_utc"|"date": ..., "temperature"|"temp": ...}, ... ]}}}
        candidates = None
        if isinstance(data, dict) and "stations" in data:
            st = data["stations"].get(station_id) or next(iter(data["stations"].values()), None)
            if isinstance(st, dict):
                candidates = st.get("hourly") or st.get("data") or st.get("observations")
        elif isinstance(data, dict) and station_id in data:
            st = data[station_id]
            candidates = st.get("hourly") or st.get("data") if isinstance(st, dict) else st
        elif isinstance(data, list):
            candidates = data

        if isinstance(candidates, list):
            for row in candidates:
                if not isinstance(row, dict):
                    continue
                t_raw = row.get("dh_utc") or row.get("timestamp") or row.get("date") or row.get("time")
                v = row.get("temperature") or row.get("temp") or row.get("temperature_sol")
                if t_raw is None or v is None:
                    continue
                t_str = str(t_raw).replace("Z", "+00:00").replace(" ", "T")
                try:
                    t = datetime.fromisoformat(t_str)
                except ValueError:
                    continue
                if t.tzinfo is None:
                    t = t.replace(tzinfo=ZoneInfo("UTC"))
                out.append((t.astimezone(TZ), float(v)))
    except Exception as e:
        print(f"[warn] Infoclimat: erreur en analysant la réponse ({e}) — station={station_id}", file=sys.stderr)

    if not out:
        preview = json.dumps(data, ensure_ascii=False)[:800]
        print(
            f"[warn] Infoclimat: aucune donnée exploitable extraite pour {station_id}. "
            f"Aperçu brut de la réponse pour diagnostic : {preview}",
            file=sys.stderr,
        )
        return []
    return out


def nearest_value(series, target_dt, max_gap_minutes=40):
    best, best_gap = None, None
    for t, v in series:
        gap = abs((t - target_dt).total_seconds()) / 60
        if best_gap is None or gap < best_gap:
            best_gap, best = gap, v
    if best is not None and best_gap is not None and best_gap <= max_gap_minutes:
        return best
    return None


def compute_window(now, nights=1):
    window_start = now.replace(hour=18, minute=0, second=0, microsecond=0)
    first_night_end = window_start + timedelta(hours=23)
    if now > first_night_end:
        window_start += timedelta(days=1)
    window_end = window_start + timedelta(hours=23 + (nights - 1) * 24)
    return window_start, window_end


def corr_window_for_evening(evening_date, sunset_by_date, sunrise_by_date):
    sunset_dt = sunset_by_date.get(evening_date)
    if sunset_dt is None:
        return None, None
    start = (sunset_dt - timedelta(minutes=SUNSET_LEAD_MINUTES)).replace(minute=0, second=0, microsecond=0)
    next_day = evening_date + timedelta(days=1)
    sunrise_dt = sunrise_by_date.get(next_day)
    end = (sunrise_dt + timedelta(hours=1)) if sunrise_dt else None
    return start, end


def fetch_station_obs(station, start_dt, end_dt):
    """Point d'entrée unique vers la bonne source d'observation selon
    station["source"]. Retourne toujours le même contrat que
    fetch_datacake_series (liste, [] ou None)."""
    if station["source"] == "datacake":
        token = os.environ.get(f"DATACAKE_TOKEN_{station['env_suffix']}", "").strip()
        device_id = os.environ.get(f"DATACAKE_DEVICE_ID_{station['env_suffix']}", "").strip()
        temp_field = os.environ.get(f"DATACAKE_TEMP_FIELD_{station['env_suffix']}", "TEMPERATURE").strip()
        return fetch_datacake_series(token, device_id, temp_field, start_dt, end_dt)
    elif station["source"] == "infoclimat":
        return fetch_infoclimat_series(INFOCLIMAT_API_KEY, station["infoclimat_id"], start_dt, end_dt)
    else:
        print(f"[error] [{station['slug']}] source inconnue: {station['source']}", file=sys.stderr)
        return None


def station_has_credentials(station):
    if station["source"] == "datacake":
        return bool(
            os.environ.get(f"DATACAKE_TOKEN_{station['env_suffix']}", "").strip()
            and os.environ.get(f"DATACAKE_DEVICE_ID_{station['env_suffix']}", "").strip()
        )
    elif station["source"] == "infoclimat":
        return bool(INFOCLIMAT_API_KEY and station.get("infoclimat_id"))
    return False


def process_station(station, now):
    slug = station["slug"]

    if not station_has_credentials(station):
        if station["source"] == "datacake":
            print(
                f"[warn] [{slug}] DATACAKE_TOKEN_{station['env_suffix']} et/ou "
                f"DATACAKE_DEVICE_ID_{station['env_suffix']} absents ou vides : "
                "correction appliquée avec le profil déjà appris, mais aucun "
                "apprentissage n'aura lieu ce passage-ci.",
                file=sys.stderr,
            )
        else:
            print(
                f"[warn] [{slug}] INFOCLIMAT_API_KEY absent ou vide : correction "
                "appliquée avec le profil déjà appris, mais aucun apprentissage "
                "n'aura lieu ce passage-ci.",
                file=sys.stderr,
            )

    forecast_path = os.path.join(DATA_DIR, f"{slug}.json")
    bias_path = os.path.join(DATA_DIR, f"{slug}_bias_state.json")

    bias = load_bias_state(bias_path)
    profile = bias["offset_profile"]
    alpha = bias.get("alpha", DEFAULT_ALPHA)

    om1 = fetch_openmeteo(station["lat"], station["lon"], station.get("elevation_m"),
                           past_days=2, forecast_days=CH1_FORECAST_DAYS, model="meteoswiss_icon_ch1")
    om2 = fetch_openmeteo(station["lat"], station["lon"], station.get("elevation_m"),
                           past_days=0, forecast_days=CH2_FORECAST_DAYS, model="meteoswiss_icon_ch2")
    om = merge_ch1_ch2(om1, om2)
    hourly = om["hourly"]
    times = [parse_iso_local(t) for t in hourly["time"]]

    def series(name):
        return hourly.get(name, [None] * len(times))

    temp = series("temperature_2m")
    wind_speed = series("wind_speed_10m")
    wind_gusts = series("wind_gusts_10m")
    wind_dir = series("wind_direction_10m")
    cloud_total = series("cloud_cover")
    cloud_low = series("cloud_cover_low")
    cloud_mid = series("cloud_cover_mid")
    cloud_high = series("cloud_cover_high")
    precipitation = series("precipitation")
    rain = series("rain")
    snowfall = series("snowfall")
    humidity = series("relative_humidity_2m")

    idx_by_time = {t: i for i, t in enumerate(times)}

    daily_dates = [datetime.fromisoformat(d).date() for d in om["daily"]["time"]]
    sunrise_by_date, sunset_by_date = {}, {}
    for i, d in enumerate(daily_dates):
        sunrise_by_date[d] = parse_iso_local(om["daily"]["sunrise"][i])
        sunset_by_date[d] = parse_iso_local(om["daily"]["sunset"][i])

    window_start, window_end = compute_window(now, nights=FORECAST_NIGHTS)

    night_windows = []
    for n in range(FORECAST_NIGHTS):
        evening_date = window_start.date() + timedelta(days=n)
        c_start, c_end = corr_window_for_evening(evening_date, sunset_by_date, sunrise_by_date)
        if c_start is not None and c_end is not None:
            night_windows.append((c_start, c_end))

    def offset_for_bucket(bucket):
        key = str(bucket)
        return profile[key] if key in profile else default_offset(bucket)

    def find_night_window(t):
        for c_start, c_end in night_windows:
            if c_start <= t <= c_end:
                return c_start, c_end
        return None, None

    current_offset_c = None
    hours_out = []
    t = window_start
    while t <= window_end:
        if t not in idx_by_time:
            t += timedelta(hours=1)
            continue
        i = idx_by_time[t]
        raw_t = temp[i]
        ws = wind_speed[i] or 0
        wg = wind_gusts[i] or 0
        wd = wind_dir[i] or 0
        ct = cloud_total[i] if cloud_total[i] is not None else 100
        cl, cm, ch = cloud_low[i], cloud_mid[i], cloud_high[i]
        pr, rn, sn = precipitation[i] or 0, rain[i] or 0, snowfall[i] or 0
        hu = humidity[i]

        eff_cloud = effective_cloud(cl, cm, ch)
        corr_start, corr_end = find_night_window(t)
        in_corr_window = corr_start is not None
        clarity = clarity_factor(eff_cloud, ws) if in_corr_window else 0.0
        apply_corr = in_corr_window and clarity > 0

        applied_offset = None
        if apply_corr:
            bucket = min(int((t - corr_start).total_seconds() // 3600), MAX_BUCKET_HOURS)
            applied_offset = offset_for_bucket(bucket) * clarity
            corrected_t = raw_t + applied_offset
            if t.replace(minute=0, second=0, microsecond=0) == now.replace(minute=0, second=0, microsecond=0):
                current_offset_c = applied_offset
        else:
            corrected_t = raw_t

        picto = pick_picto(ct, cl, cm, ch, pr, rn, sn, hu, ws)

        hours_out.append({
            "time": t.isoformat(),
            "temp_raw": round(raw_t, 1),
            "temp_corrected": round(corrected_t, 1),
            "corrected": apply_corr,
            "correction_offset_c": round(applied_offset, 2) if applied_offset is not None else None,
            "clarity": round(clarity, 2) if in_corr_window else None,
            "wind_speed": round(ws, 1),
            "wind_gusts": round(wg, 1),
            "wind_dir": round(wd),
            "cloud_cover": round(ct),
            "cloud_cover_low": round(cl) if cl is not None else None,
            "cloud_cover_mid": round(cm) if cm is not None else None,
            "cloud_cover_high": round(ch) if ch is not None else None,
            "picto": picto,
        })
        t += timedelta(hours=1)

    candidate_evening = window_start.date() - timedelta(days=1)
    cand_start, cand_end = corr_window_for_evening(candidate_evening, sunset_by_date, sunrise_by_date)
    night_ready = cand_end is not None and now >= cand_end
    night_key = candidate_evening.isoformat()

    if night_ready and bias.get("last_processed_night") != night_key and station_has_credentials(station):
        obs_series = fetch_station_obs(station, cand_start - timedelta(minutes=30), cand_end + timedelta(minutes=30))
        if obs_series is None:
            print(f"[warn] [{slug}] Nuit {night_key} non traitée (échec récupération obs) — nouvel essai au prochain passage.",
                  file=sys.stderr)
            learned = None
        else:
            learned = []
            t = cand_start
            while t <= cand_end:
                if t in idx_by_time:
                    i = idx_by_time[t]
                    eff_cloud = effective_cloud(cloud_low[i], cloud_mid[i], cloud_high[i])
                    ws_ = wind_speed[i] or 0
                    clarity_ = clarity_factor(eff_cloud, ws_)
                    if clarity_ >= LEARN_CLARITY_MIN:
                        obs_v = nearest_value(obs_series, t)
                        if obs_v is not None and temp[i] is not None:
                            bucket = min(int((t - cand_start).total_seconds() // 3600), MAX_BUCKET_HOURS)
                            error = obs_v - temp[i]
                            key = str(bucket)
                            old_val = profile.get(key, default_offset(bucket))
                            new_val = (1 - alpha) * old_val + alpha * error
                            profile[key] = round(new_val, 2)
                            learned.append({"bucket": bucket, "error_c": round(error, 2), "offset_after": profile[key]})
                t += timedelta(hours=1)

        if learned is not None:
            bias["offset_profile"] = smooth_profile(profile)
            bias["last_processed_night"] = night_key
            bias["last_night_samples"] = len(learned)
            bias["last_night_error_c"] = (
                round(sum(x["error_c"] for x in learned) / len(learned), 2) if learned else None
            )
            bias["history"].append({"night": night_key, "buckets_learned": learned})
            bias["history"] = bias["history"][-MAX_HISTORY:]

    save_bias_state(bias_path, bias)

    output = {
        "generated_at": now.isoformat(),
        "run_time": times[0].isoformat() if times else None,
        "location": {"name": station["name"], "lat": station["lat"], "lon": station["lon"],
                     "elevation_m": station.get("elevation_m")},
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "correction": {
            "current_offset_c": round(current_offset_c, 2) if current_offset_c is not None else None,
            "offset_profile": {k: bias["offset_profile"][k] for k in sorted(bias["offset_profile"], key=int)},
            "alpha": alpha,
            "last_night_error_c": bias.get("last_night_error_c"),
            "last_night_samples": bias.get("last_night_samples", 0),
            "clear_cloud_threshold_pct": CLEAR_CLOUD_THRESHOLD,
            "cloud_zero_threshold_pct": CLOUD_ZERO_THRESHOLD,
            "calm_wind_threshold_kmh": CALM_WIND_THRESHOLD,
            "wind_zero_threshold_kmh": WIND_ZERO_THRESHOLD,
            "high_cloud_attenuation": HIGH_CLOUD_ATTENUATION,
            "sunset_lead_minutes": SUNSET_LEAD_MINUTES,
        },
        "hours": hours_out,
    }
    with open(forecast_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[{slug}] OK — {len(hours_out)} heures écrites, {len(bias['offset_profile'])} cases horaires apprises")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    now = datetime.now(TZ)
    for station in STATIONS:
        try:
            process_station(station, now)
        except Exception as e:
            print(f"[error] [{station['slug']}] échec du traitement: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
