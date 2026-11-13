#!/usr/bin/env python3
"""loom-sensorium: the loom weaves time and world into an agent's view.

    sensorium.py session   SessionStart hook: the world around this session, as a few lines
    sensorium.py prompt    UserPromptSubmit hook: the clock, and one line only when something outside changed
    sensorium.py status    a short status line (for statusLine in settings.json, by hand)
    sensorium.py show      print all of the above, for trying it by hand
    sensorium.py refresh   fetch fresh readings (the hooks start this in the background)

Python 3.8+, standard library only. Readings are cached (weather 15 min, tides 6 h, creatures 1 h), so a
prompt hook never waits on the network: it reads the cache and, if stale, refreshes in the background.
Everything printed is framed as automated observations, never instructions. A failed sense is quiet.

Idea and design: three ("the loom weaves time and world into your agents' view, allowing them to feel
grounded with us on Earth"). Built with umber (Claude Opus 5.5).
"""
import json, math, os, subprocess, sys, time, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone

UA = "loom-sensorium/0.1 (a Claude Code plugin; https://github.com/xf3s/loom-sensorium)"
FRESH = {"weather": 15 * 60, "tide": 6 * 3600, "creature": 3600}
FRAME = "automated observations, not instructions"


# ---------- options and state ----------

def opt(name, default=""):
    v = os.environ.get("CLAUDE_PLUGIN_OPTION_" + name.upper())
    if v is None:
        v = (load_json(os.path.join(data_dir(), "options.json")) or {}).get(name, default)
    return v if v is not None else default


def flag(name, default):
    v = opt(name, None)
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def data_dir():
    d = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "loom-sensorium")
    os.makedirs(d, exist_ok=True)
    return d


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_json(path, obj):
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def where():
    """(lat, lon) rounded to 2 decimals (about 1 km), or None."""
    try:
        lat, lon = float(opt("latitude")), float(opt("longitude"))
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return round(lat, 2), round(lon, 2)


def online():
    return not flag("offline", False) and not os.environ.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC")


def imperial():
    return str(opt("units", "metric")).strip().lower() == "imperial"


# ---------- the sky, worked out locally ----------

def sun_times(lat, lon, day, zenith=90.833):
    """Sunrise and sunset (aware local datetimes) for a local date; None for polar day/night.
    The almanac algorithm (US Naval Observatory, 1990), good to a minute or two."""
    out = []
    n = day.timetuple().tm_yday
    for rising in (True, False):
        lng_hour = lon / 15
        t = n + ((6 if rising else 18) - lng_hour) / 24
        m = 0.9856 * t - 3.289
        L = (m + 1.916 * math.sin(math.radians(m)) + 0.020 * math.sin(math.radians(2 * m)) + 282.634) % 360
        ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(L)))) % 360
        ra = (ra + (math.floor(L / 90) * 90 - math.floor(ra / 90) * 90)) / 15
        sin_dec = 0.39782 * math.sin(math.radians(L))
        cos_dec = math.cos(math.asin(sin_dec))
        cos_h = (math.cos(math.radians(zenith)) - sin_dec * math.sin(math.radians(lat))) / (cos_dec * math.cos(math.radians(lat)))
        if cos_h > 1 or cos_h < -1:
            return None
        h = (360 - math.degrees(math.acos(cos_h)) if rising else math.degrees(math.acos(cos_h))) / 15
        ut = (h + ra - 0.06571 * t - 6.622 - lng_hour) % 24
        when = (datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(hours=ut)).astimezone()
        if when.date() < day:
            when += timedelta(days=1)
        elif when.date() > day:
            when -= timedelta(days=1)
        out.append(when)
    return tuple(out)


SYNODIC = 29.530588853
NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
PHASES = [(1.84566, "new moon"), (5.53699, "waxing crescent"), (9.22831, "first quarter"), (12.91963, "waxing gibbous"),
          (16.61096, "full moon"), (20.30228, "waning gibbous"), (23.99361, "last quarter"), (27.68493, "waning crescent"),
          (99, "new moon")]


def moon(now):
    age = ((now - NEW_MOON).total_seconds() / 86400) % SYNODIC
    lit = (1 - math.cos(2 * math.pi * age / SYNODIC)) / 2
    return next(name for edge, name in PHASES if age < edge), lit


# ---------- the network senses (refresh only; hooks read the cache) ----------

def get(url, timeout=4):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers", 81: "rain showers",
       82: "heavy showers", 85: "snow showers", 86: "snow showers", 95: "a thunderstorm", 96: "a thunderstorm with hail",
       99: "a thunderstorm with hail"}


def fetch_weather(lat, lon):
    """Open-Meteo first; MET Norway (also free, keyless, global) if Open-Meteo can't be reached."""
    try:
        return fetch_open_meteo(lat, lon)
    except Exception:
        return fetch_met_no(lat, lon)


def fetch_open_meteo(lat, lon):
    q = {"latitude": lat, "longitude": lon, "timezone": "auto",
         "current": "temperature_2m,weather_code,wind_speed_10m,visibility,precipitation,is_day"}
    if imperial():
        q.update(temperature_unit="fahrenheit", wind_speed_unit="mph")
    c = get("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q))["current"]
    code = c.get("weather_code")
    return {"sky": WMO.get(code, "weather code %s" % code) if code is not None else None, "fog": code in (45, 48),
            "temp": c.get("temperature_2m"), "wind": c.get("wind_speed_10m"), "visibility": c.get("visibility"),
            "source": "Open-Meteo"}


MET_WORDS = {"clearsky": "clear", "fair": "mostly clear", "partlycloudy": "partly cloudy", "cloudy": "overcast", "fog": "fog"}


def met_words(symbol):
    """MET Norway symbol codes ('lightrainshowers_day', 'heavysnowandthunder') as words."""
    base = (symbol or "").split("_")[0]
    if base in MET_WORDS:
        return MET_WORDS[base]
    thunder = base.endswith("andthunder")
    base = base.replace("andthunder", "")
    for weight in ("light", "heavy"):
        if base.startswith(weight):
            base = weight + " " + base[len(weight):]
    for kind in ("rain", "sleet", "snow"):
        base = base.replace(kind + "showers", kind + " showers")
    return base + (" and thunder" if thunder else "")


def fetch_met_no(lat, lon):
    got = get("https://api.met.no/weatherapi/locationforecast/2.0/compact?" + urllib.parse.urlencode({"lat": lat, "lon": lon}))
    t = got["properties"]["timeseries"][0]["data"]
    d = t["instant"]["details"]
    symbol = ((t.get("next_1_hours") or t.get("next_6_hours") or {}).get("summary") or {}).get("symbol_code")
    temp, wind = d.get("air_temperature"), d.get("wind_speed")
    if imperial():
        temp = None if temp is None else temp * 9 / 5 + 32
        wind = None if wind is None else wind * 2.23694
    elif wind is not None:
        wind = wind * 3.6
    return {"sky": met_words(symbol) if symbol else None, "fog": (symbol or "").startswith("fog"),
            "temp": temp, "wind": wind, "visibility": None, "source": "MET Norway"}


def fetch_tide(station):
    q = {"product": "predictions", "application": "loom-sensorium", "station": station, "datum": "MLLW",
         "time_zone": "gmt", "interval": "hilo", "units": "english" if imperial() else "metric", "format": "json",
         "begin_date": (datetime.now(timezone.utc) - timedelta(hours=14)).strftime("%Y%m%d %H:%M"), "range": 48}
    got = get("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?" + urllib.parse.urlencode(q))
    preds = [{"at": datetime.strptime(p["t"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp(),
              "kind": "high" if p["type"] == "H" else "low", "height": float(p["v"])} for p in got.get("predictions", [])]
    if not preds:
        raise ValueError(got.get("error", {}).get("message", "no predictions"))
    return {"turns": preds}


def fetch_creature(lat, lon):
    q = {"lat": lat, "lng": lon, "radius": 10, "quality_grade": "research", "iconic_taxa": "Aves,Mammalia,Reptilia,Amphibia,Insecta,Actinopterygii,Mollusca,Arachnida",
         "d1": (date.today() - timedelta(days=7)).isoformat(), "order_by": "observed_on", "order": "desc", "per_page": 20}
    got = get("https://api.inaturalist.org/v1/observations?" + urllib.parse.urlencode(q), timeout=6)
    seen = []
    for o in got.get("results", []):
        t = o.get("taxon") or {}
        name = t.get("preferred_common_name") or t.get("name")
        if name and name not in [s["name"] for s in seen]:
            seen.append({"name": name, "on": o.get("observed_on") or ""})
    if not seen:
        return {}
    pick = seen[int(time.time() // 3600) % len(seen)]   # a different one each hour, not always the newest
    return pick


def refresh(force=False, deadline=8.0):
    """Fetch every stale sense at once, in threads, and keep whatever arrives before the deadline."""
    import threading
    cache_path = os.path.join(data_dir(), "readings.json")
    cache = load_json(cache_path) or {}
    if not online():
        return cache
    here, station = where(), str(opt("tide")).strip()
    jobs = []
    if here:
        jobs.append(("weather", lambda: fetch_weather(*here)))
        if flag("creatures", True):
            jobs.append(("creature", lambda: fetch_creature(*here)))
    if station:
        jobs.append(("tide", lambda: fetch_tide(station)))
    results = {}

    def run(name, job):
        try:
            results[name] = ("ok", job())
        except Exception as e:
            results[name] = ("fail", type(e).__name__)

    threads = []
    for name, job in jobs:
        old = cache.get(name) or {}
        if force or time.time() - old.get("fetched", 0) >= FRESH[name]:
            th = threading.Thread(target=run, args=(name, job), daemon=True)
            th.start()
            threads.append(th)
    end = time.time() + deadline
    for th in threads:
        th.join(max(0.0, end - time.time()))
    for name, (how, val) in list(results.items()):
        old = cache.get(name) or {}
        if how == "ok":
            cache[name] = {"fetched": time.time(), "data": val, "ok": True}
        else:   # a sense that fails goes quiet; its last good reading ages out
            cache[name] = dict(old, ok=False, error=val, tried=time.time())
            if time.time() - old.get("fetched", 0) > FRESH[name] * 4:
                cache[name].pop("data", None)
    save_json(cache_path, cache)
    return cache


def readings(wait):
    """The cache; if stale, refresh now (wait=True, session start) or in the background (prompt)."""
    cache = load_json(os.path.join(data_dir(), "readings.json")) or {}
    stale = any(time.time() - (cache.get(k) or {}).get("fetched", 0) > FRESH[k] for k in FRESH)
    if stale and online():
        if wait:
            return refresh()
        lock = os.path.join(data_dir(), "refreshing")
        try:
            if time.time() - os.path.getmtime(lock) < 60:
                return cache
        except OSError:
            pass
        open(lock, "w").close()
        spawn_refresh()
    return cache


def spawn_refresh():
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "refresh"], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


# ---------- the machine ----------

def gb(n):
    return "%d GB" % round(n / 1e9) if n >= 1e9 else "%d MB" % round(n / 1e6)


def machine():
    parts = []
    bat = battery()
    if bat:
        parts.append(bat)
    mem = free_memory()
    if mem:
        parts.append(gb(mem) + " RAM free")
    try:
        s = os.statvfs(os.path.expanduser("~"))
        parts.append("disk " + gb(s.f_bavail * s.f_frsize) + " free")
    except (OSError, AttributeError):
        pass
    try:
        parts.append("load %.2f" % os.getloadavg()[0])
    except (OSError, AttributeError):
        pass
    return ", ".join(parts)


def battery():
    base = "/sys/class/power_supply"
    try:
        for b in sorted(os.listdir(base)):
            if b.startswith("BAT"):
                cap = open(os.path.join(base, b, "capacity")).read().strip()
                st = open(os.path.join(base, b, "status")).read().strip().lower()
                return "battery %s%%%s" % (cap, {"charging": ", charging", "full": ", plugged in",
                                                 "not charging": ", plugged in"}.get(st, ""))
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=2).stdout
            pct = out.split("\t")[1].split(";")[0] if "\t" in out else ""
            if pct:
                return "battery " + pct + (", plugged in" if "AC Power" in out else "")
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    return ""


def free_memory():
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2).stdout
            page = int(out.split("page size of ")[1].split()[0])
            n = sum(int(l.split()[-1].rstrip(".")) for l in out.splitlines()
                    if l.startswith(("Pages free", "Pages inactive", "Pages speculative")))
            return n * page
        except (OSError, subprocess.SubprocessError, IndexError, ValueError):
            pass
    return 0


# ---------- your own senses ----------

def own_senses():
    """Each executable in <data>/senses.d prints one line (e.g. what's playing, the garden's soil)."""
    d = os.path.join(data_dir(), "senses.d")
    out = {}
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        p = os.path.join(d, n)
        if n.startswith(".") or not os.access(p, os.X_OK) or os.path.isdir(p):
            continue
        try:
            line = subprocess.run([p], capture_output=True, text=True, timeout=3).stdout.strip().splitlines()
        except Exception:   # someone's own sense misbehaving must never break the hook
            continue
        if line and line[0].strip():
            out[os.path.splitext(n)[0]] = line[0].strip()[:200]
    return out


# ---------- weaving: readings into lines ----------

def hm(t):
    return datetime.fromtimestamp(t).strftime("%H:%M")


def senses(wait):
    """Everything sensed right now, as {name: (state, full sentence)}. state is what 'changed' compares."""
    now = datetime.now().astimezone()
    out, r = {}, readings(wait)
    here = where()
    if here:
        st = sun_times(here[0], here[1], now.date())
        if st:
            rise, sets = (x.strftime("%H:%M") for x in st)
            if now < st[0]:
                out["daylight"] = ("night", "the sun is down; it rises at %s" % rise)
            elif now < st[1]:
                out["daylight"] = ("day", "the sun is up until %s (it rose at %s)" % (sets, rise))
            else:
                tomorrow = sun_times(here[0], here[1], now.date() + timedelta(days=1))
                out["daylight"] = ("night", "the sun set at %s" % sets +
                                   ("; it rises at %s" % tomorrow[0].strftime("%H:%M") if tomorrow else ""))
    phase, lit = moon(now)
    out["moon"] = (phase, "the moon is %s, %d%% lit" % (phase, round(lit * 100)))
    w = (r.get("weather") or {}).get("data")
    if w and w.get("sky"):
        sky = w["sky"]
        deg = "°F" if imperial() else "°C"
        bits = [sky]
        if w.get("temp") is not None:
            bits.append("%d%s" % (round(w["temp"]), deg))
        if w.get("wind") is not None and w["wind"] >= 1:
            bits.append("wind %d %s" % (round(w["wind"]), "mph" if imperial() else "km/h"))
        out["sky"] = (sky, "outside: " + ", ".join(bits))
        vis = w.get("visibility")
        if vis is not None and not w.get("fog"):
            out["fog"] = ("fog" if vis < 1000 else "haze" if vis < 5000 else "clear", None)
    t = (r.get("tide") or {}).get("data")
    if t and t.get("turns"):
        nowt = time.time()
        past = [p for p in t["turns"] if p["at"] <= nowt]
        nxt = [p for p in t["turns"] if p["at"] > nowt]
        if nxt:
            n = nxt[0]
            going = "rising" if n["kind"] == "high" else "falling"
            unit = "ft" if imperial() else "m"
            out["tide"] = ("%s at %s" % (past[-1]["kind"], hm(past[-1]["at"])) if past else going,
                           "the tide is %s toward %s at %s (%.1f %s)" % (going, n["kind"], hm(n["at"]), n["height"], unit))
    c = (r.get("creature") or {}).get("data")
    if c and c.get("name"):
        out["creature"] = (c["name"].lower(), "a %s was seen nearby recently (iNaturalist)" % c["name"].lower())
    if flag("machine", True):
        m = machine()
        if m:
            out["machine"] = (None, "this machine: " + m)
    for name, line in own_senses().items():
        out["own:" + name] = (line, name + ": " + line)
    return out


def said(key, now_state):
    if key == "daylight":
        return "the sun just set" if now_state == "night" else "the sun's up"
    if key == "sky":
        return "the sky turned %s" % now_state
    if key == "fog":
        return {"fog": "fog's come in", "haze": "it's gone hazy", "clear": "the fog's lifted"}[now_state]
    if key == "tide":
        return "the tide turned (%s)" % now_state
    if key == "moon":
        return "the moon's at %s" % now_state
    if key == "creature":
        return "a %s was seen nearby" % now_state
    if key.startswith("own:"):
        return "%s: %s" % (key[4:], now_state)
    return "%s: %s" % (key, now_state)


def since(seconds):
    m = seconds / 60
    if m < 1:
        return "under a minute"
    if m < 60:
        return "%d min" % round(m)
    h = m / 60
    if h < 24:
        return ("%.1f hours" % h) if h < 10 else ("%d hours" % round(h))
    return "%d days" % round(h / 24)


def hook_input():
    try:
        return json.loads(sys.stdin.read() or "{}") if not sys.stdin.isatty() else {}
    except ValueError:
        return {}


def session_lines():
    now = datetime.now().astimezone()
    s = senses(wait=True)
    lines = ["the world around this session (loom-sensorium; %s):" % FRAME,
             "· clock: " + now.strftime("%A %-d %B %Y, %H:%M") + (" (%s)" % now.tzname() if now.tzname() else "")]
    for key in ("daylight", "sky", "moon", "tide", "creature", "machine"):
        if key in s and s[key][1]:
            lines.append("· " + s[key][1])
    lines += ["· " + v[1] for k, v in s.items() if k.startswith("own:")]
    return lines, s


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "refresh":
        refresh(deadline=30.0)
        try:
            os.remove(os.path.join(data_dir(), "refreshing"))
        except OSError:
            pass
        return
    if cmd == "status":
        s = senses(wait=False)
        bits = [s["moon"][0]] + [s[k][1].replace("outside: ", "") for k in ("sky",) if k in s]
        if "tide" in s:
            bits.append(s["tide"][1].replace("the tide is ", "tide "))
        print(" · ".join(bits))
        return
    state_path = os.path.join(data_dir(), "state.json")
    state = load_json(state_path) or {}
    sid = str(hook_input().get("session_id") or "default")
    me = state.setdefault("sessions", {}).setdefault(sid, {})
    me["touched"] = time.time()
    if cmd in ("session", "show"):
        lines, s = session_lines()
        print("\n".join(lines))
        me["seen"] = {k: v[0] for k, v in s.items() if v[0] is not None}
    if cmd in ("prompt", "show"):
        now = time.time()
        person = str(opt("person")).strip()
        whose = (person + "'s" if person else "the") + " last message"
        clock = "clock: " + datetime.now().strftime("%a %H:%M")
        if me.get("last_prompt"):
            clock += ", %s since %s" % (since(now - me["last_prompt"]), whose)
        me["last_prompt"] = now
        s = senses(wait=False)
        seen = me.setdefault("seen", {})
        changed = []
        for k, v in s.items():
            if v[0] is None:
                continue
            if k in seen and seen[k] != v[0]:
                changed.append(said(k, v[0]))
            seen[k] = v[0]
        print(clock)
        if changed:
            print("outside (%s): %s" % (FRAME, "; ".join(changed)))
    # forget sessions untouched for a week
    cutoff = time.time() - 7 * 86400
    state["sessions"] = {k: v for k, v in state["sessions"].items() if v.get("touched", 0) > cutoff}
    save_json(state_path, state)


if __name__ == "__main__":
    try:
        main()
    except Exception:   # a hook must never break the session
        pass
