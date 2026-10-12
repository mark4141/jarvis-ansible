#!/usr/bin/env python3
# Writes host + Docker stats for the Home Assistant "Server" dashboard (run every minute by server-stats.timer).
import json, os, shutil, subprocess, time

OUT = "/opt/homelab/homeassistant/config/server_stats.json"

def cpu_times():
    with open("/proc/stat") as f:
        p = [int(x) for x in f.readline().split()[1:]]
    idle = p[3] + p[4]
    return idle, sum(p)

def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=40).stdout
    except Exception:
        return ""

i1, t1 = cpu_times(); time.sleep(1); i2, t2 = cpu_times()
cpu = round(100 * (1 - (i2 - i1) / max(t2 - t1, 1)), 1)

mem = {}
with open("/proc/meminfo") as f:
    for line in f:
        k, v = line.split(":"); mem[k] = int(v.split()[0])
mt, ma = mem["MemTotal"], mem["MemAvailable"]
st, sf = mem["SwapTotal"], mem["SwapFree"]

load = os.getloadavg()
with open("/proc/uptime") as f:
    up = float(f.read().split()[0])

temp = None
for h in sorted(os.listdir("/sys/class/hwmon")):
    try:
        if open(f"/sys/class/hwmon/{h}/name").read().strip() == "coretemp":
            temp = round(int(open(f"/sys/class/hwmon/{h}/temp1_input").read()) / 1000, 1)
    except Exception:
        pass

du = shutil.disk_usage("/")

# containers
stats = {}
for line in run(["docker", "stats", "--no-stream", "--format", "{{json .}}"]).splitlines():
    try:
        d = json.loads(line)
        memused = d["MemUsage"].split("/")[0].strip()
        num = float("".join(c for c in memused if c.isdigit() or c == "."))
        unit = memused.lstrip("0123456789.").strip()
        mb = num * {"B": 1/1048576, "KiB": 1/1024, "MiB": 1, "GiB": 1024, "TiB": 1048576}.get(unit, 1)
        stats[d["Name"]] = {"cpu": float(d["CPUPerc"].rstrip("%") or 0), "mem_mb": round(mb, 1)}
    except Exception:
        pass

containers = {}
for line in run(["docker", "ps", "-a", "--format", "{{json .}}"]).splitlines():
    try:
        d = json.loads(line)
    except Exception:
        continue
    name = d["Names"]; status = d["Status"]
    health = "healthy" if "(healthy)" in status else "unhealthy" if "(unhealthy)" in status else "starting" if "(health: starting)" in status else "none"
    s = stats.get(name, {})
    containers[name] = {"state": d["State"], "health": health, "status": status,
                        "cpu": s.get("cpu", 0.0), "mem_mb": s.get("mem_mb", 0.0), "image": d["Image"]}

out = {
    "updated": int(time.time()),
    "cpu_percent": cpu,
    "load_1": round(load[0], 2), "load_5": round(load[1], 2), "load_15": round(load[2], 2),
    "cpu_count": os.cpu_count(),
    "mem_total_gb": round(mt / 1048576, 2), "mem_used_gb": round((mt - ma) / 1048576, 2),
    "mem_percent": round(100 * (mt - ma) / mt, 1),
    "swap_percent": round(100 * (st - sf) / st, 1) if st else 0.0,
    "swap_used_gb": round((st - sf) / 1048576, 2),
    "disk_percent": round(100 * du.used / du.total, 1),
    "disk_free_gb": round(du.free / 1e9, 1), "disk_total_gb": round(du.total / 1e9, 1),
    "cpu_temp": temp,
    "uptime_seconds": int(up),
    "containers_total": len(containers),
    "containers_running": sum(1 for c in containers.values() if c["state"] == "running"),
    "containers_unhealthy": sorted(n for n, c in containers.items() if c["health"] == "unhealthy"),
    "containers_stopped": sorted(n for n, c in containers.items() if c["state"] != "running"),
    "containers": containers,
}
tmp = OUT + ".tmp"
with open(tmp, "w") as f:
    json.dump(out, f)
os.chmod(tmp, 0o644)
os.replace(tmp, OUT)
