"""Garmin -> versleuteld dashboard.

Haalt je eigen data op via de officiele Garmin Connect Personal API (gratis),
zet ze om naar een compact formaat, versleutelt alles (AES-256-GCM) en
publiceert het naar een GitHub Pages-repo. Geen Claude-licentie of betaalde
dienst nodig.

Gebruik:
    python garmin_sync.py              # ophalen + versleutelen + publiceren
    python garmin_sync.py --local      # alles behalve publiceren (test)
    python garmin_sync.py --schema     # structuur (zonder waarden) dumpen om te delen
    python garmin_sync.py --demo       # demodata, geen Garmin nodig
    python garmin_sync.py --setup      # GitHub-token + wachtwoord opslaan in Windows Credential Manager

Geheimen:
    Garmin-sleutel      -> ~/.config/garmin/connect-personal-api-key (Garmin-standaard)
    GitHub-token        -> Windows Credential Manager (keyring)
    Dashboardwachtwoord -> Windows Credential Manager (keyring), plus een vaste salt
"""

from __future__ import annotations

import argparse
import base64
import getpass
import gzip
import hashlib
import json
import logging
import logging.handlers
import math
import os
import random
import secrets
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

APP_DIR = Path(os.environ.get("GARMIN_DASH_HOME", Path.home() / ".garmin-dashboard"))
CONFIG_FILE = APP_DIR / "config.json"
CACHE_FILE = APP_DIR / "cache.json"
LOG_FILE = APP_DIR / "sync.log"
OUT_DIR = APP_DIR / "out"
LOCAL_HTML = APP_DIR / "Lijf.html"   # zelfstandig dashboard (data ingebakken), werkt zonder hosting
SITE_DIR = Path(__file__).resolve().parent / "site"
SITE_FILES = ("index.html", "manifest.json", "icon.svg", "icon-180.png", "icon-512.png")
KEYRING_SERVICE = "garmin-dashboard"
PBKDF2_ITERATIONS = 600_000
AAD = "garmin-dashboard-v2"
SCHEMA_VERSION = 2
CORE_LABELS = ("dailySummaries", "steps", "sleepSummaries", "sleepDaily", "hrv", "bodyBattery")

DEFAULT_CONFIG = {
    "github_owner": "",
    "github_repo": "garmin-dashboard",
    "github_branch": "main",
    "days": 365,            # dagen dagelijkse data in het dashboard
    "activity_days": 400,   # dagen activiteitenhistoriek (jaaroverzicht + heatmap)
    "chunk_days": 28,       # bereik per API-call
    "recent_days": 35,      # na de eerste volledige run: enkel deze periode opnieuw ophalen
    "full_every_days": 7,   # en wekelijks toch alles opnieuw
    "per_day_refetch": 3,   # laatste N dagen altijd opnieuw per dag ophalen
    "sleep_budget": 60,     # max per-dag slaapcalls per run (backfill spreidt zich)
}

log = logging.getLogger("garmin_sync")


# --------------------------------------------------------------------------- helpers

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    return cfg


def iso(d: date) -> str:
    return d.isoformat()


def daterange_chunks(start: date, end: date, size: int) -> Iterable[tuple[date, date]]:
    cur = start
    while cur <= end:
        stop = min(end, cur + timedelta(days=size - 1))
        yield cur, stop
        cur = stop + timedelta(days=1)


def num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v if math.isfinite(v) else None
    if isinstance(v, str):
        try:
            f = float(v)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def find(obj: Any, *keys: str, depth: int = 4) -> Any:
    """Zoek de eerste aanwezige sleutel (in volgorde van `keys`) in een geneste dict.

    Breadth-first, zodat een sleutel op het hoogste niveau wint van een diepere.
    Robuust tegen kleine verschillen in de responsstructuur.
    """
    level = [obj]
    for _ in range(depth + 1):
        nxt = []
        for key in keys:
            for o in level:
                if isinstance(o, dict) and key in o and o[key] is not None:
                    return o[key]
        for o in level:
            if isinstance(o, dict):
                nxt.extend(v for v in o.values() if isinstance(v, (dict, list)))
            elif isinstance(o, list):
                nxt.extend(v for v in o[:50] if isinstance(v, (dict, list)))
        level = nxt
        if not level:
            break
    return None


def fnum(obj: Any, *keys: str) -> float | None:
    return num(find(obj, *keys))


def as_list(payload: Any, *container_keys: str) -> list:
    """Haal de lijst met dag-items uit een respons (lijst, of dict met lijst erin)."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in container_keys + ("data", "results", "items", "summaries", "values"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        return [payload]
    return []


DATE_KEYS = ("calendarDate", "date", "calendarDay", "summaryDate", "day")


def item_date(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    v = find(item, *DATE_KEYS, depth=1)
    if isinstance(v, str) and len(v) >= 10:
        return v[:10]
    return None


def hhmm_from(v: Any) -> str | None:
    """Tijd (HH:MM) uit een *lokale* timestamp-string of 'lokale' epoch-ms (Garmin-conventie)."""
    if isinstance(v, str) and "T" in v:
        return v.split("T", 1)[1][:5]
    if isinstance(v, str) and " " in v:
        return v.split(" ", 1)[1][:5]
    n = num(v)
    if n and n > 1e11:
        return datetime.fromtimestamp(n / 1000, tz=timezone.utc).strftime("%H:%M")
    return None


def round_or_none(v: float | None, nd: int = 0) -> float | int | None:
    if v is None:
        return None
    r = round(v, nd)
    return int(r) if nd == 0 else r


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


# --------------------------------------------------------------------------- normalisatie

def norm_daily_summary(item: dict) -> dict:
    dist = fnum(item, "totalDistanceMeters", "distanceMeters", "totalDistance")
    return {
        "steps": fnum(item, "totalSteps", "steps"),
        "stepGoal": fnum(item, "dailyStepGoal", "stepGoal", "totalStepGoal"),
        "distKm": dist / 1000 if dist else None,
        "rhr": fnum(item, "restingHeartRate", "restingValue", "restingHR"),
        "rhr7": fnum(item, "lastSevenDaysAvgRestingHeartRate"),
        "kcal": fnum(item, "totalKilocalories", "totalCalories", "calories", "kilocalories"),
        "activeKcal": fnum(item, "activeKilocalories", "activeCalories"),
        "intensityMin": _intensity(item),
        "floors": fnum(item, "floorsAscended", "floorsClimbed", "floors"),
        "stressAvg": fnum(item, "averageStressLevel", "avgStressLevel", "overallStressLevel"),
        "bbHigh": fnum(item, "bodyBatteryHighestValue", "bodyBatteryHigh", "highestBodyBattery"),
        "bbLow": fnum(item, "bodyBatteryLowestValue", "bodyBatteryLow", "lowestBodyBattery"),
        "bbCharged": fnum(item, "bodyBatteryChargedValue", "bodyBatteryCharged"),
        "bbDrained": fnum(item, "bodyBatteryDrainedValue", "bodyBatteryDrained"),
        "spo2Avg": fnum(item, "averageSpo2", "averageSpO2", "avgSpo2"),
    }


def _intensity(item: dict) -> float | None:
    mod = fnum(item, "moderateIntensityMinutes", "moderateMinutes")
    vig = fnum(item, "vigorousIntensityMinutes", "vigorousMinutes")
    if mod is None and vig is None:
        return fnum(item, "intensityMinutes", "totalIntensityMinutes")
    return (mod or 0) + 2 * (vig or 0)  # WHO-telling: intensief telt dubbel


def norm_steps(item: dict) -> dict:
    dist = fnum(item, "totalDistance", "distanceMeters", "totalDistanceMeters")
    return {
        "steps": fnum(item, "totalSteps", "steps"),
        "stepGoal": fnum(item, "stepGoal", "dailyStepGoal"),
        "distKm": dist / 1000 if dist else None,
    }


def norm_stress(item: dict) -> dict:
    out = {"stressAvg": fnum(item, "overallStressLevel", "averageStressLevel", "avgStressLevel")}
    hi = fnum(item, "highStressDuration")
    rest = fnum(item, "restStressDuration")
    if hi is not None:
        out["stressHighMin"] = hi / 60
    if rest is not None:
        out["stressRestMin"] = rest / 60
    return out


def _bb_level(row: Any, idx: int | None) -> float | None:
    if not isinstance(row, list):
        return None
    if idx is not None and idx < len(row):
        return num(row[idx])
    # Zonder descriptor: [ts, level] of [ts, "MEASURED", level, version]
    if len(row) >= 3 and num(row[1]) is None:
        return num(row[2])
    return num(row[1]) if len(row) >= 2 else None


def norm_body_battery(item: dict) -> dict:
    out = {
        "bbCharged": fnum(item, "charged", "bodyBatteryChargedValue"),
        "bbDrained": fnum(item, "drained", "bodyBatteryDrainedValue"),
    }
    idx = None
    desc = find(item, "bodyBatteryValueDescriptorDTOList", "bodyBatteryValueDescriptorsDTOList")
    if isinstance(desc, list):
        for dsc in desc:
            if isinstance(dsc, dict) and "level" in str(dsc.get("bodyBatteryValueDescriptorKey", "")).lower():
                idx = int(num(dsc.get("bodyBatteryValueDescriptorIndex")) or 0) or None
    vals = find(item, "bodyBatteryValuesArray")
    levels = []
    if isinstance(vals, list):
        for row in vals:
            lv = _bb_level(row, idx)
            if lv is not None and 0 <= lv <= 100:
                levels.append(lv)
    if levels:
        out["bbHigh"] = max(levels)
        out["bbLow"] = min(levels)
    else:
        out["bbHigh"] = fnum(item, "highestValue", "bodyBatteryHighestValue")
        out["bbLow"] = fnum(item, "lowestValue", "bodyBatteryLowestValue")
    events = find(item, "bodyBatteryActivityEvent")
    if isinstance(events, list):
        for ev in events:
            if isinstance(ev, dict) and str(ev.get("eventType", "")).upper() == "SLEEP":
                out["bbSleepGain"] = num(ev.get("bodyBatteryImpact"))
    return out


def norm_hrv(item: dict) -> dict:
    base = find(item, "baseline") or {}
    status = find(item, "status", "hrvStatus")
    return {
        "hrv": fnum(item, "lastNightAvg", "lastNightAverage", "avgOvernightHrv", "hrvValue"),
        "hrvWeekly": fnum(item, "weeklyAvg", "weeklyAverage"),
        "hrvStatus": status if isinstance(status, str) else None,
        "hrvLow": num(base.get("balancedLow")) if isinstance(base, dict) else None,
        "hrvHigh": num(base.get("balancedUpper")) if isinstance(base, dict) else None,
    }


def norm_sleep(item: dict) -> dict:
    dto = find(item, "dailySleepDTO") or item
    score = find(dto, "sleepScores")
    sleep_score = None
    if isinstance(score, dict):
        overall = score.get("overall")
        sleep_score = num(overall.get("value")) if isinstance(overall, dict) else num(overall)
    if sleep_score is None:
        sleep_score = fnum(dto, "overallSleepScore", "sleepScore")
    # Enkel lokale tijden: GMT-varianten zouden de bedtijd 1-2 u verschuiven
    start = find(dto, "sleepStartTimestampLocal", "startTimestampLocal", "sleepStartTimeLocal")
    end = find(dto, "sleepEndTimestampLocal", "endTimestampLocal", "sleepEndTimeLocal")
    return {
        "sleepSec": fnum(dto, "sleepTimeSeconds", "totalSleepSeconds", "sleepDurationSeconds"),
        "deepSec": fnum(dto, "deepSleepSeconds", "deepSleepDurationInSeconds"),
        "lightSec": fnum(dto, "lightSleepSeconds", "lightSleepDurationInSeconds"),
        "remSec": fnum(dto, "remSleepSeconds", "remSleepInSeconds"),
        "awakeSec": fnum(dto, "awakeSleepSeconds", "awakeDurationInSeconds"),
        "sleepScore": sleep_score,
        "bedtime": hhmm_from(start),
        "wake": hhmm_from(end),
        "sleepHrv": fnum(item, "avgOvernightHrv"),
        "sleepRhr": fnum(item, "restingHeartRate"),
        "respiration": fnum(dto, "averageRespirationValue", "avgSleepRespirationValue"),
    }


def norm_spo2(item: dict) -> dict:
    return {"spo2Avg": fnum(item, "averageSpO2", "averageSpo2"), "spo2Low": fnum(item, "lowestSpO2", "lowestSpo2")}


def norm_heart_rate(item: dict) -> dict:
    return {"rhr": fnum(item, "restingHeartRate"), "rhr7": fnum(item, "lastSevenDaysAvgRestingHeartRate"),
            "hrMax": fnum(item, "maxHeartRate")}


def norm_activity(a: dict) -> dict | None:
    if not isinstance(a, dict):
        return None
    atype = find(a, "activityType")
    tkey = atype.get("typeKey") if isinstance(atype, dict) else (atype if isinstance(atype, str) else None)
    start = find(a, "startTimeLocal", "startTimestampLocal") or ""
    dur = fnum(a, "duration", "elapsedDuration", "movingDuration")
    dist = fnum(a, "distance")
    out = {
        "id": find(a, "activityId"),
        "name": str(find(a, "activityName") or tkey or "Activiteit")[:80],
        "type": tkey or "other",
        "start": str(start)[:16].replace(" ", "T"),
        "durMin": round_or_none(dur / 60 if dur else None, 1),
        "distKm": round_or_none(dist / 1000 if dist else None, 2),
        "kcal": round_or_none(fnum(a, "calories")),
        "avgHr": round_or_none(fnum(a, "averageHR", "averageHeartRate")),
        "maxHr": round_or_none(fnum(a, "maxHR", "maxHeartRate")),
        "load": round_or_none(fnum(a, "activityTrainingLoad", "trainingLoad"), 1),
        "aeTE": round_or_none(fnum(a, "aerobicTrainingEffect"), 1),
        "anTE": round_or_none(fnum(a, "anaerobicTrainingEffect"), 1),
        "vo2": round_or_none(fnum(a, "vO2MaxValue", "vo2MaxValue"), 1),
        "elev": round_or_none(fnum(a, "elevationGain")),
        "teLabel": find(a, "trainingEffectLabel", "aerobicTrainingEffectMessage") if isinstance(
            find(a, "trainingEffectLabel", "aerobicTrainingEffectMessage"), str) else None,
        "plan": True if find(a, "workoutId", "associatedWorkoutId", "workoutUuid", depth=1) else None,
    }
    if not out["start"] or len(out["start"]) < 10:
        return None
    return {k: v for k, v in out.items() if v is not None}


# --------------------------------------------------------------------------- ophalen

FAIL = object()  # sentinel: call mislukt (los van een lege respons)


class Fetcher:
    def __init__(self, client: Any, cfg: dict):
        self.c = client
        self.cfg = cfg
        self.stats: dict[str, list[int]] = {}   # label -> [ok, totaal]
        self.last_err: dict[str, str] = {}
        self.dead: set[str] = set()  # endpoints die deze run blijvend falen (5xx/429/netwerk)
        self.retry_wait = cfg.get("retry_wait", 5)

    def call(self, label: str, fn: Callable[[], Any]) -> Any:
        """Voert een API-call uit; geeft FAIL terug bij een fout."""
        import requests

        st = self.stats.setdefault(label, [0, 0])
        st[1] += 1
        if label in self.dead:
            return FAIL
        for attempt in range(3):
            try:
                res = fn()
                st[0] += 1
                return res
            except Exception as exc:  # noqa: BLE001 - per endpoint doorgaan
                status = getattr(exc, "status_code", None)
                if status == 401:
                    raise
                transient = status in (429, 500, 502, 503, 504) or isinstance(
                    exc, (requests.ConnectionError, requests.Timeout))
                if transient and attempt < 2:
                    time.sleep(self.retry_wait * (6 if status == 429 else 1) * (attempt + 1))
                    continue
                if transient:
                    self.dead.add(label)
                self.last_err[label] = str(status or type(exc).__name__)
                log.warning("%s mislukt (%s)", label, self.last_err[label])
                return FAIL
        return FAIL

    def get(self, label: str, fn: Callable[[], Any]) -> Any:
        res = self.call(label, fn)
        return None if res is FAIL else res

    def ranged(self, label: str, method: str, start: date, end: date, *container: str) -> list[tuple[str, dict]]:
        out = []
        fn = getattr(self.c, method)
        for s, e in daterange_chunks(start, end, self.cfg["chunk_days"]):
            payload = self.get(label, lambda s=s, e=e: fn(s, e))
            for item in as_list(payload, *container):
                d = item_date(item)
                if d:
                    out.append((d, item))
        return out

    def activities(self, start: date, end: date) -> tuple[list[dict], bool]:
        acts, page, ok = [], 0, True
        while page < 20:
            payload = self.call("activities", lambda p=page: self.c.search_activities(
                start=p * 100, limit=100, start_date=start, end_date=end))
            if payload is FAIL:
                ok = False
                break
            items = as_list(payload, "activityList", "activities")
            items = [i for i in items if isinstance(i, dict) and find(i, "activityId", depth=0)]
            acts.extend(items)
            if len(items) < 100:
                break
            page += 1
        return acts, ok

    def coverage(self) -> dict[str, str]:
        out = {}
        for k, (ok, tot) in self.stats.items():
            out[k] = "ok" if ok == tot else f"{ok}/{tot} ok" + (f" ({self.last_err[k]})" if k in self.last_err else "")
        return out

    def core_ok(self) -> bool:
        """Minstens een kern-endpoint gaf deze run data terug."""
        return any(self.stats.get(k, [0, 0])[0] > 0 for k in CORE_LABELS)


def merge(day: dict, values: dict) -> None:
    """Vul aan zonder bestaande (meer specifieke) waarden met None te overschrijven."""
    for k, v in values.items():
        if v is None:
            continue
        if isinstance(v, float):
            v = round(v, 2)
        day[k] = v


def fetch_all(client: Any, cfg: dict, cache: dict) -> dict:
    today = date.today()
    start = today - timedelta(days=cfg["days"] - 1)
    f = Fetcher(client, cfg)
    days: dict[str, dict] = {d: dict(v) for d, v in cache.get("days", {}).items() if d >= iso(start)}

    # Bereik-endpoints: wekelijks volledig, anders enkel de recente periode
    last_full = cache.get("lastFull")
    full = not last_full or (today - date.fromisoformat(last_full)).days >= cfg["full_every_days"]
    rstart = start if full else max(start, today - timedelta(days=cfg["recent_days"] - 1))

    def put(pairs: list[tuple[str, dict]], normaliser: Callable[[dict], dict]) -> None:
        for d, item in pairs:
            if d >= iso(start):
                merge(days.setdefault(d, {}), normaliser(item))

    put(f.ranged("dailySummaries", "get_daily_summaries", rstart, today, "data"), norm_daily_summary)
    put(f.ranged("steps", "get_daily_steps", rstart, today), norm_steps)
    put(f.ranged("stress", "get_daily_stress", rstart, today), norm_stress)
    put(f.ranged("bodyBattery", "get_daily_body_battery", rstart, today), norm_body_battery)
    put(f.ranged("hrv", "get_daily_hrv_summaries", rstart, today, "hrvSummaries"), norm_hrv)
    put(f.ranged("sleepSummaries", "get_daily_sleep_summaries", rstart, today), norm_sleep)

    # Per-dag-endpoints: enkel ontbrekende dagen + de laatste N dagen (incrementeel)
    recent = {iso(today - timedelta(days=i)) for i in range(cfg["per_day_refetch"])}
    all_days = [iso(start + timedelta(days=i)) for i in range((today - start).days + 1)]
    budget = cfg["sleep_budget"]
    for d in reversed(all_days):
        rec = days.setdefault(d, {})
        tried = rec.get("_sleepTried")
        retry_ok = not tried or (today - date.fromisoformat(tried)).days >= 30
        if (d in recent or "deepSec" not in rec) and (d in recent or retry_ok) and budget > 0:
            budget -= 1
            res = f.call("sleepDaily", lambda d=d: client.get_daily_sleep(d))
            if res is FAIL:
                continue  # niet als 'geprobeerd' markeren: volgende run opnieuw
            merge(rec, norm_sleep(res or {}))
            if "deepSec" not in rec and d not in recent:
                rec["_sleepTried"] = iso(today)  # geen slaapdata (horloge niet gedragen): na 30 d opnieuw
    for d in all_days[-14:]:
        rec = days[d]
        if d in recent or "spo2Avg" not in rec:
            merge(rec, norm_spo2(f.get("spo2", lambda d=d: client.get_daily_spo2(d)) or {}))
        if d in recent or "rhr" not in rec:
            merge(rec, norm_heart_rate(f.get("heartRate", lambda d=d: client.get_daily_heart_rate(d)) or {}))

    for rec in days.values():  # slaap-fallbacks
        if "rhr" not in rec and "sleepRhr" in rec:
            rec["rhr"] = rec["sleepRhr"]
        if "hrv" not in rec and "sleepHrv" in rec:
            rec["hrv"] = rec["sleepHrv"]

    act_start = today - timedelta(days=cfg["activity_days"])
    raw_acts, acts_ok = f.activities(act_start, today)
    acts = [a for a in (norm_activity(x) for x in raw_acts) if a]
    if not acts_ok and cache.get("activities"):
        acts = cache["activities"]  # API tijdelijk stuk: hou vorige lijst
    acts.sort(key=lambda a: a["start"], reverse=True)

    fitness = f.get("fitnessAge", lambda: client.get_fitness_age(today)) or {}
    profile = f.get("profile", client.get_user_profile_settings) or {}

    if full and f.core_ok():
        cache["lastFull"] = iso(today)
    return {
        "days": days,
        "activities": acts,
        "fitnessAge": {
            "fitnessAge": round_or_none(fnum(fitness, "fitnessAge"), 1),
            "chronologicalAge": round_or_none(fnum(fitness, "chronologicalAge")),
            "achievable": round_or_none(fnum(fitness, "achievableFitnessAge"), 1),
        },
        "profile": {
            "vo2Run": fnum(profile, "vo2MaxRunning"),
            "vo2Bike": fnum(profile, "vo2MaxCycling"),
            "lactateHr": fnum(profile, "lactateThresholdHeartRate"),
        },
        "coverage": f.coverage(),
        "coreOk": f.core_ok(),
        "lastFull": cache.get("lastFull"),
    }


# --------------------------------------------------------------------------- payload

def build_payload(data: dict, demo: bool = False) -> dict:
    days = [{"d": d, **{k: v for k, v in rec.items() if not k.startswith("_")}}
            for d, rec in sorted(data["days"].items())]
    core = ("steps", "sleepSec", "hrv", "rhr", "bbHigh")
    through = max((d["d"] for d in days if any(k in d for k in core)), default=None)
    return {
        "schema": SCHEMA_VERSION,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataThrough": through,
        "demo": demo,
        "days": days,
        "activities": data["activities"],
        "fitnessAge": {k: v for k, v in data.get("fitnessAge", {}).items() if v is not None},
        "profile": {k: v for k, v in data.get("profile", {}).items() if v is not None},
        "coverage": data.get("coverage", {}),
    }


def content_hash(payload: dict) -> str:
    """Hash zonder tijdstempels: bepaalt of er iets nieuws te publiceren valt."""
    p = {k: v for k, v in payload.items() if k not in ("generated", "coverage")}
    return hashlib.sha256(json.dumps(p, sort_keys=True, allow_nan=False).encode()).hexdigest()


def get_salt(create: bool = True) -> bytes:
    """Vaste salt per wachtwoord, zodat de gsm een afgeleide sleutel kan onthouden i.p.v. het wachtwoord."""
    import keyring

    s = keyring.get_password(KEYRING_SERVICE, "salt")
    if not s and create:
        s = base64.b64encode(secrets.token_bytes(16)).decode()
        keyring.set_password(KEYRING_SERVICE, "salt", s)
    return base64.b64decode(s) if s else secrets.token_bytes(16)


def _kdf(passphrase: str, salt: bytes, iterations: int) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                      iterations=iterations).derive(nfc(passphrase).encode("utf-8"))


def encrypt(payload: dict, passphrase: str, salt: bytes) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)  # altijd nieuw per versleuteling
    plain = gzip.compress(json.dumps(payload, separators=(",", ":"), ensure_ascii=False,
                                     allow_nan=False).encode("utf-8"), mtime=0)
    ct = AESGCM(_kdf(passphrase, salt, PBKDF2_ITERATIONS)).encrypt(nonce, plain, AAD.encode())
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return {"v": 2, "kdf": "PBKDF2-SHA256", "iter": PBKDF2_ITERATIONS, "zip": "gzip",
            "salt": b64(salt), "iv": b64(nonce), "aad": AAD, "ct": b64(ct)}


def decrypt(blob: dict, passphrase: str) -> dict:
    """Zelftest: controleert dat de versleuteling terug te lezen is."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    d = base64.b64decode
    key = _kdf(passphrase, d(blob["salt"]), blob["iter"])
    plain = AESGCM(key).decrypt(d(blob["iv"]), d(blob["ct"]), blob["aad"].encode())
    return json.loads(gzip.decompress(plain) if blob.get("zip") == "gzip" else plain)


# --------------------------------------------------------------------------- lokaal dashboard

def write_local_dashboard(blob: dict) -> Path:
    """Eén HTML-bestand met de versleutelde data erin: dubbelklikken en ontgrendelen."""
    html = (SITE_DIR / "index.html").read_text(encoding="utf-8")
    data = json.dumps(blob).replace("</", "<\\/")
    marker = '<script>\n"use strict";'
    if marker not in html:
        raise RuntimeError("index.html heeft een onverwachte structuur")
    html = html.replace(marker, f"<script>window.EMBEDDED_BLOB={data};</script>\n{marker}", 1)
    tmp = LOCAL_HTML.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    os.replace(tmp, LOCAL_HTML)
    return LOCAL_HTML


# --------------------------------------------------------------------------- publiceren (GitHub Git Data API)

def publish(cfg: dict, blob: dict) -> None:
    """Publiceert site + data als EEN commit zonder voorgangers (force-push).

    Zo bevat de publieke repo nooit oude versleutelde versies en groeit hij niet.
    """
    import keyring
    import requests

    token = keyring.get_password(KEYRING_SERVICE, "github_token")
    if not token or not cfg.get("github_owner"):
        raise RuntimeError("GitHub niet ingesteld. Draai eerst: python garmin_sync.py --setup")
    api = f"https://api.github.com/repos/{cfg['github_owner']}/{cfg['github_repo']}"
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "garmin-dashboard-sync"}

    def req(method: str, url: str, **kw: Any) -> Any:
        r = requests.request(method, url, headers=h, timeout=60, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"GitHub {method} {url.rsplit('/', 2)[-2:]} -> HTTP {r.status_code}")
        return r.json() if r.content else None

    files: dict[str, bytes] = {n: (SITE_DIR / n).read_bytes() for n in SITE_FILES if (SITE_DIR / n).exists()}
    files["data.enc.json"] = json.dumps(blob).encode()
    files[".nojekyll"] = b""
    tree = []
    for name, content in files.items():
        b = req("POST", f"{api}/git/blobs", json={"content": base64.b64encode(content).decode(), "encoding": "base64"})
        tree.append({"path": name, "mode": "100644", "type": "blob", "sha": b["sha"]})
    t = req("POST", f"{api}/git/trees", json={"tree": tree})
    c = req("POST", f"{api}/git/commits", json={"message": "update", "tree": t["sha"], "parents": []})
    ref = f"{api}/git/refs/heads/{cfg['github_branch']}"
    r = requests.get(ref, headers=h, timeout=30)
    if r.status_code == 200:
        req("PATCH", ref, json={"sha": c["sha"], "force": True})
    else:
        req("POST", f"{api}/git/refs", json={"ref": f"refs/heads/{cfg['github_branch']}", "sha": c["sha"]})
    log.info("Gepubliceerd naar https://%s.github.io/%s/", cfg["github_owner"], cfg["github_repo"])


# --------------------------------------------------------------------------- schema-dump

def shape(v: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "..."
    if isinstance(v, dict):
        return {k: shape(x, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [shape(v[0], depth + 1), f"({len(v)} items)"] if v else []
    return type(v).__name__


def probe_training(client: Any) -> dict:
    """Test of trainingsplan/kalender/racevoorspelling via de Personal API bereikbaar zijn.

    Geeft enkel structuur en HTTP-status terug, nooit waarden.
    """
    out: dict[str, Any] = {}

    def tryget(name: str, fn: Callable[[], Any]) -> Any:
        try:
            res = fn()
            out[name] = shape(res)
            return res
        except Exception as exc:  # noqa: BLE001
            out[name] = f"FOUT: {getattr(exc, 'status_code', '')} {type(exc).__name__}"
            return None

    t = date.today()
    acts = tryget("recentActivities", lambda: client.search_activities(limit=40)) or []
    acts = [a for a in as_list(acts, "activityList") if isinstance(a, dict)]
    runs = [a for a in acts if "run" in str(find(a, "typeKey") or "")]
    out["info"] = {"aantalRuns": len(runs),
                   "runsMetWorkoutVeld": sum(1 for a in runs if find(a, "workoutId", "associatedWorkoutId",
                                                                       "workoutUuid", depth=1))}
    if runs:
        rid = find(runs[0], "activityId")
        tryget("run.activity", lambda: client.get_activity(rid))
        tryget("run.workouts", lambda: client.get_activity_workouts(rid))
        planned = [a for a in runs if find(a, "workoutId", "associatedWorkoutId", depth=1)]
        if planned:
            pid = find(planned[0], "activityId")
            tryget("planRun.workouts", lambda: client.get_activity_workouts(pid))
    plans = tryget("trainingPlans", lambda: client.get("/trainingplan-service/trainingplan/plans"))
    plan_id = find(plans, "trainingPlanId", "planId") if plans else None
    if plan_id:
        tryget("plan.phased", lambda: client.get(f"/trainingplan-service/trainingplan/phased/{plan_id}"))
        tryget("plan.adaptive", lambda: client.get(f"/trainingplan-service/trainingplan/fbt-adaptive/{plan_id}"))
    tryget("calendar.m0", lambda: client.get(f"/calendar-service/year/{t.year}/month/{t.month - 1}"))
    tryget("calendar.m1", lambda: client.get(f"/calendar-service/year/{t.year}/month/{t.month}"))
    tryget("workouts", lambda: client.get("/workout-service/workouts", params={"start": 0, "limit": 5}))
    tryget("trainingStatus", lambda: client.get(f"/metrics-service/metrics/trainingstatus/aggregated/{iso(t)}"))
    social = None
    try:
        social = client.get("/userprofile-service/socialProfile")
        out["socialProfile"] = "ok"
    except Exception as exc:  # noqa: BLE001
        out["socialProfile"] = f"FOUT: {getattr(exc, 'status_code', '')}"
    name = find(social, "displayName") if social else None
    if name:
        tryget("racePredictions", lambda: client.get(f"/metrics-service/metrics/racepredictions/latest/{name}"))
    return out


def dump_schema(client: Any) -> Path:
    t = date.today()
    y = t - timedelta(days=1)
    w = t - timedelta(days=6)
    calls = {
        "dailySummaries": lambda: client.get_daily_summaries(w, t),
        "steps": lambda: client.get_daily_steps(w, t),
        "stress": lambda: client.get_daily_stress(w, t),
        "bodyBattery": lambda: client.get_daily_body_battery(y, t),
        "hrvSummaries": lambda: client.get_daily_hrv_summaries(w, t),
        "sleepSummaries": lambda: client.get_daily_sleep_summaries(w, t),
        "sleepDaily": lambda: client.get_daily_sleep(y),
        "heartRate": lambda: client.get_daily_heart_rate(y),
        "spo2": lambda: client.get_daily_spo2(y),
        "fitnessAge": lambda: client.get_fitness_age(t),
        "activities": lambda: client.search_activities(limit=3),
        "profile": client.get_user_profile_settings,
    }
    out = {}
    for name, fn in calls.items():
        try:
            out[name] = shape(fn())
        except Exception as exc:  # noqa: BLE001
            out[name] = f"FOUT: {getattr(exc, 'status_code', '')} {type(exc).__name__}"
    out["probes"] = probe_training(client)
    path = APP_DIR / "schema.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- demo

def demo_data(cfg: dict) -> dict:
    rnd = random.Random(42)
    today = date.today()
    days = {}
    hrv_base, rhr_base = 58.0, 52.0
    for i in range(cfg["days"]):
        d = today - timedelta(days=cfg["days"] - 1 - i)
        wk = d.weekday() >= 5
        sleep = rnd.gauss(7.1 if not wk else 7.6, 0.7) * 3600
        out = d.weekday() in (5, 6) and rnd.random() < 0.3  # avond uit (vrij/zat)
        if out:
            sleep -= 2.5 * 3600
        hrv = hrv_base + rnd.gauss(0, 6) - (8 if sleep < 6 * 3600 else 0) + i * 0.03
        deep = sleep * rnd.uniform(0.14, 0.22)
        rem = sleep * rnd.uniform(0.18, 0.25)
        bed_min = int(rnd.gauss(23.5 * 60 + (60 if wk else 0), 35)) % (24 * 60)
        if out:
            bed_min = int(rnd.gauss(2.6 * 60, 30))
            hrv -= 9
        steps = int(max(1500, rnd.gauss(9500 if not wk else 7500, 2800)))
        days[iso(d)] = {
            "steps": steps, "stepGoal": 8500, "distKm": round(steps * 0.00074, 2),
            "rhr": round(rhr_base + rnd.gauss(0, 1.8) + (3 if sleep < 6 * 3600 else 0) - i * 0.01),
            "kcal": int(rnd.gauss(2550, 300)), "activeKcal": int(rnd.gauss(600, 250)),
            "intensityMin": max(0, int(rnd.gauss(28, 25))), "floors": max(0, int(rnd.gauss(9, 5))),
            "stressAvg": int(max(12, rnd.gauss(30 if not wk else 24, 7))),
            "stressHighMin": max(0, int(rnd.gauss(35, 20))), "stressRestMin": int(rnd.gauss(560, 60)),
            "bbHigh": int(min(100, max(30, rnd.gauss(82, 10) - (20 if sleep < 6 * 3600 else 0)))),
            "bbLow": int(max(5, rnd.gauss(22, 8))), "bbCharged": int(rnd.gauss(60, 12)),
            "bbDrained": int(rnd.gauss(62, 12)),
            "hrv": round(hrv), "hrvWeekly": round(hrv_base + i * 0.03), "hrvLow": 50, "hrvHigh": 68,
            "hrvStatus": "BALANCED" if hrv > 50 else "UNBALANCED",
            "sleepSec": int(sleep), "deepSec": int(deep), "remSec": int(rem),
            "awakeSec": int(rnd.uniform(600, 2400)), "lightSec": int(sleep - deep - rem),
            "sleepScore": int(min(98, max(35, 40 + (sleep / 3600 - 5) * 14 + rnd.gauss(0, 6)))),
            "bedtime": f"{bed_min // 60:02d}:{bed_min % 60:02d}",
            "wake": f"{(bed_min + int(sleep / 60)) // 60 % 24:02d}:{(bed_min + int(sleep / 60)) % 60:02d}",
            "spo2Avg": round(rnd.gauss(95.5, 1), 1), "spo2Low": int(rnd.gauss(89, 2)),
            "respiration": round(rnd.gauss(14.5, 0.8), 1),
        }
    acts = []
    types = [("tennis", "Tennis", 0.30), ("bouldering", "Boulderen", 0.30), ("running", "Lopen", 0.25),
             ("strength_training", "Kracht", 0.15)]
    aid = 1
    for i in range(cfg["activity_days"]):
        d = today - timedelta(days=i)
        if rnd.random() < 0.55:
            r, acc = rnd.random(), 0
            for key, name, p in types:
                acc += p
                if r <= acc:
                    break
            dur = rnd.gauss({"tennis": 85, "bouldering": 110, "running": 42, "strength_training": 55}[key], 12)
            a = {"id": aid, "name": name, "type": key,
                 "start": f"{iso(d)}T{rnd.choice(['07:10', '12:15', '18:30', '19:45', '20:00'])}",
                 "durMin": round(dur, 1), "distKm": round(dur / 5.6, 2) if key == "running" else None,
                 "kcal": int(dur * rnd.uniform(7, 11)), "avgHr": int(rnd.gauss(138, 10)),
                 "maxHr": int(rnd.gauss(172, 8)), "load": round(rnd.gauss(95, 35), 1),
                 "aeTE": round(min(5, max(1, rnd.gauss(2.9, 0.6))), 1),
                 "anTE": round(min(5, max(0, rnd.gauss(1.4, 0.7))), 1),
                 "vo2": round(49 + (cfg["activity_days"] - i) * 0.006 + rnd.gauss(0, 0.3), 1) if key == "running" else None}
            acts.append({k: v for k, v in a.items() if v is not None})
            aid += 1
        # marathonblok: lange duurloop op zondag + 2 plan-runs in de week (laatste 16 weken)
        if i < 112 and d.weekday() in (1, 3, 6):
            wk = (112 - i) / 112
            if d.weekday() == 6:
                km, pace, lbl = 14 + 16 * wk + rnd.gauss(0, 1.2), 6.25 - 0.25 * wk, "BASE"
            elif d.weekday() == 1:
                km, pace, lbl = 8 + 3 * wk, 5.15 - 0.2 * wk + rnd.gauss(0, 0.08), rnd.choice(["THRESHOLD", "TEMPO", "VO2MAX"])
            else:
                km, pace, lbl = 7 + 3 * wk, 6.1 - 0.15 * wk + rnd.gauss(0, 0.1), "RECOVERY" if rnd.random() < .3 else "BASE"
            hr = {"BASE": 142, "RECOVERY": 132, "TEMPO": 158, "THRESHOLD": 164, "VO2MAX": 168}[lbl] - 4 * wk
            acts.append({"id": aid, "name": "Marathon: " + {"BASE": "Duurloop", "RECOVERY": "Herstelloop",
                         "TEMPO": "Tempo", "THRESHOLD": "Drempel", "VO2MAX": "Intervallen"}[lbl] if d.weekday() != 6 else "Marathon: Lange duurloop",
                         "type": "running", "start": f"{iso(d)}T{'09:30' if d.weekday() == 6 else '19:00'}",
                         "durMin": round(km * pace, 1), "distKm": round(km, 2), "kcal": int(km * 70),
                         "avgHr": int(hr + rnd.gauss(0, 3)), "maxHr": int(hr + 18), "load": round(km * 9, 1),
                         "aeTE": 3.0, "anTE": 1.0, "teLabel": lbl, "plan": True,
                         "vo2": round(49 + (cfg["activity_days"] - i) * 0.006 + rnd.gauss(0, 0.3), 1)})
            aid += 1
    acts.sort(key=lambda a: a["start"], reverse=True)
    return {"days": days, "activities": acts,
            "fitnessAge": {"fitnessAge": 24.5, "chronologicalAge": 29, "achievable": 22.0},
            "profile": {"vo2Run": 51.0}, "coverage": {"demo": "ok"}, "coreOk": True}


# --------------------------------------------------------------------------- setup

def setup() -> None:
    import keyring

    APP_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    print("== Garmin-dashboard setup ==\n")
    huidig = "ja" if cfg["github_owner"] else "nee"
    ans = input(f"Online publiceren via GitHub Pages (voor je gsm)? j/n [{huidig}]: ").strip().lower()
    online = (ans or huidig[0]).startswith(("j", "y"))
    if online:
        owner = input(f"GitHub-gebruikersnaam [{cfg['github_owner']}]: ").strip() or cfg["github_owner"]
        repo = input(f"Repo-naam [{cfg['github_repo']}]: ").strip() or cfg["github_repo"]
        cfg.update(github_owner=owner, github_repo=repo)
        tok = getpass.getpass("GitHub fine-grained token (Contents: read & write, enkel deze repo) "
                              "[Enter = ongewijzigd]: ").strip()
        if tok:
            keyring.set_password(KEYRING_SERVICE, "github_token", tok)
    else:
        cfg["github_owner"] = ""
        print(f"Alleen lokaal: het dashboard komt in {LOCAL_HTML}")
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    print("\nDashboardwachtwoord: hiermee ontgrendel je het dashboard.")
    print("Tip: 4-5 losse woorden, bv. 'kiezel-tango-wolk-boulder-rood'. Minstens 16 tekens.")
    while True:
        pw = getpass.getpass("Wachtwoord [Enter = ongewijzigd]: ")
        if not pw:
            break
        if len(pw) < 16:
            print("Te kort (min. 16 tekens).")
            continue
        if getpass.getpass("Herhaal: ") != pw:
            print("Komt niet overeen.")
            continue
        keyring.set_password(KEYRING_SERVICE, "passphrase", nfc(pw))
        # Nieuwe salt: sleutels die op toestellen onthouden zijn, werken niet meer
        keyring.set_password(KEYRING_SERVICE, "salt", base64.b64encode(secrets.token_bytes(16)).decode())
        break
    key_file = Path("~/.config/garmin/connect-personal-api-key").expanduser()
    print(f"\nGarmin-sleutel: {'gevonden' if key_file.exists() else 'NIET gevonden'} op {key_file}")
    print(f"Config: {CONFIG_FILE}\nKlaar. Test met: python garmin_sync.py --local")


# --------------------------------------------------------------------------- main

def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    cache = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
    if args.demo:
        data = demo_data(cfg)
    else:
        from garmin_personal_api import (GarminPersonalApiClient, HttpError,
                                         PersonalApiAuthenticator, resolve_api_key)
        try:
            with PersonalApiAuthenticator() as auth:
                token = auth.obtain_token(resolve_api_key())
        except Exception as exc:  # noqa: BLE001
            log.error("Garmin-token ophalen mislukt (sleutel verlopen of ontbrekend?): %s %s",
                      type(exc).__name__, getattr(exc, "status_code", ""))
            return 2
        with GarminPersonalApiClient(token.access_token) as client:
            if args.schema:
                log.info("Schema (zonder waarden) geschreven naar %s", dump_schema(client))
                return 0
            try:
                data = fetch_all(client, cfg, cache)
            except HttpError as exc:
                if exc.status_code != 401:
                    raise
                with PersonalApiAuthenticator() as auth:
                    token = auth.obtain_token(resolve_api_key(), force_refresh=True)
                client.update_bearer_token(token.access_token)
                data = fetch_all(client, cfg, cache)
        if not data["coreOk"]:
            log.error("Geen enkel kern-endpoint gaf data terug; niets gepubliceerd. %s", data["coverage"])
            return 4
        CACHE_FILE.write_text(json.dumps({"days": data["days"], "activities": data["activities"],
                                          "lastFull": data.get("lastFull"),
                                          "lastHash": cache.get("lastHash"),
                                          "lastPublish": cache.get("lastPublish")}), encoding="utf-8")

    payload = build_payload(data, demo=args.demo)
    n_days = sum(1 for d in payload["days"] if len(d) > 1)
    log.info("%d dagen met data, %d activiteiten. Endpoints: %s", n_days, len(payload["activities"]),
             ", ".join(f"{k}={v}" for k, v in payload["coverage"].items()))

    import keyring
    pw = os.environ.get("GARMIN_DASH_PASSPHRASE") or keyring.get_password(KEYRING_SERVICE, "passphrase")
    if not pw:
        log.error("Geen dashboardwachtwoord. Draai: python garmin_sync.py --setup")
        return 3
    salt = base64.b64decode(os.environ["GARMIN_DASH_SALT"]) if os.environ.get("GARMIN_DASH_SALT") else get_salt()
    blob = encrypt(payload, pw, salt)
    if decrypt(blob, pw)["generated"] != payload["generated"]:
        raise RuntimeError("Zelftest versleuteling faalde")
    (OUT_DIR / "data.enc.json").write_text(json.dumps(blob), encoding="utf-8")
    log.info("Versleuteld bestand: %s (%d kB)", OUT_DIR / "data.enc.json", (OUT_DIR / "data.enc.json").stat().st_size // 1024)
    log.info("Lokaal dashboard: %s", write_local_dashboard(blob))

    if not cfg.get("github_owner"):
        return 0  # geen hosting ingesteld: enkel lokaal
    if not args.local and not args.demo:
        h = content_hash(payload) + base64.b64encode(salt).decode()
        site_h = hashlib.sha256(b"".join((SITE_DIR / n).read_bytes() for n in SITE_FILES if (SITE_DIR / n).exists())).hexdigest()
        stamp_age = (datetime.now() - datetime.fromisoformat(cache.get("lastPublish") or "2000-01-01T00:00:00")).total_seconds()
        # Publiceer bij nieuwe data, gewijzigde site, of minstens elke 12 u (zodat 'laatste sync' klopt)
        if h + site_h != cache.get("lastHash") or stamp_age > 12 * 3600:
            publish(cfg, blob)
            cache_now = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            cache_now.update(lastHash=h + site_h, lastPublish=datetime.now().isoformat(timespec="seconds"))
            CACHE_FILE.write_text(json.dumps(cache_now), encoding="utf-8")
        else:
            log.info("Niets nieuws: niet gepubliceerd.")
    elif args.demo and not args.local:
        publish(cfg, blob)
    return 0


# --------------------------------------------------------------------------- CI (GitHub Actions)

def garmin_session(cfg: dict, cache: dict, schema: bool = False) -> dict | None:
    """Haalt token op en draait fetch_all (of de schema-dump). Geeft None bij schema-modus."""
    from garmin_personal_api import (GarminPersonalApiClient, HttpError,
                                     PersonalApiAuthenticator, resolve_api_key)
    with PersonalApiAuthenticator() as auth:
        token = auth.obtain_token(resolve_api_key())
    with GarminPersonalApiClient(token.access_token) as client:
        if schema:
            p = dump_schema(client)
            print(p.read_text(encoding="utf-8"))  # enkel structuur, geen waarden
            return None
        try:
            return fetch_all(client, cfg, cache)
        except HttpError as exc:
            if exc.status_code != 401:
                raise
            with PersonalApiAuthenticator() as auth:
                token = auth.obtain_token(resolve_api_key(), force_refresh=True)
            client.update_bearer_token(token.access_token)
            return fetch_all(client, cfg, cache)


def run_ci(args: argparse.Namespace) -> int:
    """Draait op GitHub Actions: geen keyring, geen lokale bestanden.

    De vorige stand (versleutelde data + versleutelde cache) komt uit --prev-dir
    (de gh-pages-branch); de nieuwe site komt in --out-dir.
    Geheimen komen uit de omgeving: GARMIN_PERSONAL_API_KEY en GARMIN_DASH_PASSPHRASE.
    """
    import shutil

    APP_DIR.mkdir(parents=True, exist_ok=True)
    pw = nfc(os.environ.get("GARMIN_DASH_PASSPHRASE", ""))
    if len(pw) < 16:
        log.error("Secret GARMIN_DASH_PASSPHRASE ontbreekt of is korter dan 16 tekens.")
        return 3
    if not os.environ.get("GARMIN_PERSONAL_API_KEY") and not args.demo:
        log.error("Secret GARMIN_PERSONAL_API_KEY ontbreekt.")
        return 2
    cfg = load_config()
    prev = Path(args.prev_dir) if args.prev_dir else None
    out = Path(args.out_dir)

    # Vorige salt + cache hergebruiken als het wachtwoord nog klopt
    salt, cache = secrets.token_bytes(16), {}
    if prev and (prev / "data.enc.json").exists():
        try:
            old = json.loads((prev / "data.enc.json").read_text(encoding="utf-8"))
            decrypt(old, pw)
            salt = base64.b64decode(old["salt"])
            if (prev / "cache.enc.json").exists():
                cache = decrypt(json.loads((prev / "cache.enc.json").read_text(encoding="utf-8")), pw)
            log.info("Vorige stand ingelezen.")
        except Exception:  # noqa: BLE001
            log.warning("Vorige stand niet te ontsleutelen (wachtwoord gewijzigd?): opnieuw beginnen.")
            salt, cache = secrets.token_bytes(16), {}

    if args.demo:
        data = demo_data(cfg)
    else:
        try:
            data = garmin_session(cfg, cache, schema=args.schema)
        except Exception as exc:  # noqa: BLE001
            log.error("Garmin-aanroep mislukt: %s %s (sleutel verlopen of fout?)",
                      type(exc).__name__, getattr(exc, "status_code", ""))
            return 2
        if data is None:
            return 0
        if not data["coreOk"]:
            log.error("Geen enkel kern-endpoint gaf data terug; niets gepubliceerd. %s", data["coverage"])
            return 4

    payload = build_payload(data, demo=args.demo)
    # Publieke Actions-log: geen aantallen of andere afgeleide info over je data, enkel de status per bron
    log.info("Bronnen: %s", ", ".join(f"{k}={v}" for k, v in payload["coverage"].items()))
    blob = encrypt(payload, pw, salt)
    if decrypt(blob, pw)["generated"] != payload["generated"]:
        raise RuntimeError("Zelftest versleuteling faalde")
    new_cache = {"days": data["days"], "activities": data["activities"], "lastFull": data.get("lastFull")}

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for n in SITE_FILES:
        if (SITE_DIR / n).exists():
            shutil.copy2(SITE_DIR / n, out / n)
    (out / "data.enc.json").write_text(json.dumps(blob), encoding="utf-8")
    (out / "cache.enc.json").write_text(json.dumps(encrypt(new_cache, pw, salt)), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    log.info("Site klaargezet.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--local", action="store_true", help="niet publiceren")
    ap.add_argument("--schema", action="store_true", help="responsstructuur (zonder waarden) dumpen")
    ap.add_argument("--demo", action="store_true", help="demodata i.p.v. Garmin")
    ap.add_argument("--setup", action="store_true", help="GitHub + wachtwoord instellen")
    ap.add_argument("--ci", action="store_true", help="GitHub Actions-modus (geheimen uit omgeving)")
    ap.add_argument("--prev-dir", help="CI: map met de vorige gh-pages-inhoud")
    ap.add_argument("--out-dir", default="public", help="CI: map waarin de nieuwe site komt")
    args = ap.parse_args()

    if args.ci:  # enkel naar stdout (publieke Actions-log: geen gezondheidswaarden loggen)
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
        logging.getLogger("garmin_personal_api").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        try:
            return run_ci(args)
        except Exception as exc:  # noqa: BLE001 - geen traceback met mogelijke responsinhoud
            log.error("Sync mislukt: %s", type(exc).__name__)
            return 1

    APP_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=512_000, backupCount=2, encoding="utf-8")]
    if sys.stdout is not None:  # pythonw.exe heeft geen console
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    logging.getLogger("garmin_personal_api").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.captureWarnings(True)
    if args.setup:
        setup()
        return 0
    try:
        return run(args)
    except Exception:  # noqa: BLE001 - alles in sync.log, ook onder pythonw
        log.exception("Sync mislukt")
        return 1


if __name__ == "__main__":
    sys.exit(main())
