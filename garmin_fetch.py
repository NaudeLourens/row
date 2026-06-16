#!/usr/bin/env python3
"""
garmin_fetch.py
---------------
Logs into Garmin Connect with YOUR account and writes the metrics your
dashboard needs into ./garmin_data.json.

This is the Garmin equivalent of WHOOP's OAuth flow. Garmin has no consumer
OAuth for individuals, so instead we authenticate the same way the official
Garmin Connect app does, using the `python-garminconnect` library.

Setup (run once):
    python3 -m venv .venv
    source .venv/bin/activate          # Windows: .venv\\Scripts\\activate
    pip install garminconnect

Credentials: set environment variables (never hard-code them in a file you
might commit to GitHub):
    export GARMIN_EMAIL="you@example.com"
    export GARMIN_PASSWORD="your-password"

Then:
    python3 garmin_fetch.py

The first run will ask for an MFA code if your account has 2FA on. After that,
tokens are cached in ~/.garminconnect so it logs in silently (good for cron).

NOTE: The exact field names inside Garmin's JSON responses occasionally shift
between library releases. Every metric below is wrapped in a try/except and
digs defensively. If a value comes back as None, run `python3 -c "import
garminconnect; help(garminconnect.Garmin)"` or the library's demo.py to see the
current method names, and adjust the extract_* functions. This is exactly the
kind of refinement Claude Code is good at — paste a raw response and ask it to
fix the extractor.
"""

import os
import json
import datetime
from pathlib import Path

try:
    import garminconnect
except ImportError:
    raise SystemExit(
        "Missing dependency. Run:  pip install garminconnect"
    )

TOKENSTORE = os.path.expanduser("~/.garminconnect")
OUTPUT = Path(__file__).with_name("garmin_data.json")
TODAY = datetime.date.today().isoformat()


# --------------------------------------------------------------------------
# Login (token reuse first, fall back to email/password + optional MFA)
# --------------------------------------------------------------------------
def login():
    try:
        api = garminconnect.Garmin()
        api.login(TOKENSTORE)
        print("Logged in with cached tokens.")
        return api
    except (FileNotFoundError, garminconnect.GarminConnectAuthenticationError, Exception):
        pass  # fall through to a fresh login

    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        raise SystemExit(
            "Set GARMIN_EMAIL and GARMIN_PASSWORD environment variables, "
            "then run again."
        )

    api = garminconnect.Garmin(email=email, password=password, return_on_mfa=True)
    result1, result2 = api.login()
    if result1 == "needs_mfa":
        mfa = input("Enter the MFA code Garmin just sent you: ").strip()
        api.resume_login(result2, mfa)

    # cache tokens so future runs are silent
    try:
        api.client.dump(TOKENSTORE)
        print(f"Logged in and cached tokens to {TOKENSTORE}.")
    except Exception as e:
        print(f"  Warning: could not cache tokens ({e}) — next run may prompt again.")
    return api


# --------------------------------------------------------------------------
# Small helper: never let one bad endpoint crash the whole fetch
# --------------------------------------------------------------------------
def safe(label, fn):
    try:
        return fn()
    except Exception as e:
        print(f"  ! {label} failed: {e}")
        return None


def dig(obj, *path, default=None):
    """Walk nested dicts/lists safely: dig(d, 'a', 'b', 0)."""
    cur = obj
    for key in path:
        if cur is None:
            return default
        try:
            cur = cur[key]
        except (KeyError, IndexError, TypeError):
            return default
    return cur if cur is not None else default


# --------------------------------------------------------------------------
# Extractors — map Garmin's raw responses to the dashboard's metric model.
# Garmin metric  ->  WHOOP analogue used by the dashboard card:
#   Training Readiness  ->  Recovery ring
#   Sleep score+stages  ->  Sleep + stages bar
#   Body Battery        ->  (strain slot — closest daily-energy analogue)
#   HRV last-night avg  ->  HRV
#   Resting HR          ->  RHR
#   Stress / SpO2 / Respiration -> biomarker strip
# --------------------------------------------------------------------------
def build_payload(api):
    data = {"updated": datetime.datetime.now().isoformat(timespec="seconds")}

    readiness = safe("training_readiness", lambda: api.get_training_readiness(TODAY))
    if isinstance(readiness, list) and readiness:
        readiness = readiness[0]
    data["readiness"] = {
        "score": dig(readiness, "score"),
        "level": dig(readiness, "level"),
    }

    sleep = safe("sleep", lambda: api.get_sleep_data(TODAY))
    daily = dig(sleep, "dailySleepDTO", default={})
    data["sleep"] = {
        "score": dig(daily, "sleepScores", "overall", "value"),
        "totalSec": dig(daily, "sleepTimeSeconds"),
        "stages": {
            "deep": dig(daily, "deepSleepSeconds"),
            "light": dig(daily, "lightSleepSeconds"),
            "rem": dig(daily, "remSleepSeconds"),
            "awake": dig(daily, "awakeSleepSeconds"),
        },
    }

    bb = safe("body_battery", lambda: api.get_body_battery(TODAY, TODAY))
    # body battery returns a list of day objects, each with a values array
    bb_levels = dig(bb, 0, "bodyBatteryValuesArray", default=[])
    bb_nums = [v[1] for v in bb_levels if isinstance(v, list) and len(v) > 1 and v[1] is not None]
    data["bodyBattery"] = {
        "current": bb_nums[-1] if bb_nums else None,
        "high": max(bb_nums) if bb_nums else None,
        "low": min(bb_nums) if bb_nums else None,
    }

    hrv = safe("hrv", lambda: api.get_hrv_data(TODAY))
    data["hrv"] = {
        "lastNightAvg": dig(hrv, "hrvSummary", "lastNightAvg"),
        "status": dig(hrv, "hrvSummary", "status"),
    }

    rhr = safe("rhr", lambda: api.get_rhr_day(TODAY))
    data["rhr"] = (
        dig(rhr, "allMetrics", "metricsMap", "WELLNESS_RESTING_HEART_RATE", 0, "value")
        or dig(rhr, "restingHeartRate")
    )

    stats = safe("user_summary", lambda: api.get_user_summary(TODAY))
    data["steps"] = dig(stats, "totalSteps")
    data["stress"] = {"avg": dig(stats, "averageStressLevel")}
    data["intensityMinutes"] = (
        (dig(stats, "moderateIntensityMinutes") or 0)
        + (dig(stats, "vigorousIntensityMinutes") or 0)
    )

    spo2 = safe("spo2", lambda: api.get_spo2_data(TODAY))
    data["spo2"] = {"avg": dig(spo2, "averageSpO2")}

    resp = safe("respiration", lambda: api.get_respiration_data(TODAY))
    data["respiration"] = {"avg": dig(resp, "avgSleepRespirationValue") or dig(resp, "avgWakingRespirationValue")}

    return data


def main():
    api = login()
    print("Fetching metrics for", TODAY)
    payload = build_payload(api)
    OUTPUT.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {OUTPUT}")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
