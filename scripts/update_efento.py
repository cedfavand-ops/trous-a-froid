#!/usr/bin/env python3
"""
Génère data/efento.json (série température + humidité des dernières 48h) pour
la station Efento "Causse de Montbel", à partir de l'API Efento Cloud.

Secret requis : EFENTO_TOKEN (jeton créé dans Efento Cloud > Access > API tokens).
Variables optionnelles :
  EFENTO_STATION_NAME           nom (ou fragment) du point de mesure / de la
                                localisation à rechercher (défaut: "Causse de Montbel")
  EFENTO_MEASUREMENT_POINT_ID   si renseigné, court-circuite la recherche par nom

La doc publique de l'API (v2, 2022) ne détaille ni le format exact de la
réponse des mesures, ni le mode d'authentification d'un jeton API : ce script
essaie donc plusieurs formes plausibles et journalise de façon détaillée (sans
jamais afficher le jeton) ce qu'il a trouvé, pour pouvoir ajuster rapidement.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")
UTC = ZoneInfo("UTC")
BASE_URL = "https://cloud.efento.io/api/v2"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
OUT_PATH = os.path.join(DATA_DIR, "efento.json")

TOKEN = os.environ.get("EFENTO_TOKEN", "").strip()
STATION_NAME = os.environ.get("EFENTO_STATION_NAME", "").strip() or "Causse de Montbel"
FORCED_POINT_ID = os.environ.get("EFENTO_MEASUREMENT_POINT_ID", "").strip()
HOURS = 48

# Schémas d'authentification essayés dans l'ordre jusqu'au premier qui répond 200.
AUTH_SCHEMES = [
    ("X-API-KEY", lambda t: {"X-API-KEY": t}),
    ("Bearer", lambda t: {"Authorization": f"Bearer {t}"}),
    ("Authorization brut", lambda t: {"Authorization": t}),
]

_headers = {}


def log(msg):
    print(msg, file=sys.stderr)


def http_get(path, params=None, headers=None):
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api_get(path, params=None):
    try:
        return http_get(path, params, _headers)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        log(f"[warn] GET {path} {params or ''} -> HTTP {e.code} {e.reason} {body}")
    except Exception as e:
        log(f"[warn] GET {path} -> {e}")
    return None


def authenticate():
    global _headers
    for name, build in AUTH_SCHEMES:
        try:
            http_get("/organizations", None, build(TOKEN))
        except urllib.error.HTTPError as e:
            log(f"[info] Authentification '{name}' refusée (HTTP {e.code}).")
            continue
        except Exception as e:
            log(f"[info] Authentification '{name}' : erreur {e}.")
            continue
        _headers = build(TOKEN)
        log(f"[info] Authentification acceptée : schéma '{name}'.")
        return True
    return False


def paged(path, key, params):
    out, offset = [], 0
    while True:
        p = dict(params, limit=100, offset=offset)
        data = api_get(path, p)
        if not isinstance(data, dict):
            break
        items = data.get(key) or []
        out.extend(items)
        total = data.get("totalCount")
        offset += 100
        if not items or total is None or offset >= total:
            break
    return out


def find_measurement_points():
    """Retourne la liste des points de mesure à utiliser."""
    if FORCED_POINT_ID:
        return [{"id": FORCED_POINT_ID, "name": f"point {FORCED_POINT_ID}"}]

    needle = STATION_NAME.lower()
    orgs = api_get("/organizations")
    org_list = (orgs or {}).get("organizations") or []
    log(f"[info] {len(org_list)} organisation(s) visible(s) : {[o.get('name') for o in org_list]}")

    matches, seen = [], set()
    all_names = []
    for org in org_list:
        locations = paged("/locations", "locations", {"organization-id": org.get("id")})
        log(f"[info] Organisation '{org.get('name')}' : {len(locations)} localisation(s) : {[l.get('name') for l in locations]}")
        for loc in locations:
            points = paged("/measurement-points", "measurementPoints", {"location-ids": loc.get("id")})
            for pt in points:
                all_names.append(f"{loc.get('name')} / {pt.get('name')}")
                hay = f"{loc.get('name', '')} {pt.get('name', '')}".lower()
                if needle in hay and pt.get("id") not in seen:
                    seen.add(pt.get("id"))
                    matches.append(pt)
    if not matches:
        log(f"[error] Aucun point de mesure ne correspond à '{STATION_NAME}'. Points visibles : {all_names}")
    else:
        log(f"[info] Points retenus : {[(m.get('id'), m.get('name')) for m in matches]}")
    return matches


def parse_time(raw):
    """Horodatages Efento : 'YYYY-MM-DD HH:MM:SS' ou ISO ; sans fuseau -> UTC."""
    if raw is None:
        return None
    s = str(raw).strip().replace("Z", "+00:00").replace(" ", "T")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromtimestamp(float(raw) / (1000 if float(raw) > 1e11 else 1), tz=UTC)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(TZ)


def classify(channel):
    """'temp', 'hum' ou None, d'après le type / nom du canal."""
    label = f"{channel.get('type', '')} {channel.get('name', '')}".lower()
    if "temp" in label:
        return "temp"
    if "humid" in label or label.strip().startswith("rh") or " rh" in label:
        return "hum"
    return None


def to_float(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def extract_samples(payload):
    """Renvoie une liste de (datetime, 'temp'|'hum', valeur) depuis la réponse
    des mesures, en gérant plusieurs structures plausibles."""
    if isinstance(payload, dict):
        items = payload.get("measurements") or payload.get("data") or payload.get("items") or []
    else:
        items = payload or []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        t = parse_time(it.get("measuredAt") or it.get("timestamp") or it.get("time"))
        if t is None:
            continue
        if isinstance(it.get("channels"), list):          # un instant, plusieurs canaux
            for ch in it["channels"]:
                kind = classify(ch)
                v = to_float(ch.get("value"))
                if kind and v is not None:
                    out.append((t, kind, v))
        else:                                              # une ligne par canal
            kind = classify(it)
            v = to_float(it.get("value"))
            if kind and v is not None:
                out.append((t, kind, v))
    return out


def main():
    if not TOKEN:
        log("[error] EFENTO_TOKEN absent ou vide.")
        sys.exit(1)
    if not authenticate():
        log("[error] Aucun schéma d'authentification accepté avec ce jeton. "
            "Vérifier le jeton (actif, droits sur la localisation) ; sinon me transmettre le message ci-dessus.")
        sys.exit(1)

    points = find_measurement_points()
    if not points:
        sys.exit(1)

    now = datetime.now(UTC)
    start = now - timedelta(hours=HOURS)
    fmt = "%Y-%m-%d %H:%M:%S"
    merged = {}   # datetime -> {"temp":..., "hum":...}
    sample_logged = False
    for pt in points:
        payload = api_get(f"/measurement-points/{pt['id']}/measurements",
                          {"from": start.strftime(fmt), "to": now.strftime(fmt)})
        if payload is None:
            continue
        if not sample_logged:
            log(f"[debug] Extrait brut de la réponse des mesures : {json.dumps(payload, ensure_ascii=False)[:700]}")
            sample_logged = True
        samples = extract_samples(payload)
        log(f"[info] Point {pt.get('id')} : {len(samples)} valeur(s) température/humidité extraites.")
        for t, kind, v in samples:
            merged.setdefault(t, {})[kind] = v

    if not merged:
        log("[error] Aucune mesure exploitable extraite (voir l'extrait brut ci-dessus). Fichier non modifié.")
        sys.exit(1)

    series = [
        {"time": t.isoformat(), "temp_c": round(v["temp"], 1) if "temp" in v else None,
         "humidity_pct": round(v["hum"], 1) if "hum" in v else None}
        for t, v in sorted(merged.items())
    ]
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now(TZ).isoformat(),
            "name": STATION_NAME,
            "series": series,
        }, f, ensure_ascii=False, indent=2)
    log(f"[efento] OK — {len(series)} mesures écrites (dernière : {series[-1]}).")


if __name__ == "__main__":
    main()
