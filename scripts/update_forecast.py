#!/usr/bin/env python3
"""
Génère data/<slug>.json pour chaque station de la liste STATIONS, à partir
du modèle ICON-CH1 (Open-Meteo / MétéoSuisse), avec correction nocturne du
"trou à froid" apprise automatiquement à partir des observations Datacake
de chaque station.

Toute la logique (nébulosité effective, profil de correction non-linéaire
par case horaire, apprentissage par moyenne mobile, lissage entre cases
voisines) est identique à ce qui existait sur les dépôts séparés
Gréolières / Chaud Clapier - seule la boucle sur plusieurs stations est
nouvelle.

Pour ajouter une station : ajouter une entrée à STATIONS ci-dessous, puis
créer les 3 secrets GitHub Actions correspondants
(DATACAKE_TOKEN_<SUFFIX>, DATACAKE_DEVICE_ID_<SUFFIX>,
DATACAKE_TEMP_FIELD_<SUFFIX>).

Sans les secrets d'une station, celle-ci fonctionne quand même : elle
applique le dernier profil de correction connu (ou le profil par défaut)
mais n'apprend pas.
"""
import json
import math
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import urllib.request
import urllib.parse
import urllib.error

TZ = ZoneInfo("Europe/Paris")

# --- Registre des stations --------------------------------------------------
# env_suffix -> lit les secrets DATACAKE_TOKEN_<suffix>, DATACAKE_DEVICE_ID_<suffix>,
# DATACAKE_TEMP_FIELD_<suffix> (déclarés dans .github/workflows/update.yml).
STATIONS = [
    {
        "slug": "greolieres",
        "name": "Gréolières-les-Neiges",
        "lat": 43.8318,
        "lon": 6.9617,
        "elevation_m": 1388,
        "datacake_url": "https://app.datacake.de/pd/8edbfefb-584e-4866-a684-0b84928b85c9",
        "env_suffix": "GREOLIERES",
    },
    {
        "slug": "chaud-clapier",
        "name": "Doline de Chaud Clapier",
        "lat": 44.915705,
        "lon": 5.335803,
        "elevation_m": 1378,
        "datacake_url": "https://app.datacake.de/pd/d4b23fdc-eca0-4658-9d0b-66829a0b9890",
        "env_suffix": "CHAUD_CLAPIER",
    },
    {
        "slug": "la-pesse",
        "name": "La Pesse - Le Cernétrou",
        "lat": 46.272,
        "lon": 5.834,
        "elevation_m": 1175,
        "datacake_url": "https://app.datacake.de/pd/96a819b1-c90c-4353-8a65-ba13f4fd0276",
        "env_suffix": "LA_PESSE",
    },
    {
        "slug": "tignes",
        "name": "Tignes - Pramécou",
        "lat": 45.439,
        "lon": 6.872,
        "elevation_m": 2684,
        "datacake_url": "https://app.datacake.de/pd/39371b61-daed-40b2-b329-d1e9db559c49",
        "env_suffix": "TIGNES",
    },
    {
        "slug": "beuil",
        "name": "Beuil - Cumba Clava",
        "lat": 44.090,
        "lon": 6.957,
        "elevation_m": 1545,
        "datacake_url": "https://app.datacake.de/pd/6152f64c-a2bc-4678-b6d0-77836ae26eb8",
        "env_suffix": "BEUIL",
    },
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")

CLEAR_CLOUD_THRESHOLD = 20      # nébulosité EFFECTIVE (%) en-dessous de laquelle le ciel est pleinement dégagé
CLOUD_ZERO_THRESHOLD = 45       # nébulosité EFFECTIVE (%) au-delà de laquelle la correction est nulle
CALM_WIND_THRESHOLD = 10        # vent moyen (km/h) en-dessous duquel il est pleinement calme
WIND_ZERO_THRESHOLD = 22        # vent moyen (km/h) au-delà duquel la correction est nulle
HIGH_CLOUD_ATTENUATION = 0.1    # poids résiduel des nuages hauts dans la nébulosité effective
SUNSET_LEAD_MINUTES = 15        # la fenêtre de correction démarre ~15 min avant le coucher du soleil
DEFAULT_ALPHA = 0.25            # poids donné à la dernière nuit dans la moyenne mobile (par case horaire)
MAX_HISTORY = 90                # nombre de nuits conservées dans l'historique
MAX_BUCKET_HOURS = 16           # nombre max de cases horaires suivies après le début de fenêtre
LEARN_CLARITY_MIN = 0.6         # clarté minimale d'une heure pour qu'elle compte dans l'apprentissage


def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_openmeteo(lat, lon, elevation_m=None, past_days=2, forecast_days=3):
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join([
            "temperature_2m", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
            "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
            "precipitation", "rain", "snowfall", "relative_humidity_2m",
        ]),
        "daily": "sunrise,sunset",
        "models": "meteoswiss_icon_ch1",
        "timezone": "Europe/Berlin",
        "wind_speed_unit": "kmh",
        "forecast_days": forecast_days,
        # past_days étend aussi le tableau "daily" (lever/coucher du soleil) en
        # arrière, contrairement à past_hours qui ne joue que sur "hourly" -
        # indispensable pour retrouver le coucher de soleil d'hier soir et
        # calculer rétrospectivement la fenêtre de correction de la nuit passée.
        "past_days": past_days,
    }
    if elevation_m is not None:
        params["elevation"] = elevation_m
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    return http_get_json(url)


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
    donnée, ou None si la requête elle-même a échoué (à ne JAMAIS traiter
    comme "nuit sans données exploitables", pour permettre un nouvel essai
    au prochain passage plutôt que d'abandonner définitivement)."""
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
            "[warn] Datacake a répondu mais sans aucune donnée sur cette période "
            "(vérifier DATACAKE_DEVICE_ID, ou absence de mesures récentes).",
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


def nearest_value(series, target_dt, max_gap_minutes=40):
    best, best_gap = None, None
    for t, v in series:
        gap = abs((t - target_dt).total_seconds()) / 60
        if best_gap is None or gap < best_gap:
            best_gap, best = gap, v
    if best is not None and best_gap is not None and best_gap <= max_gap_minutes:
        return best
    return None


def compute_window(now):
    window_start = now.replace(hour=18, minute=0, second=0, microsecond=0)
    window_end = window_start + timedelta(hours=23)
    if now > window_end:
        window_start += timedelta(days=1)
        window_end += timedelta(days=1)
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


def process_station(station, now):
    slug = station["slug"]
    token = os.environ.get(f"DATACAKE_TOKEN_{station['env_suffix']}", "").strip()
    device_id = os.environ.get(f"DATACAKE_DEVICE_ID_{station['env_suffix']}", "").strip()
    temp_field = os.environ.get(f"DATACAKE_TEMP_FIELD_{station['env_suffix']}", "TEMPERATURE").strip()

    if not token or not device_id:
        print(
            f"[warn] [{slug}] DATACAKE_TOKEN_{station['env_suffix']} et/ou "
            f"DATACAKE_DEVICE_ID_{station['env_suffix']} absents ou vides : "
            "correction appliquée avec le profil déjà appris, mais aucun "
            "apprentissage n'aura lieu ce passage-ci.",
            file=sys.stderr,
        )

    forecast_path = os.path.join(DATA_DIR, f"{slug}.json")
    bias_path = os.path.join(DATA_DIR, f"{slug}_bias_state.json")

    bias = load_bias_state(bias_path)
    profile = bias["offset_profile"]
    alpha = bias.get("alpha", DEFAULT_ALPHA)

    om = fetch_openmeteo(station["lat"], station["lon"], station.get("elevation_m"), past_days=2, forecast_days=3)
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

    window_start, window_end = compute_window(now)
    corr_start, corr_end = corr_window_for_evening(window_start.date(), sunset_by_date, sunrise_by_date)

    def offset_for_bucket(bucket):
        key = str(bucket)
        return profile[key] if key in profile else default_offset(bucket)

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
        in_corr_window = corr_end is not None and corr_start <= t <= corr_end
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

    # --- Apprentissage : nuit la plus récente entièrement écoulée ---
    candidate_evening = window_start.date() - timedelta(days=1)
    cand_start, cand_end = corr_window_for_evening(candidate_evening, sunset_by_date, sunrise_by_date)
    night_ready = cand_end is not None and now >= cand_end
    night_key = candidate_evening.isoformat()

    if night_ready and bias.get("last_processed_night") != night_key and token and device_id:
        obs_series = fetch_datacake_series(token, device_id, temp_field,
                                            cand_start - timedelta(minutes=30), cand_end + timedelta(minutes=30))
        if obs_series is None:
            print(f"[warn] [{slug}] Nuit {night_key} non traitée (échec Datacake) — nouvel essai au prochain passage.",
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
