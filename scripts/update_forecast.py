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
        # Affiche un indicateur "neige fraîche cumulée (24h)" sur la page :
        # une couche de neige fraîche renforce le refroidissement nocturne du
        # trou à froid (albédo élevé, air sec, meilleure émission IR).
        "show_snow": True,
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
        "slug": "stpaul",
        "name": "Saint-Paul-le-Froid",
        "lat": 44.810537,
        "lon": 3.560501,
        "elevation_m": 1161,
        "source": "datacake",
        "datacake_url": "https://app.datacake.de/pd/65e2a79e-7400-468c-b7de-655557c32aa4",
        "env_suffix": "PAUL",
    },
    {
        "slug": "randon",
        "name": "Châteauneuf-de-Randon",
        "lat": 44.655824,
        "lon": 3.670625,
        "elevation_m": 1141,
        "source": "datacake",
        "datacake_url": "https://app.datacake.de/pd/c21a6a3f-174d-46ff-8237-c61cedb15b45",
        "env_suffix": "RANDON",
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
MAX_COND_LOG = 60   # nuits / journées conservées dans le journal des conditions
MAX_BUCKET_HOURS = 20
DAY_MAX_BUCKET_HOURS = 16
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
            "precipitation", "rain", "snowfall", "relative_humidity_2m", "dew_point_2m",
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


# Température à 850 hPa : non disponible sur les modèles MeteoSwiss ICON-CH1/CH2
# (les niveaux de pression ne sont pas encore intégrés côté Open-Meteo pour
# cette famille de modèles) - récupérée séparément sur ICON-EU (DWD, 7 km,
# couvre toute l'Europe donc toutes nos stations). Purement indicative
# (advection d'air en altitude) : jamais utilisée pour la correction ni
# l'apprentissage, qui restent basés uniquement sur ICON-CH1/CH2.
TEMP_850HPA_MODEL = "dwd_icon_eu"


def fetch_850hpa_temp(lat, lon, forecast_days, past_days=2):
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_850hPa",
        "models": TEMP_850HPA_MODEL,
        "timezone": "Europe/Berlin",
        "forecast_days": forecast_days,
        "past_days": past_days,
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    try:
        return http_get_json(url)
    except Exception as e:
        print(f"[warn] Échec récupération température 850 hPa (ICON-EU) : {e}", file=sys.stderr)
        return None


def index_850hpa_by_time(om850):
    """Retourne {datetime_local: temperature_850hPa}, ou {} si la requête a
    échoué ou renvoyé une forme inattendue - l'affichage doit simplement se
    passer de la donnée ce passage-ci, jamais planter dessus."""
    out = {}
    if not om850 or "hourly" not in om850:
        return out
    h850 = om850["hourly"]
    times850 = h850.get("time", [])
    vals850 = h850.get("temperature_850hPa", [])
    for t_str, v in zip(times850, vals850):
        try:
            out[parse_iso_local(t_str)] = v
        except Exception:
            continue
    return out


def merge_ch1_ch2(om1, om2):
    h1, h2 = om1["hourly"], om2["hourly"]
    idx1 = {t: i for i, t in enumerate(h1["time"])}
    idx2 = {t: i for i, t in enumerate(h2["time"])}
    all_times = sorted(set(h1["time"]) | set(h2["time"]))

    var_names = [
        "temperature_2m", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
        "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
        "precipitation", "rain", "snowfall", "relative_humidity_2m", "dew_point_2m",
    ]
    merged_hourly = {"time": all_times}
    for name in var_names:
        vals = []
        arr1, arr2 = h1.get(name), h2.get(name)
        for t in all_times:
            v = None
            if arr1 is not None and t in idx1 and idx1[t] < len(arr1) and arr1[idx1[t]] is not None:
                v = arr1[idx1[t]]
            elif arr2 is not None and t in idx2 and idx2[t] < len(arr2) and arr2[idx2[t]] is not None:
                v = arr2[idx2[t]]
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
        # Correction de journée (Tx) - même mécanique que la nuit (profil par
        # case horaire, EMA), mais fenêtre et historique indépendants.
        state.setdefault("day_offset_profile", {})
        state.setdefault("day_alpha", DEFAULT_ALPHA)
        state.setdefault("last_processed_day", None)
        state.setdefault("last_day_error_c", None)
        state.setdefault("last_day_samples", 0)
        state.setdefault("day_history", [])
        # Fiabilité : écart entre le Tn/Tx réellement prévu (profil figé tel
        # qu'il était avant l'apprentissage de cette nuit/journée) et le
        # Tn/Tx réellement observé à la station - indicateur de confiance
        # affiché sur la page, indépendant du mécanisme d'apprentissage.
        state.setdefault("last_night_verif", None)
        state.setdefault("last_day_verif", None)
        state.setdefault("cond_log", {"columns": COND_COLUMNS, "night": [], "day": []})
        state["cond_log"]["columns"] = COND_COLUMNS
        state["cond_log"].setdefault("night", [])
        state["cond_log"].setdefault("day", [])
        return state
    return {
        "offset_profile": {},
        "alpha": DEFAULT_ALPHA,
        "last_processed_night": None,
        "last_night_error_c": None,
        "last_night_samples": 0,
        "history": [],
        "day_offset_profile": {},
        "day_alpha": DEFAULT_ALPHA,
        "last_processed_day": None,
        "last_day_error_c": None,
        "last_day_samples": 0,
        "day_history": [],
        "last_night_verif": None,
        "last_day_verif": None,
        "cond_log": {"columns": COND_COLUMNS, "night": [], "day": []},
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
        # Forme réelle confirmée de l'API StatIC Infoclimat (opendata) :
        # {"status": "OK", "errors": [...], "data": [...], "stations": [...],
        #  "metadata": {...}, "hourly": {"_params": ["temperature", ...],
        #  "<station_id>": {"YYYY-MM-DD HH:MM:SS": ["<val_param0>", "<val_param1>", ...], ...}}}
        # Les valeurs sont positionnelles par rapport à hourly["_params"].
        hourly = data.get("hourly") if isinstance(data, dict) else None
        if isinstance(hourly, dict):
            params = hourly.get("_params") or []
            station_rows = hourly.get(station_id)
            if isinstance(station_rows, dict) and "temperature" in params:
                temp_idx = params.index("temperature")
                for t_raw, values in station_rows.items():
                    if not isinstance(values, list) or temp_idx >= len(values):
                        continue
                    v = values[temp_idx]
                    if v is None or v == "":
                        continue
                    t_str = str(t_raw).replace("Z", "+00:00").replace(" ", "T")
                    try:
                        t = datetime.fromisoformat(t_str)
                    except ValueError:
                        continue
                    if t.tzinfo is None:
                        # Les horodatages Infoclimat StatIC sont en UTC.
                        t = t.replace(tzinfo=ZoneInfo("UTC"))
                    try:
                        out.append((t.astimezone(TZ), float(v)))
                    except (TypeError, ValueError):
                        continue
            elif isinstance(station_rows, list):
                # Autre forme possible : une liste de lignes, chacune étant soit
                # un dict {"dh_utc"|"date"|"time": ..., "temperature"|<nom_param>: ...},
                # soit une liste positionnelle [horodatage, val_param0, val_param1, ...].
                for row in station_rows:
                    t_raw, v = None, None
                    if isinstance(row, dict):
                        t_raw = row.get("dh_utc") or row.get("timestamp") or row.get("date") or row.get("time")
                        v = row.get("temperature")
                    elif isinstance(row, list) and row:
                        t_raw = row[0]
                        if "temperature" in params:
                            temp_idx = params.index("temperature") + 1  # +1 car row[0] = horodatage
                            if temp_idx < len(row):
                                v = row[temp_idx]
                    if t_raw is None or v is None or v == "":
                        continue
                    t_str = str(t_raw).replace("Z", "+00:00").replace(" ", "T")
                    try:
                        t = datetime.fromisoformat(t_str)
                    except ValueError:
                        continue
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=ZoneInfo("UTC"))
                    try:
                        out.append((t.astimezone(TZ), float(v)))
                    except (TypeError, ValueError):
                        continue

        # Anciennes formes plausibles conservées en repli, au cas où l'API
        # renverrait un jour une structure différente pour une autre station.
        if not out:
            candidates = None
            if isinstance(data, dict) and isinstance(data.get("stations"), dict):
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
        hourly_dbg = data.get("hourly") if isinstance(data, dict) else None
        if isinstance(hourly_dbg, dict):
            hourly_keys = list(hourly_dbg.keys())
            station_rows_dbg = hourly_dbg.get(station_id)
            sample = None
            if isinstance(station_rows_dbg, dict):
                sample = dict(list(station_rows_dbg.items())[:3])
            elif isinstance(station_rows_dbg, list):
                sample = station_rows_dbg[:3]
            print(
                f"[warn] Infoclimat: aucune donnée exploitable extraite pour {station_id}. "
                f"Clés présentes dans data['hourly'] : {hourly_keys!r} — "
                f"_params={hourly_dbg.get('_params')!r} — "
                f"type(hourly[station_id])={type(station_rows_dbg).__name__} — "
                f"échantillon={sample!r}",
                file=sys.stderr,
            )
        else:
            preview = json.dumps(data, ensure_ascii=False)[:800]
            print(
                f"[warn] Infoclimat: aucune donnée exploitable extraite pour {station_id} "
                f"(pas de clé 'hourly' exploitable). Aperçu brut : {preview}",
                file=sys.stderr,
            )
        return []
    return out


LIVE_OBS_HOURS = 48


def fetch_recent_observations(station, now, hours=LIVE_OBS_HOURS):
    """Pour les stations Datacake uniquement : relevé des observations réelles
    des dernières `hours` heures, pour la carte "températures en direct"
    (dernière valeur mesurée + historique horaire cliquable). Contrat :
      - dict {"latest": {...}|None, "recent": [...]} en cas de succès
        (recent peut être vide si la station n'a rien renvoyé)
      - None si la requête elle-même a échoué (pour permettre à l'appelant de
        conserver les dernières valeurs publiées plutôt que d'afficher un trou)
    """
    if station["source"] != "datacake":
        return None
    token = os.environ.get(f"DATACAKE_TOKEN_{station['env_suffix']}", "").strip()
    device_id = os.environ.get(f"DATACAKE_DEVICE_ID_{station['env_suffix']}", "").strip()
    temp_field = os.environ.get(f"DATACAKE_TEMP_FIELD_{station['env_suffix']}", "TEMPERATURE").strip()
    if not (token and device_id):
        return None
    start_dt = now - timedelta(hours=hours)
    series = fetch_datacake_series(token, device_id, temp_field, start_dt, now)
    if series is None:
        return None
    series = sorted(series, key=lambda x: x[0])
    recent = [{"time": t.isoformat(), "temp_c": round(v, 1)} for t, v in series]
    latest = recent[-1] if recent else None
    return {"latest": latest, "recent": recent}


COND_COLUMNS = [
    "bucket", "heure_loc", "obs_c", "modele_c", "erreur_c", "prevu_corrige_c",
    "clarte", "nebul_eff_pct", "vent_kmh", "t_moins_td_c", "t_moins_t850_c", "hr_pct", "appris",
]


def build_condition_rows(
    w_start, w_end, idx_by_time, temp, cloud_low, cloud_mid, cloud_high, wind_speed,
    dew_point, humidity, temp850_by_time, obs_series, max_bucket, profile_snapshot,
    default_fn=None,
):
    """Journal des conditions prévues et de l'erreur obs-modèle, pour TOUTES
    les heures de la fenêtre (y compris nuageuses/ventées, que l'apprentissage
    ignore) - pour pouvoir analyser a posteriori comment l'erreur dépend du
    potentiel radiatif (clarté, vent, écart T-Td, T-T850). Chaque ligne suit
    COND_COLUMNS. 'appris' = 1 si l'heure a alimenté le profil."""
    rows = []
    t = w_start.replace(minute=0, second=0, microsecond=0)
    if t < w_start:
        t += timedelta(hours=1)
    while t <= w_end:
        if t in idx_by_time:
            i = idx_by_time[t]
            obs_v = nearest_value(obs_series, t)
            if obs_v is not None and temp[i] is not None:
                eff_cloud = effective_cloud(cloud_low[i], cloud_mid[i], cloud_high[i])
                ws_ = wind_speed[i] or 0
                clarity_ = clarity_factor(eff_cloud, ws_)
                bucket = min(int((t - w_start).total_seconds() // 3600), max_bucket)
                off = profile_snapshot.get(str(bucket), default_fn(bucket) if default_fn else 0.0)
                td = dew_point[i]
                t850 = temp850_by_time.get(t)

                def r1(v):
                    return round(v, 1) if v is not None else None

                rows.append([
                    bucket, t.hour, r1(obs_v), r1(temp[i]), r1(obs_v - temp[i]),
                    r1(temp[i] + off * clarity_), round(clarity_, 2), round(eff_cloud),
                    r1(ws_), r1(temp[i] - td) if td is not None else None,
                    r1(temp[i] - t850) if t850 is not None else None,
                    r1(humidity[i]), 1 if clarity_ >= LEARN_CLARITY_MIN else 0,
                ])
        t += timedelta(hours=1)
    return rows


def corrected_series_over_window(
    w_start, w_end, idx_by_time, temp, cloud_low, cloud_mid, cloud_high, wind_speed,
    max_bucket, profile_snapshot, default_fn=None,
):
    """Reconstitue, heure par heure sur [w_start, w_end], la température
    "telle que prévue" en appliquant un profil de correction figé (celui
    d'avant l'apprentissage du jour/de la nuit en cours) - sert uniquement à
    l'indicateur de fiabilité (Tn/Tx prévus vs réellement observés), jamais
    à l'apprentissage lui-même (qui mute le profil en direct).

    `w_start` peut ne pas être aligné sur l'heure pile (cas de la fenêtre de
    jour, dont le début doit recoller exactement à la fin de la fenêtre de
    nuit précédente) : on itère à partir de la première heure pile >= w_start
    (seule grille sur laquelle existent des données horaires), mais le calcul
    de la case horaire (bucket) reste basé sur w_start non arrondi, exactement
    comme dans la boucle d'apprentissage correspondante."""
    vals = []
    t = w_start.replace(minute=0, second=0, microsecond=0)
    if t < w_start:
        t += timedelta(hours=1)
    while t <= w_end:
        if t in idx_by_time:
            i = idx_by_time[t]
            if temp[i] is not None:
                eff_cloud = effective_cloud(cloud_low[i], cloud_mid[i], cloud_high[i])
                ws_ = wind_speed[i] or 0
                clarity_ = clarity_factor(eff_cloud, ws_)
                bucket = min(int((t - w_start).total_seconds() // 3600), max_bucket)
                key = str(bucket)
                offset = profile_snapshot.get(key, default_fn(bucket) if default_fn else 0.0)
                vals.append(temp[i] + offset * clarity_)
        t += timedelta(hours=1)
    return vals


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


def day_window_for_date(day_date, sunset_by_date, sunrise_by_date):
    """Fenêtre de correction diurne pour la date donnée : du lever du soleil
    (+1h, symétrique du SUNSET_LEAD_MINUTES ... +1h utilisé côté nuit) au
    coucher du soleil (-SUNSET_LEAD_MINUTES). Complémentaire exacte de la
    fenêtre nocturne (corr_window_for_evening) : les deux se recollent sans
    trou ni recouvrement sur 24h."""
    sunrise_dt = sunrise_by_date.get(day_date)
    sunset_dt = sunset_by_date.get(day_date)
    if sunrise_dt is None or sunset_dt is None:
        return None, None
    # Pas d'arrondi sur le début : doit matcher exactement la fin de la
    # fenêtre de nuit précédente (corr_window_for_evening, qui ne l'arrondit
    # pas non plus), pour que les deux fenêtres se recollent sans trou ni
    # recouvrement sur la grille horaire.
    start = sunrise_dt + timedelta(hours=1)
    end = (sunset_dt - timedelta(minutes=SUNSET_LEAD_MINUTES)).replace(minute=0, second=0, microsecond=0)
    if end <= start:
        return None, None
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
    day_profile = bias["day_offset_profile"]
    day_alpha = bias.get("day_alpha", DEFAULT_ALPHA)

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
    dew_point = series("dew_point_2m")

    idx_by_time = {t: i for i, t in enumerate(times)}

    # Température à 850 hPa (ICON-EU, indicative) - requête séparée des
    # modèles CH1/CH2 (voir commentaire sur TEMP_850HPA_MODEL). On demande une
    # marge confortable pour couvrir toute la fenêtre de prévision (jusqu'à
    # FORECAST_NIGHTS + quelques jours plus loin que la station elle-même).
    om850 = fetch_850hpa_temp(station["lat"], station["lon"], forecast_days=CH2_FORECAST_DAYS + 2)
    temp850_by_time = index_850hpa_by_time(om850)

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

    # Fenêtres de journée (correction Tx) - une par date calendaire couverte
    # par la fenêtre de prévision, complémentaires des fenêtres de nuit.
    day_windows = []
    for n in range(FORECAST_NIGHTS + 2):
        day_date = window_start.date() + timedelta(days=n)
        d_start, d_end = day_window_for_date(day_date, sunset_by_date, sunrise_by_date)
        if d_start is not None and d_end is not None:
            day_windows.append((d_start, d_end))

    def offset_for_bucket(bucket):
        key = str(bucket)
        return profile[key] if key in profile else default_offset(bucket)

    def day_offset_for_bucket(bucket):
        # Pas de modèle a priori pour le biais diurne (contrairement à la
        # nuit) : tant qu'aucune donnée n'a été apprise, la correction est
        # nulle - on ne change rien tant qu'on n'a pas de preuve du biais.
        return day_profile.get(str(bucket), 0.0)

    def find_night_window(t):
        for c_start, c_end in night_windows:
            if c_start <= t <= c_end:
                return c_start, c_end
        return None, None

    def find_day_window(t):
        for d_start, d_end in day_windows:
            if d_start <= t <= d_end:
                return d_start, d_end
        return None, None

    # --- Passe 1 : collecte brute heure par heure --------------------------
    rows = []
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
        dp = dew_point[i]
        t850 = temp850_by_time.get(t)

        eff_cloud = effective_cloud(cl, cm, ch)
        corr_start, corr_end = find_night_window(t)
        in_night_window = corr_start is not None
        day_start, day_end = find_day_window(t)
        in_day_window = day_start is not None

        if in_night_window:
            period = "night"
            w_start = corr_start
            max_bucket = MAX_BUCKET_HOURS
            get_offset = offset_for_bucket
        elif in_day_window:
            period = "day"
            w_start = day_start
            max_bucket = DAY_MAX_BUCKET_HOURS
            get_offset = day_offset_for_bucket
        else:
            period = None
            w_start = None
            max_bucket = None
            get_offset = None

        clarity_raw = clarity_factor(eff_cloud, ws) if period else 0.0

        rows.append({
            "t": t, "raw_t": raw_t, "ws": ws, "wg": wg, "wd": wd, "ct": ct,
            "cl": cl, "cm": cm, "ch": ch, "pr": pr, "rn": rn, "sn": sn, "hu": hu,
            "dp": dp, "t850": t850,
            "period": period, "w_start": w_start, "max_bucket": max_bucket,
            "get_offset": get_offset, "clarity_raw": clarity_raw,
        })
        t += timedelta(hours=1)

    # --- Passe 2 : lissage temporel de la clarté ----------------------------
    # Un pic isolé de nébulosité (ou de vent) sur une seule heure modèle fait
    # chuter la clarté de cette heure à 0 et donc annuler brutalement la
    # correction, qui revient tout aussi brutalement l'heure suivante (écarts
    # de 10°C+ observés). On lisse la clarté sur les heures voisines (moyenne
    # pondérée 0.25/0.5/0.25), en ne mélangeant jamais deux fenêtres
    # différentes (nuit/jour, ou nuits non contiguës séparées par une
    # journée) pour ne pas atténuer les vraies transitions jour/nuit.
    clarity_smoothed = []
    for idx, row in enumerate(rows):
        if row["period"] is None:
            clarity_smoothed.append(0.0)
            continue
        total_w, total_v = 0.0, 0.0
        for off, w in ((-1, 0.25), (0, 0.5), (1, 0.25)):
            j = idx + off
            if 0 <= j < len(rows) and rows[j]["period"] == row["period"] and rows[j]["w_start"] == row["w_start"]:
                total_v += rows[j]["clarity_raw"] * w
                total_w += w
        clarity_smoothed.append(total_v / total_w if total_w > 0 else row["clarity_raw"])

    # --- Passe 3 : application de la correction (clarté lissée) ------------
    current_offset_c = None
    hours_out = []
    for idx, row in enumerate(rows):
        t = row["t"]
        raw_t = row["raw_t"]
        period = row["period"]
        clarity = clarity_smoothed[idx]
        # raw_t peut être None sur un trou ponctuel de données modèle (bord de
        # fusion CH1/CH2) - dans ce cas on n'essaie pas de corriger.
        apply_corr = period is not None and clarity > 0 and raw_t is not None

        applied_offset = None
        if apply_corr:
            bucket = min(int((t - row["w_start"]).total_seconds() // 3600), row["max_bucket"])
            applied_offset = row["get_offset"](bucket) * clarity
            corrected_t = raw_t + applied_offset
            if t.replace(minute=0, second=0, microsecond=0) == now.replace(minute=0, second=0, microsecond=0):
                current_offset_c = applied_offset
        else:
            corrected_t = raw_t

        picto = pick_picto(row["ct"], row["cl"], row["cm"], row["ch"], row["pr"], row["rn"], row["sn"], row["hu"], row["ws"])

        # N'affiche "corrigé" (barré + couleur) que si l'écart est réellement
        # perceptible - sinon (ex: profil de jour pas encore appris, décalage
        # encore à 0) on affiche juste la valeur normale, sans astérisque
        # trompeur sur un écart nul.
        is_meaningful = applied_offset is not None and abs(applied_offset) >= 0.05

        hours_out.append({
            "time": t.isoformat(),
            "temp_raw": round(raw_t, 1) if raw_t is not None else None,
            "temp_corrected": round(corrected_t, 1) if corrected_t is not None else None,
            "corrected": apply_corr and is_meaningful,
            "correction_period": period,
            "correction_offset_c": round(applied_offset, 2) if applied_offset is not None else None,
            "clarity": round(clarity, 2) if period else None,
            "wind_speed": round(row["ws"], 1),
            "wind_gusts": round(row["wg"], 1),
            "wind_dir": round(row["wd"]),
            "cloud_cover": round(row["ct"]),
            "cloud_cover_low": round(row["cl"]) if row["cl"] is not None else None,
            "cloud_cover_mid": round(row["cm"]) if row["cm"] is not None else None,
            "cloud_cover_high": round(row["ch"]) if row["ch"] is not None else None,
            "dew_point_c": round(row["dp"], 1) if row["dp"] is not None else None,
            "temp_850hpa_c": round(row["t850"], 1) if row["t850"] is not None else None,
            "picto": picto,
        })

    fresh_snow_24h_cm = None
    if station.get("show_snow"):
        now_floor = now.replace(minute=0, second=0, microsecond=0)
        window_24h_start = now_floor - timedelta(hours=23)
        total_snow = 0.0
        found_any = False
        for tt, i in idx_by_time.items():
            if window_24h_start <= tt <= now_floor and snowfall[i] is not None:
                total_snow += snowfall[i]
                found_any = True
        fresh_snow_24h_cm = round(total_snow, 1) if found_any else None

    candidate_evening = window_start.date() - timedelta(days=1)
    cand_start, cand_end = corr_window_for_evening(candidate_evening, sunset_by_date, sunrise_by_date)
    night_ready = cand_end is not None and now >= cand_end
    night_key = candidate_evening.isoformat()

    print(
        f"[debug] [{slug}] night_key={night_key} cand_start={cand_start!r} cand_end={cand_end!r} "
        f"night_ready={night_ready} now={now!r} last_processed_night={bias.get('last_processed_night')!r} "
        f"has_creds={station_has_credentials(station)} source={station['source']} "
        f"INFOCLIMAT_API_KEY_set={bool(INFOCLIMAT_API_KEY)}",
        file=sys.stderr,
    )

    if night_ready and bias.get("last_processed_night") != night_key and station_has_credentials(station):
        obs_series = fetch_station_obs(station, cand_start - timedelta(minutes=30), cand_end + timedelta(minutes=30))
        print(f"[debug] [{slug}] obs_series = {obs_series!r}"[:600], file=sys.stderr)
        if obs_series is None:
            print(f"[warn] [{slug}] Nuit {night_key} non traitée (échec récupération obs) — nouvel essai au prochain passage.",
                  file=sys.stderr)
            learned = None
        else:
            # Profil tel qu'il était avant l'apprentissage de cette nuit -
            # c'est ce profil (pas celui mis à jour ci-dessous) qui a réellement
            # servi à produire les prévisions affichées cette nuit-là, donc
            # c'est lui qu'il faut comparer à l'observé pour juger la fiabilité.
            night_profile_snapshot = dict(profile)
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

            print(f"[debug] [{slug}] learned={learned}", file=sys.stderr)

        if learned is not None:
            bias["offset_profile"] = smooth_profile(profile)
            bias["last_processed_night"] = night_key
            bias["last_night_samples"] = len(learned)
            bias["last_night_error_c"] = (
                round(sum(x["error_c"] for x in learned) / len(learned), 2) if learned else None
            )
            bias["history"].append({"night": night_key, "buckets_learned": learned})
            bias["history"] = bias["history"][-MAX_HISTORY:]

            # --- Fiabilité : Tn réellement prévu (profil figé) vs Tn observé ---
            forecast_vals = corrected_series_over_window(
                cand_start, cand_end, idx_by_time, temp, cloud_low, cloud_mid, cloud_high,
                wind_speed, MAX_BUCKET_HOURS, night_profile_snapshot, default_fn=default_offset,
            )
            observed_vals = [v for _, v in obs_series]
            if forecast_vals and observed_vals:
                forecast_tn = min(forecast_vals)
                observed_tn = min(observed_vals)
                bias["last_night_verif"] = {
                    "night": night_key,
                    "forecast_tn_c": round(forecast_tn, 1),
                    "observed_tn_c": round(observed_tn, 1),
                    "diff_c": round(forecast_tn - observed_tn, 1),
                }
            else:
                bias["last_night_verif"] = None

            # --- Journal des conditions (toutes les heures observées) -------
            cond_rows = build_condition_rows(
                cand_start, cand_end, idx_by_time, temp, cloud_low, cloud_mid, cloud_high,
                wind_speed, dew_point, humidity, temp850_by_time, obs_series,
                MAX_BUCKET_HOURS, night_profile_snapshot, default_fn=default_offset,
            )
            if cond_rows:
                bias["cond_log"]["night"].append({"night": night_key, "rows": cond_rows})
                bias["cond_log"]["night"] = bias["cond_log"]["night"][-MAX_COND_LOG:]

    # --- Apprentissage rétrospectif de la correction de journée (Tx) -------
    # Même principe que la nuit : on cherche la journée la plus récente déjà
    # terminée (aujourd'hui si son créneau diurne est fini, sinon hier) et on
    # ne la traite qu'une fois (bias["last_processed_day"]).
    candidate_day_date = now.date()
    d_start, d_end = day_window_for_date(candidate_day_date, sunset_by_date, sunrise_by_date)
    if d_end is None or now < d_end:
        candidate_day_date = now.date() - timedelta(days=1)
        d_start, d_end = day_window_for_date(candidate_day_date, sunset_by_date, sunrise_by_date)
    day_ready = d_end is not None and now >= d_end
    day_key = candidate_day_date.isoformat()

    if day_ready and bias.get("last_processed_day") != day_key and station_has_credentials(station):
        obs_series_day = fetch_station_obs(station, d_start - timedelta(minutes=30), d_end + timedelta(minutes=30))
        if obs_series_day is None:
            print(f"[warn] [{slug}] Journée {day_key} non traitée (échec récupération obs) — nouvel essai au prochain passage.",
                  file=sys.stderr)
            learned_day = None
        else:
            # Même logique que côté nuit : on fige le profil d'avant mise à
            # jour pour juger a posteriori de la fiabilité de ce qui a été
            # réellement affiché aux utilisateurs pendant cette journée.
            day_profile_snapshot = dict(day_profile)
            learned_day = []
            # d_start n'est pas forcément aligné sur l'heure pile (il doit
            # recoller exactement à la fin de la fenêtre de nuit précédente) -
            # on démarre l'itération à la première heure pile >= d_start,
            # seule grille sur laquelle existent des données horaires.
            t = d_start.replace(minute=0, second=0, microsecond=0)
            if t < d_start:
                t += timedelta(hours=1)
            while t <= d_end:
                if t in idx_by_time:
                    i = idx_by_time[t]
                    eff_cloud = effective_cloud(cloud_low[i], cloud_mid[i], cloud_high[i])
                    ws_ = wind_speed[i] or 0
                    clarity_ = clarity_factor(eff_cloud, ws_)
                    if clarity_ >= LEARN_CLARITY_MIN:
                        obs_v = nearest_value(obs_series_day, t)
                        if obs_v is not None and temp[i] is not None:
                            bucket = min(int((t - d_start).total_seconds() // 3600), DAY_MAX_BUCKET_HOURS)
                            error = obs_v - temp[i]
                            key = str(bucket)
                            old_val = day_profile.get(key, 0.0)
                            new_val = (1 - day_alpha) * old_val + day_alpha * error
                            day_profile[key] = round(new_val, 2)
                            learned_day.append({"bucket": bucket, "error_c": round(error, 2), "offset_after": day_profile[key]})
                t += timedelta(hours=1)

            print(f"[debug] [{slug}] learned_day={learned_day}", file=sys.stderr)

        if learned_day is not None:
            bias["day_offset_profile"] = smooth_profile(day_profile)
            bias["last_processed_day"] = day_key
            bias["last_day_samples"] = len(learned_day)
            bias["last_day_error_c"] = (
                round(sum(x["error_c"] for x in learned_day) / len(learned_day), 2) if learned_day else None
            )
            bias["day_history"].append({"day": day_key, "buckets_learned": learned_day})
            bias["day_history"] = bias["day_history"][-MAX_HISTORY:]

            # --- Fiabilité : Tx réellement prévu (profil figé) vs Tx observé ---
            forecast_vals_day = corrected_series_over_window(
                d_start, d_end, idx_by_time, temp, cloud_low, cloud_mid, cloud_high,
                wind_speed, DAY_MAX_BUCKET_HOURS, day_profile_snapshot, default_fn=lambda b: 0.0,
            )
            observed_vals_day = [v for _, v in obs_series_day]
            if forecast_vals_day and observed_vals_day:
                forecast_tx = max(forecast_vals_day)
                observed_tx = max(observed_vals_day)
                bias["last_day_verif"] = {
                    "day": day_key,
                    "forecast_tx_c": round(forecast_tx, 1),
                    "observed_tx_c": round(observed_tx, 1),
                    "diff_c": round(forecast_tx - observed_tx, 1),
                }
            else:
                bias["last_day_verif"] = None

            cond_rows_day = build_condition_rows(
                d_start, d_end, idx_by_time, temp, cloud_low, cloud_mid, cloud_high,
                wind_speed, dew_point, humidity, temp850_by_time, obs_series_day,
                DAY_MAX_BUCKET_HOURS, day_profile_snapshot, default_fn=lambda b: 0.0,
            )
            if cond_rows_day:
                bias["cond_log"]["day"].append({"day": day_key, "rows": cond_rows_day})
                bias["cond_log"]["day"] = bias["cond_log"]["day"][-MAX_COND_LOG:]

    save_bias_state(bias_path, bias)

    # --- Observations récentes "en direct" (carte, stations Datacake) ------
    live = fetch_recent_observations(station, now)
    if live is None:
        # Échec de la requête (ou station non-Datacake) : on tente de
        # conserver les dernières valeurs déjà publiées plutôt que de faire
        # disparaître la station de la carte "en direct".
        try:
            with open(forecast_path, "r", encoding="utf-8") as f:
                prev_output = json.load(f)
            live = prev_output.get("live")
        except Exception:
            live = None
    live_out = {
        "available": bool(live and live.get("recent")),
        "latest": (live or {}).get("latest"),
        "recent": (live or {}).get("recent", []),
    }

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
        "day_correction": {
            "offset_profile": {k: bias["day_offset_profile"][k] for k in sorted(bias["day_offset_profile"], key=int)},
            "alpha": day_alpha,
            "last_day_error_c": bias.get("last_day_error_c"),
            "last_day_samples": bias.get("last_day_samples", 0),
        },
        "snow": {
            "show": bool(station.get("show_snow", False)),
            "fresh_24h_cm": fresh_snow_24h_cm,
        },
        "verification": {
            "night": bias.get("last_night_verif"),
            "day": bias.get("last_day_verif"),
        },
        "live": live_out,
        "hours": hours_out,
    }
    with open(forecast_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[{slug}] OK — {len(hours_out)} heures écrites, {len(bias['offset_profile'])} cases horaires apprises")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    now = datetime.now(TZ)
    # STATIONS_SOURCE_FILTER (variable d'env optionnelle) limite le run aux
    # stations d'une source donnée ("datacake" ou "infoclimat") - utile pour
    # séparer le workflow horaire (cloud, Datacake) du workflow dédié aux
    # stations Infoclimat (runner auto-hébergé, IP fixe requise par leur API).
    source_filter = os.environ.get("STATIONS_SOURCE_FILTER", "").strip()
    stations_to_run = STATIONS
    if source_filter:
        stations_to_run = [s for s in STATIONS if s["source"] == source_filter]
        print(f"[info] Filtré sur source='{source_filter}' : {[s['slug'] for s in stations_to_run]}")
    for station in stations_to_run:
        try:
            process_station(station, now)
        except Exception as e:
            print(f"[error] [{station['slug']}] échec du traitement: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()