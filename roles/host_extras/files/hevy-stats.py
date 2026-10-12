#!/usr/bin/env python3
"""Hevy -> Home Assistant stats. Writes /opt/homelab/homeassistant/config/hevy_stats.json.
Caches all workouts in /var/lib/hevy-stats/workouts.json; syncs incrementally via
/v1/workouts/events and does a full re-download once a day. Hevy stores kg; output in lbs."""
import json, os, sys, time, urllib.request, urllib.parse, re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from collections import Counter, defaultdict

ENV = "/opt/homelab/hevy/.env"
OUT = "/opt/homelab/homeassistant/config/hevy_stats.json"
CACHE_DIR = "/var/lib/hevy-stats"
CACHE = os.path.join(CACHE_DIR, "workouts.json")
API = "https://api.hevyapp.com"
TZ = ZoneInfo("America/Moncton")
KG2LB = 2.2046226218
FULL_EVERY = 24 * 3600

def key():
    for line in open(ENV):
        line = line.strip()
        if line.startswith("HEVY_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("no key")

KEY = key()

def get(path, **params):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"api-key": KEY, "accept": "application/json",
                                                       "user-agent": "homelab-hevy-stats/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429 or e.code >= 500:
                time.sleep(5 * (attempt + 1)); continue
            raise
        except Exception:
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("API failed: " + path)

def paged(path, field, **params):
    page, out = 1, []
    while True:
        d = get(path, page=page, pageSize=10, **params)
        if not d: break
        out += d.get(field, [])
        if page >= d.get("page_count", 1): break
        page += 1
        time.sleep(0.3)
    return out

def sync():
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = None
    try:
        cache = json.load(open(CACHE))
    except Exception:
        pass
    now = time.time()
    if not cache or now - cache.get("full_sync", 0) > FULL_EVERY:
        ws = paged("/v1/workouts", "workouts")
        cache = {"full_sync": now, "since": datetime.now(timezone.utc).isoformat(),
                 "workouts": {w["id"]: w for w in ws}}
    else:
        since = cache["since"]
        new_since = datetime.now(timezone.utc).isoformat()
        evs = paged("/v1/workouts/events", "events", since=since)
        for e in evs:
            if e.get("type") == "deleted":
                cache["workouts"].pop(e.get("id") or (e.get("workout") or {}).get("id"), None)
            elif e.get("workout"):
                cache["workouts"][e["workout"]["id"]] = e["workout"]
        # step back 1 h so nothing slips between runs
        cache["since"] = (datetime.fromisoformat(new_since) - timedelta(hours=1)).isoformat()
    tmp = CACHE + ".tmp"
    json.dump(cache, open(tmp, "w")); os.chmod(tmp, 0o600); os.replace(tmp, CACHE)
    return list(cache["workouts"].values())

def pt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def wvol(w):
    v = 0.0; sets = 0
    for ex in w.get("exercises", []):
        for s in ex.get("sets", []):
            sets += 1
            if s.get("weight_kg") and s.get("reps"):
                v += s["weight_kg"] * s["reps"]
    return v * KG2LB, sets

MAIN_LIFTS = [
    ("Bench Press", r"^bench press \(barbell\)$"),
    ("Squat", r"^(full |back )?squat \(barbell\)$"),
    ("Deadlift", r"^deadlift \(barbell\)$"),
    ("Overhead Press", r"^(overhead|shoulder|military) press \((barbell|standing)\)$|^overhead press"),
    ("Incline Bench", r"^incline bench press \(barbell\)$"),
    ("Barbell Row", r"^bent over row \(barbell\)$"),
]

def main():
    workouts = sync()
    for w in workouts:
        w["_s"] = pt(w["start_time"]).astimezone(TZ)
        w["_dur"] = max(0, (pt(w["end_time"]) - pt(w["start_time"])).total_seconds() / 60) if w.get("end_time") else 0
        w["_vol"], w["_sets"] = wvol(w)
    workouts.sort(key=lambda w: w["_s"], reverse=True)
    now = datetime.now(TZ)
    today = now.date()
    wk0 = today - timedelta(days=today.weekday())  # Monday
    mo0 = today.replace(day=1)
    d30 = now - timedelta(days=30)

    def wkstart(d): return d - timedelta(days=d.weekday())
    this_week = [w for w in workouts if w["_s"].date() >= wk0]
    this_month = [w for w in workouts if w["_s"].date() >= mo0]
    last30 = [w for w in workouts if w["_s"] >= d30]

    weeks = Counter(); wvols = defaultdict(float)
    for w in workouts:
        k = wkstart(w["_s"].date()); weeks[k] += 1; wvols[k] += w["_vol"]
    # streak: consecutive weeks with >=1 workout, counting back from this week
    # (an empty current week doesn't break the streak yet)
    streak = 0; k = wk0
    if weeks.get(k, 0) == 0: k -= timedelta(weeks=1)
    while weeks.get(k, 0) > 0:
        streak += 1; k -= timedelta(weeks=1)
    best = 0; run = 0
    if weeks:
        k = min(weeks)
        while k <= wk0:
            run = run + 1 if weeks.get(k, 0) else 0; best = max(best, run); k += timedelta(weeks=1)

    weekly = []
    for i in range(11, -1, -1):
        k = wk0 - timedelta(weeks=i)
        weekly.append({"week": k.isoformat(), "label": k.strftime("%b %-d"),
                       "workouts": weeks.get(k, 0), "volume_lbs": round(wvols.get(k, 0))})

    freq = Counter(); prs = {}; lastdone = {}
    for w in workouts:
        for ex in w.get("exercises", []):
            t = ex["title"]; freq[t] += 1
            lastdone.setdefault(t, w["_s"].date().isoformat())
            for s in ex.get("sets", []):
                kg = s.get("weight_kg"); reps = s.get("reps")
                if not kg or not reps or s.get("type") == "warmup": continue
                e1rm = kg * (1 + reps / 30) if reps > 1 else kg
                p = prs.setdefault(t, {"kg": 0, "reps": 0, "date": None, "e1rm": 0})
                if kg > p["kg"] or (kg == p["kg"] and reps > p["reps"]):
                    p.update(kg=kg, reps=reps, date=w["_s"].date().isoformat())
                p["e1rm"] = max(p["e1rm"], e1rm)
    def prrow(name, t):
        p = prs[t]
        return {"lift": name, "exercise": t, "weight_lbs": round(p["kg"] * KG2LB, 1), "reps": p["reps"],
                "date": p["date"], "e1rm_lbs": round(p["e1rm"] * KG2LB)}
    main_prs = []
    for name, rx in MAIN_LIFTS:
        cands = [t for t in prs if re.search(rx, t.lower())]
        if cands:
            t = max(cands, key=lambda t: freq[t])
            main_prs.append(prrow(name, t))
    top = [{"exercise": t, "count": c, "last": lastdone[t],
            "best_lbs": round(prs[t]["kg"] * KG2LB, 1) if t in prs else None,
            "best_reps": prs[t]["reps"] if t in prs else None}
           for t, c in freq.most_common(10)]

    last = workouts[0] if workouts else None
    last_out = None
    if last:
        last_out = {"title": last["title"].strip(), "date": last["_s"].isoformat(),
                    "duration_min": round(last["_dur"]), "volume_lbs": round(last["_vol"]),
                    "sets": last["_sets"], "exercises": [e["title"] for e in last.get("exercises", [])]}

    body = []
    try:
        bm = paged("/v1/body_measurements", "body_measurements")
        body = sorted([b for b in bm if b.get("weight_kg")], key=lambda b: b["date"], reverse=True)
    except Exception:
        bm = None
    bw = None
    if body:
        b = body[0]
        bw = {"weight_lbs": round(b["weight_kg"] * KG2LB, 1), "date": b["date"],
              "fat_percent": next((x.get("fat_percent") for x in body if x.get("fat_percent")), None),
              "history": [{"date": x["date"], "lbs": round(x["weight_kg"] * KG2LB, 1)} for x in body[:10]]}

    durs = [w["_dur"] for w in workouts if w["_dur"] > 0]
    durs30 = [w["_dur"] for w in last30 if w["_dur"] > 0]
    out = {
        "updated": int(time.time()),
        "total_workouts": len(workouts),
        "this_week": len(this_week),
        "this_week_volume_lbs": round(sum(w["_vol"] for w in this_week)),
        "this_month": len(this_month),
        "this_month_volume_lbs": round(sum(w["_vol"] for w in this_month)),
        "last_30_days": len(last30),
        "streak_weeks": streak,
        "best_streak_weeks": best,
        "total_volume_lbs": round(sum(w["_vol"] for w in workouts)),
        "avg_duration_min": round(sum(durs) / len(durs)) if durs else None,
        "avg_duration_30d_min": round(sum(durs30) / len(durs30)) if durs30 else None,
        "first_workout": workouts[-1]["_s"].date().isoformat() if workouts else None,
        "last_workout": last_out,
        "weekly": weekly,
        "top_exercises": top,
        "prs": main_prs,
        "body_weight": bw,
        "units": "lbs (Hevy stores kg; converted)",
    }
    tmp = OUT + ".tmp"
    json.dump(out, open(tmp, "w"), ensure_ascii=False); os.chmod(tmp, 0o644); os.replace(tmp, OUT)

if __name__ == "__main__":
    main()
