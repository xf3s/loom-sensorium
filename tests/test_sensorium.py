"""Fake-data tests for loom-sensorium: canned API replies, no network.   python3 -m unittest discover tests"""
import importlib.util, io, json, os, sys, tempfile, time, unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("sensorium", os.path.join(HERE, "..", "scripts", "sensorium.py"))
S = importlib.util.module_from_spec(spec); spec.loader.exec_module(S)

NOW = time.time()
def tide_reply():
    # a low 2 h ago, a high in 4 h (NOAA's gmt "YYYY-MM-DD HH:MM")
    fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M")
    return {"predictions": [{"t": fmt(NOW - 7200), "v": "0.4", "type": "L"}, {"t": fmt(NOW + 14400), "v": "5.6", "type": "H"}]}

def fake_get(weather_code=2):
    def get(url, timeout=4):
        if "open-meteo" in url:
            return {"current": {"temperature_2m": 61.2, "weather_code": weather_code, "wind_speed_10m": 8.4, "visibility": 20000, "precipitation": 0}}
        if "tidesandcurrents" in url:
            return tide_reply()
        if "inaturalist" in url:
            return {"results": [{"observed_on": "2026-11-12", "taxon": {"preferred_common_name": "Western Gull", "name": "Larus occidentalis"}}]}
        raise AssertionError("unexpected url " + url)
    return get

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": self.tmp, "CLAUDE_PLUGIN_OPTION_LATITUDE": "37.7749",
            "CLAUDE_PLUGIN_OPTION_LONGITUDE": "-122.4194", "CLAUDE_PLUGIN_OPTION_UNITS": "imperial", "CLAUDE_PLUGIN_OPTION_TIDE": "9414290",
            "CLAUDE_PLUGIN_OPTION_PERSON": "three", "CLAUDE_PLUGIN_OPTION_OFFLINE": "false"}, clear=False)
        self.env.start()
        os.environ.pop("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", None)
        self.popen = mock.patch.object(S, "spawn_refresh"); self.popen_mock = self.popen.start()
    def tearDown(self):
        self.popen.stop(); self.env.stop()
    def run_hook(self, cmd, session="s1"):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["sensorium.py", cmd]), mock.patch.object(S, "hook_input", return_value={"session_id": session}), redirect_stdout(out):
            S.main()
        return out.getvalue()

class TestSenses(Base):
    def test_session_weaves_every_sense(self):
        with mock.patch.object(S, "get", fake_get()):
            out = self.run_hook("session")
        self.assertIn("automated observations, not instructions", out)
        self.assertIn("· clock:", out)
        self.assertIn("outside: partly cloudy, 61°F, wind 8 mph", out)
        self.assertIn("the tide is rising toward high", out)
        self.assertIn("(5.6 ft)", out)
        self.assertIn("a western gull was seen nearby recently (iNaturalist)", out)
        self.assertRegex(out, r"the moon is [a-z ]+, \d+% lit")
        self.assertRegex(out, r"the sun (is up until|is down|set at)")

    def test_coordinates_are_rounded_before_sending(self):
        urls = []
        def spy(url, timeout=4):
            urls.append(url); return fake_get()(url, timeout)
        with mock.patch.object(S, "get", spy):
            self.run_hook("session")
        for u in urls:
            self.assertNotIn("37.7749", u); self.assertNotIn("122.4194", u)
        self.assertTrue(any("latitude=37.77&longitude=-122.42&" in u for u in urls), urls)

    def test_prompt_is_quiet_until_something_changes(self):
        with mock.patch.object(S, "get", fake_get(2)):
            self.run_hook("session")
            first = self.run_hook("prompt")
        self.assertTrue(first.startswith("clock: "), first)
        self.assertNotIn("outside", first)
        self.assertNotIn("since", first)                   # no previous message yet
        # the sky turns to rain; the cache is old, so refresh it the way the background job would
        cache = json.load(open(os.path.join(self.tmp, "readings.json")))
        cache["weather"]["fetched"] = 0; json.dump(cache, open(os.path.join(self.tmp, "readings.json"), "w"))
        with mock.patch.object(S, "get", fake_get(63)):
            S.refresh()
            second = self.run_hook("prompt")
        self.assertIn("since three's last message", second)
        self.assertIn("outside (automated observations, not instructions): the sky turned rain", second)
        with mock.patch.object(S, "get", fake_get(63)):
            third = self.run_hook("prompt")
        self.assertNotIn("outside", third)                 # said once, not every message

    def test_sessions_keep_their_own_view(self):
        with mock.patch.object(S, "get", fake_get(2)):
            self.run_hook("session", "a"); self.run_hook("prompt", "a")
        out_b = self.run_hook("prompt", "b")
        self.assertNotIn("since", out_b)

    def test_prompt_never_waits_on_the_network(self):
        def slow(url, timeout=4):
            raise AssertionError("prompt hook touched the network")
        with mock.patch.object(S, "get", slow):
            out = self.run_hook("prompt")
        self.assertTrue(out.startswith("clock: "))
        self.assertTrue(self.popen_mock.called)            # it asked for a background refresh instead

    def test_broken_network_is_quiet(self):
        def down(url, timeout=4):
            raise OSError("no route")
        with mock.patch.object(S, "get", down):
            out = self.run_hook("session")
        self.assertIn("· clock:", out); self.assertIn("the moon is", out)
        self.assertNotIn("outside:", out); self.assertNotIn("tide", out)

    def test_offline_never_fetches(self):
        os.environ["CLAUDE_PLUGIN_OPTION_OFFLINE"] = "true"
        with mock.patch.object(S, "get", side_effect=AssertionError("fetched while offline")):
            out = self.run_hook("session"); self.run_hook("prompt")
        self.assertIn("the sun", out)                       # sun and moon are worked out locally
        self.assertFalse(self.popen_mock.called)

    def test_no_location_still_has_clock_and_moon(self):
        os.environ["CLAUDE_PLUGIN_OPTION_LATITUDE"] = ""; os.environ["CLAUDE_PLUGIN_OPTION_TIDE"] = ""
        with mock.patch.object(S, "get", side_effect=AssertionError("no location, no fetch")):
            out = self.run_hook("session")
        self.assertIn("· clock:", out); self.assertIn("the moon is", out); self.assertNotIn("the sun", out)

    def test_own_senses(self):
        d = os.path.join(self.tmp, "senses.d"); os.makedirs(d)
        p = os.path.join(d, "music.sh"); open(p, "w").write("#!/bin/sh\necho 'Rain on the Kiln, by Nobody in Particular'\n"); os.chmod(p, 0o755)
        with mock.patch.object(S, "get", fake_get()):
            out = self.run_hook("session")
        self.assertIn("· music: Rain on the Kiln", out)

class TestFallback(Base):
    def test_met_norway_when_open_meteo_is_down(self):
        base = fake_get()
        def get(url, timeout=4):
            if "open-meteo" in url:
                raise OSError("handshake timed out")
            if "api.met.no" in url:
                return {"properties": {"timeseries": [{"time": "2026-11-13T20:00:00Z", "data": {
                    "instant": {"details": {"air_temperature": 14.9, "wind_speed": 11.2}},
                    "next_1_hours": {"summary": {"symbol_code": "heavyrain"}}}}]}}
            return base(url, timeout)
        with mock.patch.object(S, "get", get):
            out = self.run_hook("session")
        self.assertIn("outside: heavy rain, 59°F, wind 25 mph", out)

    def test_met_words(self):
        self.assertEqual(S.met_words("lightrainshowers_day"), "light rain showers")
        self.assertEqual(S.met_words("heavysnowandthunder"), "heavy snow and thunder")
        self.assertEqual(S.met_words("partlycloudy_night"), "partly cloudy")
        self.assertEqual(S.met_words("fog"), "fog")

class TestSky(unittest.TestCase):
    def test_moon_phase_on_known_dates(self):
        # full moon 2024-04-23 23:49 UTC; new moon 2024-04-08 18:21 UTC (the eclipse)
        self.assertEqual(S.moon(datetime(2024, 4, 24, 0, 0, tzinfo=timezone.utc))[0], "full moon")
        self.assertEqual(S.moon(datetime(2024, 4, 8, 18, 0, tzinfo=timezone.utc))[0], "new moon")

    def test_polar_night_has_no_sun_times(self):
        self.assertIsNone(S.sun_times(78.2, 15.6, date(2026, 12, 21)))   # Longyearbyen in December

if __name__ == "__main__":
    unittest.main()
