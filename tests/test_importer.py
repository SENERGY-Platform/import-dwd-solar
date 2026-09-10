# Copyright 2026 InfAI (CC SES)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import importlib
import io
import logging
import unittest
import zipfile
from typing import Dict, List, Optional, Tuple
from unittest import mock

from lib.dwd import archive, importer, rows, stations
from lib.dwd.archive import BASE_URL, NOW, RECENT, SOLAR, TEMPERATURE, blocks_overlapping, \
    fetch_historical_blocks, historical_url, product_url, read_products
from lib.dwd.importer import MAX_HOLD, NOW_REACH, RECENT_REACH, SolarImport, as_utc, covered_until, \
    start_of_day
from lib.dwd.stations import Station

STATION = Station(station_id="02932", name="Leipzig/Halle", lat=51.4347, long=12.2396, height=131.0)
OTHER_STATION = Station(station_id="00427", name="Berlin Brandenburg", lat=52.3805, long=13.5304, height=46.0)

SOLAR_HEADER = "STATIONS_ID;MESS_DATUM;  QN;DS_10;GS_10;SD_10;LS_10;eor"
TEMPERATURE_HEADER = "STATIONS_ID;MESS_DATUM;  QN;PP_10;TT_10;TM5_10;RF_10;TD_10;eor"

# Padded exactly like the delivered files, so the tests exercise the space stripping too.
SOLAR_NOW_ROWS = [
    "       2932;202609090000;    2;   0.0;   0.0;   0.000;  23.7;eor",
    "       2932;202609090010;    2;  12.3;  16.7;   0.050;  24.1;eor",
    "       2932;202609090020;    1;  -999;  -999;    -999;  -999;eor",
    "       2932;202609090030;    2;  20.8;  26.8;   0.150;  21.5;eor",
]

# The recent archive reaches back over yesterday and overlaps the current day by one row.
SOLAR_RECENT_ROWS = [
    "       2932;202609082340;    2;   1.0;   2.0;   0.000;  30.0;eor",
    "       2932;202609082350;    2;   3.0;   4.0;   0.000;  31.0;eor",
    "       2932;202609090000;    2;   0.0;   0.0;   0.000;  23.7;eor",
]

TEMPERATURE_NOW_ROWS = [
    "       2932;202609090000;    2;  988.4;  22.4;  20.8;  55.9;  13.2;eor",
    "       2932;202609090010;    2;  988.5;  22.3;  21.0;  56.2;  13.2;eor",
    # Delivered with DWD's missing marker, so this archive has a row for every solar instant of the day and the
    # tests using it exercise what they are about rather than the hold back rule.
    "       2932;202609090020;    2;  988.6;  -999;  21.2;  56.5;  13.1;eor",
    "       2932;202609090030;    2;  995.4;  20.0;  22.7;  45.6;   7.9;eor",
]

TEMPERATURE_RECENT_ROWS = [
    "       2932;202609082340;    2;  988.0;  18.0;  17.0;  60.0;  10.0;eor",
    "       2932;202609082350;    2;  988.1;  17.5;  16.5;  61.0;  10.1;eor",
]


def utc(year, month, day, hour, minute):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.timezone.utc)


def build_zip(member: str, header: str, rows: List[str]) -> bytes:
    '''Builds an archive of the shape DWD delivers: one produkt_ file of semicolon separated padded rows.'''
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Metadaten_Geographie_02932.txt", "not an observation file")
        archive.writestr(member, "\r\n".join([header] + rows) + "\r\n")
    return buffer.getvalue()


ARCHIVES = {
    product_url(SOLAR, NOW, "02932"):
        build_zip("produkt_zehn_now_sd_20260909_20260909_02932.txt", SOLAR_HEADER, SOLAR_NOW_ROWS),
    product_url(SOLAR, RECENT, "02932"):
        build_zip("produkt_zehn_min_sd_20250308_20260908_02932.txt", SOLAR_HEADER, SOLAR_RECENT_ROWS),
    product_url(TEMPERATURE, NOW, "02932"):
        build_zip("produkt_zehn_now_tu_20260909_20260909_02932.txt", TEMPERATURE_HEADER, TEMPERATURE_NOW_ROWS),
    product_url(TEMPERATURE, RECENT, "02932"):
        build_zip("produkt_zehn_min_tu_20250308_20260908_02932.txt", TEMPERATURE_HEADER, TEMPERATURE_RECENT_ROWS),
}


def stub_fetch(url: str) -> Optional[bytes]:
    '''Answers only the archives built above; anything else is treated as unavailable.'''
    return ARCHIVES.get(url)


class FakeLib:
    '''Stands in for import_lib.ImportLib, recording what would be published.'''

    def __init__(self, config: Dict = None, last_published: Optional[datetime.datetime] = None):
        self.config = config or {}
        self.last_published = last_published
        self.puts: List[Tuple[datetime.datetime, Dict]] = []

    def get_config(self, key: str, default):
        return self.config.get(key, default)

    def get_last_published_datetime(self):
        if self.last_published is None:
            return None, None
        return self.last_published, {"time": "recorded"}

    def put(self, date_time: datetime.datetime, value: Dict):
        self.puts.append((date_time, value))


def make_import(lib: FakeLib, stations=None, with_temperature: bool = True) -> SolarImport:
    return SolarImport(lib, stations if stations is not None else [STATION], with_temperature, stub_fetch)


class TestAsUtc(unittest.TestCase):
    def test_a_naive_datetime_is_read_as_utc(self):
        # import_lib parses its own ...Z timestamp without a tzinfo, so the instant is UTC but not marked.
        normalised = as_utc(datetime.datetime(2026, 9, 9, 0, 10))
        self.assertEqual(utc(2026, 9, 9, 0, 10), normalised)
        self.assertIsNotNone(normalised.tzinfo)

    def test_an_aware_datetime_keeps_its_instant(self):
        offset = datetime.datetime(2026, 9, 9, 2, 10, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
        self.assertEqual(utc(2026, 9, 9, 0, 10), as_utc(offset))

    def test_none_stays_none(self):
        self.assertIsNone(as_utc(None))


class TestStartOfDay(unittest.TestCase):
    def test_returns_midnight_utc_of_the_same_day(self):
        self.assertEqual(utc(2026, 9, 9, 0, 0), start_of_day(utc(2026, 9, 9, 23, 59)))

    def test_midnight_is_its_own_start_of_day(self):
        self.assertEqual(utc(2026, 9, 9, 0, 0), start_of_day(utc(2026, 9, 9, 0, 0)))


class TestCursors(unittest.TestCase):
    def test_every_station_starts_from_the_single_last_published_signal(self):
        solar_import = make_import(FakeLib(), [STATION, OTHER_STATION])
        solar_import.seed_cursors(utc(2026, 9, 9, 0, 10))
        self.assertEqual(utc(2026, 9, 9, 0, 10), solar_import.cursor("02932"))
        self.assertEqual(utc(2026, 9, 9, 0, 10), solar_import.cursor("00427"))

    def test_a_naive_signal_is_seeded_as_utc(self):
        solar_import = make_import(FakeLib())
        solar_import.seed_cursors(datetime.datetime(2026, 9, 9, 0, 10))
        self.assertEqual(utc(2026, 9, 9, 0, 10), solar_import.cursor("02932"))

    def test_a_fresh_import_has_no_cursor(self):
        solar_import = make_import(FakeLib())
        solar_import.seed_cursors(None)
        self.assertIsNone(solar_import.cursor("02932"))


class TestStartupGates(unittest.TestCase):
    def setUp(self):
        self.now = utc(2026, 9, 9, 12, 0)
        self.solar_import = make_import(FakeLib())

    def test_a_fresh_import_needs_both_archives(self):
        self.solar_import.seed_cursors(None)
        self.assertTrue(self.solar_import.needs_historical(STATION, self.now))
        self.assertTrue(self.solar_import.needs_recent(STATION, self.now))

    def test_a_cursor_inside_the_recent_reach_needs_no_history(self):
        self.solar_import.seed_cursors(self.now - RECENT_REACH + datetime.timedelta(minutes=10))
        self.assertFalse(self.solar_import.needs_historical(STATION, self.now))

    def test_a_cursor_outside_the_recent_reach_needs_history(self):
        self.solar_import.seed_cursors(self.now - RECENT_REACH - datetime.timedelta(minutes=10))
        self.assertTrue(self.solar_import.needs_historical(STATION, self.now))

    def test_a_cursor_inside_the_now_reach_needs_no_recent(self):
        self.solar_import.seed_cursors(self.now - NOW_REACH + datetime.timedelta(minutes=10))
        self.assertFalse(self.solar_import.needs_recent(STATION, self.now))

    def test_a_cursor_outside_the_now_reach_needs_recent(self):
        self.solar_import.seed_cursors(self.now - NOW_REACH - datetime.timedelta(minutes=10))
        self.assertTrue(self.solar_import.needs_recent(STATION, self.now))


class TestKindsFor(unittest.TestCase):
    def setUp(self):
        self.solar_import = make_import(FakeLib())

    def test_a_cursor_from_today_is_caught_up_from_the_now_archive(self):
        self.solar_import.seed_cursors(utc(2026, 9, 9, 0, 10))
        self.assertEqual((NOW,), self.solar_import.kinds_for(STATION, utc(2026, 9, 9, 12, 0)))

    def test_a_cursor_from_yesterday_evening_also_needs_the_recent_archive(self):
        # The hole this guards: at 00:05 the gap is 15 minutes, yet the tail of yesterday is only in recent.
        self.solar_import.seed_cursors(utc(2026, 9, 8, 23, 50))
        self.assertEqual((RECENT, NOW), self.solar_import.kinds_for(STATION, utc(2026, 9, 9, 0, 5)))

    def test_the_first_instant_of_today_is_covered_by_the_now_archive(self):
        self.solar_import.seed_cursors(utc(2026, 9, 9, 0, 0))
        self.assertEqual((NOW,), self.solar_import.kinds_for(STATION, utc(2026, 9, 9, 0, 5)))

    def test_a_fresh_import_does_not_backfill_from_recent(self):
        # HISTORIC and RECENT decide how far back a fresh import reaches; the scheduled run only closes a known gap.
        self.solar_import.seed_cursors(None)
        self.assertEqual((NOW,), self.solar_import.kinds_for(STATION, utc(2026, 9, 9, 12, 0)))


class TestPublishFilter(unittest.TestCase):
    # The backfill is handed the instant of the run, so what it may publish does not depend on the wall clock
    # of the machine the tests run on.
    BACKFILL_NOW = utc(2026, 9, 9, 0, 35)

    def test_only_instants_strictly_newer_than_the_cursor_are_published(self):
        lib = FakeLib()
        solar_import = make_import(lib)
        solar_import.seed_cursors(utc(2026, 9, 8, 23, 40))
        # The recent temperature archive ends at 23:50, so the solar row of 00:00 stays behind for a later run.
        self.assertTrue(solar_import.import_recent(STATION, self.BACKFILL_NOW))
        self.assertEqual([utc(2026, 9, 8, 23, 50)], [date_time for date_time, _ in lib.puts])

    def test_an_instant_equal_to_the_cursor_is_not_republished(self):
        lib = FakeLib()
        solar_import = make_import(lib)
        solar_import.seed_cursors(utc(2026, 9, 9, 0, 0))
        self.assertTrue(solar_import.import_recent(STATION, self.BACKFILL_NOW))
        self.assertEqual([], lib.puts)

    def test_the_cursor_advances_so_a_second_run_republishes_nothing(self):
        lib = FakeLib()
        solar_import = make_import(lib)
        solar_import.seed_cursors(None)
        self.assertTrue(solar_import.import_recent(STATION, self.BACKFILL_NOW))
        self.assertEqual(2, len(lib.puts))
        self.assertEqual(utc(2026, 9, 8, 23, 50), solar_import.cursor("02932"))
        self.assertTrue(solar_import.import_recent(STATION, self.BACKFILL_NOW))
        self.assertEqual(2, len(lib.puts))

    def test_a_station_cursor_does_not_hold_back_another_station(self):
        lib = FakeLib()
        solar_import = make_import(lib, [STATION, OTHER_STATION])
        solar_import.seed_cursors(None)
        solar_import.import_recent(STATION, self.BACKFILL_NOW)
        self.assertEqual(utc(2026, 9, 8, 23, 50), solar_import.cursor("02932"))
        self.assertIsNone(solar_import.cursor("00427"))


class TestCursorDurability(unittest.TestCase):
    def test_the_cursor_holds_what_was_already_published_when_a_put_fails(self):
        # The cursor is advanced per published instant, so a run that dies half way does not replay what it sent.
        lib = FakeLib()
        real_put = lib.put

        failures = []

        def put_twice_then_fail(date_time, value):
            if len(lib.puts) == 2 and not failures:
                failures.append(date_time)
                raise IOError("kafka gone")
            real_put(date_time, value)

        lib.put = put_twice_then_fail
        solar_import = make_import(lib)
        solar_import.seed_cursors(None)
        # The aborted run reports nothing, but the cursor keeps the two instants that did reach the platform.
        with self.assertLogs("import.importer", level=logging.ERROR):
            self.assertEqual(0, solar_import.import_latest(utc(2026, 9, 9, 12, 0)))
        self.assertEqual(2, len(lib.puts))
        self.assertEqual(utc(2026, 9, 9, 0, 10), solar_import.cursor("02932"))
        # The next run resumes behind the cursor instead of resending the first two instants.
        self.assertEqual(2, solar_import.import_latest(utc(2026, 9, 9, 12, 0)))
        self.assertEqual([utc(2026, 9, 9, 0, 20), utc(2026, 9, 9, 0, 30)],
                         [date_time for date_time, _ in lib.puts[2:]])


class TestEndToEnd(unittest.TestCase):
    def test_a_fresh_run_publishes_the_current_day_in_order(self):
        lib = FakeLib()
        solar_import = make_import(lib)
        solar_import.seed_cursors(as_utc(lib.get_last_published_datetime()[0]))

        self.assertEqual(4, solar_import.import_latest(utc(2026, 9, 9, 12, 0)))
        self.assertEqual([utc(2026, 9, 9, 0, 0), utc(2026, 9, 9, 0, 10), utc(2026, 9, 9, 0, 20),
                          utc(2026, 9, 9, 0, 30)], [date_time for date_time, _ in lib.puts])

        first = lib.puts[0][1]
        self.assertEqual(0.0, first["global_irradiance_wm2"])
        self.assertAlmostEqual(395.0, first["longwave_wm2"], places=9)
        self.assertAlmostEqual(22.4, first["temperature_2m"], places=9)
        self.assertEqual("02932", first["meta"]["id"])
        self.assertEqual("Leipzig/Halle", first["meta"]["name"])
        self.assertEqual(2, first["meta"]["quality_level"])

        second = lib.puts[1][1]
        self.assertAlmostEqual(278.3333333333333, second["global_irradiance_wm2"], places=9)
        self.assertAlmostEqual(205.0, second["diffuse_irradiance_wm2"], places=9)
        self.assertAlmostEqual(180.0, second["sunshine_seconds"], places=9)
        self.assertAlmostEqual(16.7, second["global_irradiance_raw"], places=9)
        self.assertAlmostEqual(22.3, second["temperature_2m"], places=9)

        # Every column of that row carries DWD's missing marker, so nothing but the metadata is published.
        third = lib.puts[2][1]
        self.assertEqual(["meta"], list(third))
        self.assertEqual(1, third["meta"]["quality_level"])

        fourth = lib.puts[3][1]
        self.assertAlmostEqual(446.6666666666667, fourth["global_irradiance_wm2"], places=9)
        self.assertAlmostEqual(540.0, fourth["sunshine_seconds"], places=9)
        self.assertAlmostEqual(20.0, fourth["temperature_2m"], places=9)

    def test_a_run_after_midnight_publishes_yesterdays_tail_before_today(self):
        lib = FakeLib()
        solar_import = make_import(lib)
        solar_import.seed_cursors(utc(2026, 9, 8, 23, 40))

        self.assertEqual(5, solar_import.import_latest(utc(2026, 9, 9, 0, 35)))
        self.assertEqual([utc(2026, 9, 8, 23, 50), utc(2026, 9, 9, 0, 0), utc(2026, 9, 9, 0, 10),
                          utc(2026, 9, 9, 0, 20), utc(2026, 9, 9, 0, 30)],
                         [date_time for date_time, _ in lib.puts])
        self.assertAlmostEqual(17.5, lib.puts[0][1]["temperature_2m"], places=9)

    def test_without_temperature_no_temperature_is_published_or_fetched(self):
        lib = FakeLib()
        requested = []

        def counting_fetch(url):
            requested.append(url)
            return stub_fetch(url)

        solar_import = SolarImport(lib, [STATION], False, counting_fetch)
        solar_import.seed_cursors(None)
        self.assertEqual(4, solar_import.import_latest(utc(2026, 9, 9, 12, 0)))
        for _, value in lib.puts:
            self.assertNotIn("temperature_2m", value)
        self.assertEqual([product_url(SOLAR, NOW, "02932")], requested)

    def test_an_unavailable_station_does_not_stop_the_others(self):
        lib = FakeLib()
        solar_import = make_import(lib, [OTHER_STATION, STATION])
        solar_import.seed_cursors(None)
        with self.assertLogs("import.archive", level=logging.ERROR):
            self.assertEqual(4, solar_import.import_latest(utc(2026, 9, 9, 12, 0)))
        self.assertEqual(4, len(lib.puts))
        self.assertIsNone(solar_import.cursor("00427"))

    def test_a_raising_fetch_does_not_stop_the_others(self):
        lib = FakeLib()

        def failing_fetch(url):
            if "00427" in url:
                raise IOError("connection reset")
            return stub_fetch(url)

        solar_import = SolarImport(lib, [OTHER_STATION, STATION], True, failing_fetch)
        solar_import.seed_cursors(None)
        with self.assertLogs("import.importer", level=logging.ERROR):
            self.assertEqual(4, solar_import.import_latest(utc(2026, 9, 9, 12, 0)))
        self.assertEqual(4, len(lib.puts))

    def test_a_fetch_error_carrying_a_percent_sign_is_still_logged(self):
        # The configured logger interpolates its message against its arguments, so an exception text that looks
        # like a format string must not reach the message itself.
        lib = FakeLib()

        def failing_fetch(url):
            raise IOError("connection reset after 100%")

        solar_import = SolarImport(lib, [STATION], True, failing_fetch)
        solar_import.seed_cursors(None)
        with self.assertLogs("import.importer", level=logging.ERROR):
            self.assertEqual(0, solar_import.import_latest(utc(2026, 9, 9, 12, 0)))
        self.assertEqual([], lib.puts)


SOLAR_TAIL_ROWS = [
    "       2932;202609090000;    2;   0.0;   0.0;   0.000;  23.7;eor",
    "       2932;202609090010;    2;  12.3;  16.7;   0.050;  24.1;eor",
    "       2932;202609090020;    2;  18.0;  22.0;   0.100;  22.0;eor",
    "       2932;202609090030;    2;  20.8;  26.8;   0.150;  21.5;eor",
]

TEMPERATURE_TAIL_ROWS = [
    "       2932;202609090000;    2;  988.4;  22.4;  20.8;  55.9;  13.2;eor",
    "       2932;202609090010;    2;  988.5;  22.3;  21.0;  56.2;  13.2;eor",
    "       2932;202609090020;    2;  988.6;  22.1;  21.2;  56.5;  13.1;eor",
    "       2932;202609090030;    2;  995.4;  20.0;  22.7;  45.6;   7.9;eor",
]


class TailFetch:
    '''
    Serves the now archives of one station out of rows the test can exchange between two runs

    temperature_rows None stands for a temperature archive DWD does not answer for at all.
    '''

    def __init__(self, solar_rows: List[str], temperature_rows: Optional[List[str]]):
        self.solar_rows = solar_rows
        self.temperature_rows = temperature_rows

    def __call__(self, url: str) -> Optional[bytes]:
        if url == product_url(SOLAR, NOW, "02932"):
            return build_zip("produkt_zehn_now_sd_20260909_20260909_02932.txt", SOLAR_HEADER, self.solar_rows)
        if url == product_url(TEMPERATURE, NOW, "02932") and self.temperature_rows is not None:
            return build_zip("produkt_zehn_now_tu_20260909_20260909_02932.txt", TEMPERATURE_HEADER,
                             self.temperature_rows)
        return None


class TestHoldBackUntilTemperature(unittest.TestCase):
    def test_the_tail_beyond_the_newest_temperature_waits_for_the_next_run(self):
        lib = FakeLib()
        fetch = TailFetch(SOLAR_TAIL_ROWS, TEMPERATURE_TAIL_ROWS[:2])
        solar_import = SolarImport(lib, [STATION], True, fetch)
        solar_import.seed_cursors(None)

        self.assertEqual(2, solar_import.import_latest(utc(2026, 9, 9, 0, 40)))
        self.assertEqual([utc(2026, 9, 9, 0, 0), utc(2026, 9, 9, 0, 10)],
                         [date_time for date_time, _ in lib.puts])
        for _, value in lib.puts:
            self.assertIn("temperature_2m", value)
        self.assertEqual(utc(2026, 9, 9, 0, 10), solar_import.cursor("02932"))

        # The next run finds the temperature caught up and publishes the two instants it held back, with it.
        fetch.temperature_rows = TEMPERATURE_TAIL_ROWS
        self.assertEqual(2, solar_import.import_latest(utc(2026, 9, 9, 1, 10)))
        self.assertEqual([utc(2026, 9, 9, 0, 20), utc(2026, 9, 9, 0, 30)],
                         [date_time for date_time, _ in lib.puts[2:]])
        self.assertAlmostEqual(22.1, lib.puts[2][1]["temperature_2m"], places=9)
        self.assertAlmostEqual(20.0, lib.puts[3][1]["temperature_2m"], places=9)
        self.assertEqual(utc(2026, 9, 9, 0, 30), solar_import.cursor("02932"))

    def test_a_hole_below_the_newest_temperature_row_holds_the_instants_above_it_too(self):
        # The newest temperature row is no longer the measure: a hole anywhere stops the run there, because
        # publishing across it would step the cursor over an instant the next archive still fills.
        lib = FakeLib()
        with_hole = [TEMPERATURE_TAIL_ROWS[0]] + TEMPERATURE_TAIL_ROWS[2:]
        fetch = TailFetch(SOLAR_TAIL_ROWS, with_hole)
        solar_import = SolarImport(lib, [STATION], True, fetch)
        solar_import.seed_cursors(None)

        self.assertEqual(1, solar_import.import_latest(utc(2026, 9, 9, 0, 40)))
        self.assertEqual([utc(2026, 9, 9, 0, 0)], [date_time for date_time, _ in lib.puts])
        self.assertEqual(utc(2026, 9, 9, 0, 0), solar_import.cursor("02932"))

        # DWD fills the hole in its next refresh, so the three instants go out with their temperature.
        fetch.temperature_rows = TEMPERATURE_TAIL_ROWS
        self.assertEqual(3, solar_import.import_latest(utc(2026, 9, 9, 1, 10)))
        self.assertEqual([utc(2026, 9, 9, 0, 10), utc(2026, 9, 9, 0, 20), utc(2026, 9, 9, 0, 30)],
                         [date_time for date_time, _ in lib.puts[1:]])
        for _, value in lib.puts:
            self.assertIn("temperature_2m", value)
        self.assertEqual(utc(2026, 9, 9, 0, 30), solar_import.cursor("02932"))

    def test_a_hole_is_published_without_the_temperature_once_the_maximum_hold_has_passed(self):
        lib = FakeLib()
        with_hole = [TEMPERATURE_TAIL_ROWS[0]] + TEMPERATURE_TAIL_ROWS[2:]
        solar_import = SolarImport(lib, [STATION], True, TailFetch(SOLAR_TAIL_ROWS, with_hole))
        solar_import.seed_cursors(utc(2026, 9, 9, 0, 0))

        # A hole DWD never fills would stall the series for good, so after MAX_HOLD it goes out as unmeasured
        # and the instants above it follow with their temperature.
        with self.assertLogs("import.importer", level=logging.WARNING) as captured:
            self.assertEqual(3, solar_import.import_latest(utc(2026, 9, 9, 0, 10) + MAX_HOLD
                                                           + datetime.timedelta(minutes=1)))
        self.assertEqual([utc(2026, 9, 9, 0, 10), utc(2026, 9, 9, 0, 20), utc(2026, 9, 9, 0, 30)],
                         [date_time for date_time, _ in lib.puts])
        self.assertNotIn("temperature_2m", lib.puts[0][1])
        self.assertIn("temperature_2m", lib.puts[1][1])
        self.assertEqual(utc(2026, 9, 9, 0, 30), solar_import.cursor("02932"))
        # One line for the run, not one per instant, and no second line for a tail that is no longer waiting.
        self.assertEqual(1, len(captured.records))
        self.assertIn("Published 1 instants of station 02932 without a temperature", captured.output[0])
        self.assertIn("2026-09-09 00:10:00+00:00", captured.output[0])

    def test_an_instant_exactly_as_old_as_the_maximum_hold_is_still_held(self):
        # The bound is a hold of MAX_HOLD, so the instant goes out on the run after it, not on the one that
        # reaches it: an off by one here would publish bare a run early on every hole.
        lib = FakeLib()
        with_hole = [TEMPERATURE_TAIL_ROWS[0]] + TEMPERATURE_TAIL_ROWS[2:]
        solar_import = SolarImport(lib, [STATION], True, TailFetch(SOLAR_TAIL_ROWS, with_hole))
        solar_import.seed_cursors(utc(2026, 9, 9, 0, 0))
        self.assertEqual(0, solar_import.import_latest(utc(2026, 9, 9, 0, 10) + MAX_HOLD))
        self.assertEqual([], lib.puts)
        self.assertEqual(utc(2026, 9, 9, 0, 0), solar_import.cursor("02932"))

    def test_the_held_back_count_is_logged_with_the_oldest_instant_that_waits(self):
        lib = FakeLib()
        solar_import = SolarImport(lib, [STATION], True, TailFetch(SOLAR_TAIL_ROWS, TEMPERATURE_TAIL_ROWS[:2]))
        solar_import.seed_cursors(None)
        with self.assertLogs("import.importer", level=logging.INFO) as captured:
            self.assertEqual(2, solar_import.import_latest(utc(2026, 9, 9, 0, 40)))
        held_back = [line for line in captured.output if "Holding back" in line]
        self.assertEqual(1, len(held_back))
        self.assertIn("Holding back 2 instants of station 02932", held_back[0])
        self.assertIn("2026-09-09 00:20:00+00:00", held_back[0])

    def test_a_missing_temperature_archive_holds_everything_back_and_logs_the_hold_once(self):
        lib = FakeLib()
        solar_import = SolarImport(lib, [STATION], True, TailFetch(SOLAR_TAIL_ROWS, None))
        solar_import.seed_cursors(None)
        with self.assertLogs("import.archive", level=logging.ERROR) as fetch_errors:
            with self.assertLogs("import.importer", level=logging.INFO) as captured:
                self.assertEqual(0, solar_import.import_latest(utc(2026, 9, 9, 0, 40)))
        # One line per run on each side: the fetch reports the missing archive, the importer the held back tail.
        self.assertEqual(1, len(fetch_errors.records))
        self.assertEqual([], lib.puts)
        self.assertIsNone(solar_import.cursor("02932"))
        holds = [line for line in captured.output if "Holding back" in line]
        self.assertEqual(1, len(holds))
        self.assertIn("Holding back 4 instants of station 02932", holds[0])
        self.assertIn("2026-09-09 00:00:00+00:00", holds[0])

    def test_a_temperature_archive_that_stays_away_lets_the_tail_out_bare_after_the_maximum_hold(self):
        lib = FakeLib()
        solar_import = SolarImport(lib, [STATION], True, TailFetch(SOLAR_TAIL_ROWS, None))
        solar_import.seed_cursors(None)
        with self.assertLogs("import.archive", level=logging.ERROR):
            with self.assertLogs("import.importer", level=logging.WARNING) as captured:
                self.assertEqual(4, solar_import.import_latest(utc(2026, 9, 9, 0, 30) + MAX_HOLD
                                                               + datetime.timedelta(minutes=1)))
        for _, value in lib.puts:
            self.assertNotIn("temperature_2m", value)
        self.assertEqual(utc(2026, 9, 9, 0, 30), solar_import.cursor("02932"))
        self.assertEqual(1, len(captured.records))
        self.assertIn("Published 4 instants of station 02932 without a temperature", captured.output[0])

    def test_a_temperature_archive_without_rows_holds_everything_back_too(self):
        lib = FakeLib()
        solar_import = SolarImport(lib, [STATION], True, TailFetch(SOLAR_TAIL_ROWS, []))
        solar_import.seed_cursors(utc(2026, 9, 9, 0, 10))
        with self.assertLogs("import.importer", level=logging.INFO) as captured:
            self.assertEqual(0, solar_import.import_latest(utc(2026, 9, 9, 0, 40)))
        self.assertEqual([], lib.puts)
        self.assertEqual(utc(2026, 9, 9, 0, 10), solar_import.cursor("02932"))
        # Only the two instants above the cursor are waiting; what was published before is not counted again.
        holds = [line for line in captured.output if "Holding back" in line]
        self.assertIn("Holding back 2 instants of station 02932", holds[0])
        self.assertIn("2026-09-09 00:20:00+00:00", holds[0])

    def test_without_temperature_the_whole_solar_tail_is_published(self):
        lib = FakeLib()
        solar_import = SolarImport(lib, [STATION], False, TailFetch(SOLAR_TAIL_ROWS, None))
        solar_import.seed_cursors(None)
        self.assertEqual(4, solar_import.import_latest(utc(2026, 9, 9, 0, 40)))
        self.assertEqual(utc(2026, 9, 9, 0, 30), solar_import.cursor("02932"))
        for _, value in lib.puts:
            self.assertNotIn("temperature_2m", value)

    def test_a_missing_temperature_value_inside_the_covered_range_is_published_as_delivered(self):
        lib = FakeLib()
        temperature_rows = list(TEMPERATURE_TAIL_ROWS)
        temperature_rows[1] = "       2932;202609090010;    2;  988.5;  -999;  21.0;  56.2;  13.2;eor"
        solar_import = SolarImport(lib, [STATION], True, TailFetch(SOLAR_TAIL_ROWS, temperature_rows))
        solar_import.seed_cursors(None)
        # An instant DWD did not measure is not the same as one it has not published yet: it is covered right
        # away, well inside MAX_HOLD, and does not hold the instants above it back.
        self.assertEqual(4, solar_import.import_latest(utc(2026, 9, 9, 0, 40)))
        self.assertNotIn("temperature_2m", lib.puts[1][1])
        # The neighbouring instants keep the temperature they were measured with.
        self.assertAlmostEqual(22.4, lib.puts[0][1]["temperature_2m"], places=9)
        self.assertAlmostEqual(22.1, lib.puts[2][1]["temperature_2m"], places=9)

    def test_a_trailing_missing_temperature_value_still_counts_as_covered(self):
        lib = FakeLib()
        temperature_rows = list(TEMPERATURE_TAIL_ROWS)
        temperature_rows[3] = "       2932;202609090030;    2;  995.4;  -999;  22.7;  45.6;   7.9;eor"
        solar_import = SolarImport(lib, [STATION], True, TailFetch(SOLAR_TAIL_ROWS, temperature_rows))
        solar_import.seed_cursors(None)
        # A row is a row, measured or marked, so a station whose sensor delivers only the marker does not stall
        # the solar series until MAX_HOLD every single step.
        self.assertEqual(4, solar_import.import_latest(utc(2026, 9, 9, 0, 40)))
        self.assertNotIn("temperature_2m", lib.puts[3][1])
        self.assertEqual(utc(2026, 9, 9, 0, 30), solar_import.cursor("02932"))


def solar_row(stamp: str) -> str:
    return "       2932;" + stamp + ";    2;   1.0;   2.0;   0.000;  20.0;eor"


def temperature_row(stamp: str) -> str:
    return "       2932;" + stamp + ";    2;  990.0;  15.0;  14.0;  70.0;   9.0;eor"


class RollingFeed:
    '''
    Serves the now and recent archives of one station out of rows the test exchanges between runs

    An archive whose rows are not set stands for one DWD does not answer for.
    '''

    def __init__(self, rows: Dict[str, List[str]] = None):
        self.rows: Dict[str, List[str]] = rows or {}

    def __call__(self, url: str) -> Optional[bytes]:
        if url not in self.rows:
            return None
        header = TEMPERATURE_HEADER if "_TU_" in url else SOLAR_HEADER
        return build_zip("produkt_zehn_02932.txt", header, self.rows[url])


SOLAR_NOW_URL = product_url(SOLAR, NOW, "02932")
SOLAR_RECENT_URL = product_url(SOLAR, RECENT, "02932")
TEMPERATURE_NOW_URL = product_url(TEMPERATURE, NOW, "02932")
TEMPERATURE_RECENT_URL = product_url(TEMPERATURE, RECENT, "02932")


class TestCoveredUntil(unittest.TestCase):
    def test_a_run_that_did_not_read_the_recent_archive_relies_on_everything_it_has(self):
        instants = [utc(2026, 9, 9, 0, 0), utc(2026, 9, 9, 0, 10)]
        self.assertEqual(utc(2026, 9, 9, 0, 10), covered_until(instants, None))

    def test_a_product_without_instants_covers_nothing(self):
        self.assertIsNone(covered_until([], None))
        self.assertIsNone(covered_until([], [utc(2026, 9, 8, 23, 50)]))

    def test_a_recent_archive_without_instants_covers_nothing(self):
        # Its reach is unknown, so there is no instant the run could prove nothing is missing below.
        self.assertIsNone(covered_until([utc(2026, 9, 9, 0, 0)], []))

    def test_the_now_archive_of_the_day_after_the_recent_one_is_contiguous(self):
        recent = [utc(2026, 9, 8, 23, 40), utc(2026, 9, 8, 23, 50)]
        instants = recent + [utc(2026, 9, 9, 0, 0), utc(2026, 9, 9, 0, 10)]
        self.assertEqual(utc(2026, 9, 9, 0, 10), covered_until(instants, recent))

    def test_a_now_archive_two_days_above_the_recent_one_stops_at_the_recent_one(self):
        # The midnight seam: the now archive rolled to the new day before the recent archive was regenerated,
        # so the tail of the previous day is in no archive.
        recent = [utc(2026, 9, 8, 23, 50)]
        instants = recent + [utc(2026, 9, 10, 0, 0), utc(2026, 9, 10, 0, 10)]
        self.assertEqual(utc(2026, 9, 8, 23, 50), covered_until(instants, recent))

    def test_an_absent_last_row_of_the_recent_archive_is_not_a_gap(self):
        # Days are compared, not steps: a missing 23:50 row would otherwise stall the series for a whole day.
        recent = [utc(2026, 9, 8, 23, 30)]
        instants = recent + [utc(2026, 9, 9, 0, 0)]
        self.assertEqual(utc(2026, 9, 9, 0, 0), covered_until(instants, recent))

    def test_a_run_holding_only_recent_instants_covers_their_newest(self):
        recent = [utc(2026, 9, 8, 23, 40), utc(2026, 9, 8, 23, 50)]
        self.assertEqual(utc(2026, 9, 8, 23, 50), covered_until(recent, recent))


class TestMaximumHold(unittest.TestCase):
    def test_the_hold_is_bounded_at_two_hours(self):
        # The tests around it are written against the constant, so the promise the README makes about the
        # latency of the export is pinned here and nowhere else.
        self.assertEqual(datetime.timedelta(hours=2), MAX_HOLD)


class TestMidnightSeam(unittest.TestCase):
    def test_the_tail_of_the_previous_day_waits_for_the_regenerated_recent_archive(self):
        lib = FakeLib()
        feed = RollingFeed({
            SOLAR_NOW_URL: [solar_row("202609092330"), solar_row("202609092340"), solar_row("202609092350")],
            TEMPERATURE_NOW_URL: [temperature_row("202609092330"), temperature_row("202609092340")],
            # The recent archives were regenerated at 2026-09-09 01:15Z, so they end on 2026-09-08.
            SOLAR_RECENT_URL: [solar_row("202609082350")],
            TEMPERATURE_RECENT_URL: [temperature_row("202609082350")],
        })
        solar_import = SolarImport(lib, [STATION], True, feed)
        solar_import.seed_cursors(utc(2026, 9, 9, 23, 20))

        # 00:35Z: the now archives still serve the previous day, the solar one a step ahead of the temperature.
        self.assertEqual(2, solar_import.import_latest(utc(2026, 9, 10, 0, 35)))
        self.assertEqual(utc(2026, 9, 9, 23, 40), solar_import.cursor("02932"))

        # 01:05Z: the now archives rolled over to the new day and the recent ones are not regenerated yet, so
        # 23:50 of the previous day is in no archive and nothing may be published past it.
        feed.rows[SOLAR_NOW_URL] = [solar_row("202609100000"), solar_row("202609100010")]
        feed.rows[TEMPERATURE_NOW_URL] = [temperature_row("202609100000"), temperature_row("202609100010")]
        with self.assertLogs("import.importer", level=logging.INFO) as captured:
            self.assertEqual(0, solar_import.import_latest(utc(2026, 9, 10, 1, 5)))
        self.assertEqual(utc(2026, 9, 9, 23, 40), solar_import.cursor("02932"))
        waiting = [line for line in captured.output if "Holding back" in line]
        self.assertEqual(1, len(waiting))
        self.assertIn("Holding back 2 instants of station 02932", waiting[0])
        self.assertIn("2026-09-10 00:00:00+00:00", waiting[0])

        # 01:35Z: the recent archives carry the previous day, so its tail goes out before the new day's rows.
        feed.rows[SOLAR_RECENT_URL] = [solar_row("202609092340"), solar_row("202609092350")]
        feed.rows[TEMPERATURE_RECENT_URL] = [temperature_row("202609092340"), temperature_row("202609092350")]
        self.assertEqual(3, solar_import.import_latest(utc(2026, 9, 10, 1, 35)))
        self.assertEqual([utc(2026, 9, 9, 23, 50), utc(2026, 9, 10, 0, 0), utc(2026, 9, 10, 0, 10)],
                         [date_time for date_time, _ in lib.puts[2:]])
        self.assertEqual(utc(2026, 9, 10, 0, 10), solar_import.cursor("02932"))
        for _, value in lib.puts:
            self.assertIn("temperature_2m", value)

    def test_the_seam_holds_the_tail_back_without_the_temperature_too(self):
        # The gap is in the solar archives themselves, so WITH_TEMPERATURE off does not make it safe.
        lib = FakeLib()
        feed = RollingFeed({
            SOLAR_NOW_URL: [solar_row("202609100000"), solar_row("202609100010")],
            SOLAR_RECENT_URL: [solar_row("202609082350")],
        })
        solar_import = SolarImport(lib, [STATION], False, feed)
        solar_import.seed_cursors(utc(2026, 9, 9, 23, 40))
        with self.assertLogs("import.importer", level=logging.INFO):
            self.assertEqual(0, solar_import.import_latest(utc(2026, 9, 10, 1, 5)))
        self.assertEqual(utc(2026, 9, 9, 23, 40), solar_import.cursor("02932"))

        feed.rows[SOLAR_RECENT_URL] = [solar_row("202609092350")]
        self.assertEqual(3, solar_import.import_latest(utc(2026, 9, 10, 1, 35)))
        self.assertEqual([utc(2026, 9, 9, 23, 50), utc(2026, 9, 10, 0, 0), utc(2026, 9, 10, 0, 10)],
                         [date_time for date_time, _ in lib.puts])

    def test_a_cursor_from_yesterday_is_caught_up_in_one_run_and_keeps_going(self):
        # The recent archive ends where the now archive begins, so there is no gap to wait for. A rule that
        # capped every run at the recent archive would stall the import here for a whole day.
        lib = FakeLib()
        feed = RollingFeed({
            SOLAR_RECENT_URL: [solar_row("202609092340"), solar_row("202609092350")],
            TEMPERATURE_RECENT_URL: [temperature_row("202609092340"), temperature_row("202609092350")],
            SOLAR_NOW_URL: [solar_row("202609100000"), solar_row("202609100010")],
            TEMPERATURE_NOW_URL: [temperature_row("202609100000"), temperature_row("202609100010")],
        })
        solar_import = SolarImport(lib, [STATION], True, feed)
        solar_import.seed_cursors(utc(2026, 9, 9, 23, 30))

        self.assertEqual(4, solar_import.import_latest(utc(2026, 9, 10, 2, 0)))
        self.assertEqual(utc(2026, 9, 10, 0, 10), solar_import.cursor("02932"))

        feed.rows[SOLAR_NOW_URL] = feed.rows[SOLAR_NOW_URL] + [solar_row("202609100020")]
        feed.rows[TEMPERATURE_NOW_URL] = feed.rows[TEMPERATURE_NOW_URL] + [temperature_row("202609100020")]
        self.assertEqual(1, solar_import.import_latest(utc(2026, 9, 10, 2, 30)))
        self.assertEqual(utc(2026, 9, 10, 0, 20), solar_import.cursor("02932"))

    def test_an_unavailable_recent_archive_holds_the_run_back_until_it_answers(self):
        # Its reach is unknown, so this run cannot tell whether the tail of the previous day is missing; the
        # fetch reports the outage every run and the cursor waits instead of stepping over the gap.
        lib = FakeLib()
        feed = RollingFeed({
            SOLAR_NOW_URL: [solar_row("202609100000"), solar_row("202609100010")],
            TEMPERATURE_NOW_URL: [temperature_row("202609100000"), temperature_row("202609100010")],
        })
        solar_import = SolarImport(lib, [STATION], True, feed)
        solar_import.seed_cursors(utc(2026, 9, 9, 23, 40))
        with self.assertLogs("import.archive", level=logging.ERROR) as fetch_errors:
            with self.assertLogs("import.importer", level=logging.INFO):
                self.assertEqual(0, solar_import.import_latest(utc(2026, 9, 10, 1, 5)))
        self.assertEqual(2, len(fetch_errors.records))
        self.assertEqual([], lib.puts)
        self.assertEqual(utc(2026, 9, 9, 23, 40), solar_import.cursor("02932"))

        feed.rows[SOLAR_RECENT_URL] = [solar_row("202609092350")]
        feed.rows[TEMPERATURE_RECENT_URL] = [temperature_row("202609092350")]
        self.assertEqual(3, solar_import.import_latest(utc(2026, 9, 10, 1, 35)))
        self.assertEqual(utc(2026, 9, 10, 0, 10), solar_import.cursor("02932"))

    def test_a_station_returning_after_an_outage_waits_for_the_next_regeneration(self):
        # From the archives alone the days a station skipped look exactly like the seam, so the run waits here
        # too: a day of latency for a station coming back, rather than a permanent hole every night.
        lib = FakeLib()
        feed = RollingFeed({
            SOLAR_RECENT_URL: [solar_row("202609072350")],
            TEMPERATURE_RECENT_URL: [temperature_row("202609072350")],
            SOLAR_NOW_URL: [solar_row("202609100800")],
            TEMPERATURE_NOW_URL: [temperature_row("202609100800")],
        })
        solar_import = SolarImport(lib, [STATION], True, feed)
        solar_import.seed_cursors(utc(2026, 9, 7, 23, 50))
        with self.assertLogs("import.importer", level=logging.INFO):
            self.assertEqual(0, solar_import.import_latest(utc(2026, 9, 10, 9, 0)))
        self.assertEqual(utc(2026, 9, 7, 23, 50), solar_import.cursor("02932"))

        # The next regeneration carries the day the station came back on, and the run catches up in one go.
        feed.rows[SOLAR_RECENT_URL] = [solar_row("202609072350"), solar_row("202609100800")]
        feed.rows[TEMPERATURE_RECENT_URL] = [temperature_row("202609072350"), temperature_row("202609100800")]
        feed.rows[SOLAR_NOW_URL] = [solar_row("202609110000")]
        feed.rows[TEMPERATURE_NOW_URL] = [temperature_row("202609110000")]
        self.assertEqual(2, solar_import.import_latest(utc(2026, 9, 11, 2, 0)))
        self.assertEqual([utc(2026, 9, 10, 8, 0), utc(2026, 9, 11, 0, 0)],
                         [date_time for date_time, _ in lib.puts])

    def test_an_out_of_step_recent_regeneration_waits_instead_of_publishing_bare(self):
        # The solar recent archive is regenerated, the temperature one is still the previous generation. The
        # solar side has no gap, so only the temperature holds the tail: it waits rather than going out bare.
        lib = FakeLib()
        feed = RollingFeed({
            SOLAR_RECENT_URL: [solar_row("202609092340"), solar_row("202609092350")],
            TEMPERATURE_RECENT_URL: [temperature_row("202609082340"), temperature_row("202609082350")],
            SOLAR_NOW_URL: [solar_row("202609100000"), solar_row("202609100010")],
            TEMPERATURE_NOW_URL: [temperature_row("202609100000"), temperature_row("202609100010")],
        })
        solar_import = SolarImport(lib, [STATION], True, feed)
        solar_import.seed_cursors(utc(2026, 9, 9, 23, 40))
        with self.assertLogs("import.importer", level=logging.INFO) as captured:
            self.assertEqual(0, solar_import.import_latest(utc(2026, 9, 10, 1, 35)))
        self.assertEqual([], lib.puts)
        self.assertEqual(utc(2026, 9, 9, 23, 40), solar_import.cursor("02932"))
        holds = [line for line in captured.output if "Holding back" in line]
        self.assertEqual(1, len(holds))
        self.assertIn("2026-09-09 23:50:00+00:00", holds[0])

        # The next generation of the temperature archive carries the day, and the run catches up with it.
        feed.rows[TEMPERATURE_RECENT_URL] = [temperature_row("202609092340"), temperature_row("202609092350")]
        self.assertEqual(3, solar_import.import_latest(utc(2026, 9, 10, 2, 5)))
        self.assertEqual([utc(2026, 9, 9, 23, 50), utc(2026, 9, 10, 0, 0), utc(2026, 9, 10, 0, 10)],
                         [date_time for date_time, _ in lib.puts])
        for _, value in lib.puts:
            self.assertIn("temperature_2m", value)

    def test_a_seam_lasting_longer_than_the_maximum_hold_warns_once(self):
        # MAX_HOLD does not lift the seam, because the instants behind it are in no archive of this run: the
        # run says so once, naming the oldest instant that waits, instead of once per instant.
        lib = FakeLib()
        feed = RollingFeed({
            SOLAR_NOW_URL: [solar_row("202609100000"), solar_row("202609100010")],
            TEMPERATURE_NOW_URL: [temperature_row("202609100000"), temperature_row("202609100010")],
        })
        solar_import = SolarImport(lib, [STATION], True, feed)
        solar_import.seed_cursors(utc(2026, 9, 9, 23, 40))
        with self.assertLogs("import.archive", level=logging.ERROR):
            with self.assertLogs("import.importer", level=logging.WARNING) as captured:
                self.assertEqual(0, solar_import.import_latest(utc(2026, 9, 10, 3, 0)))
        self.assertEqual([], lib.puts)
        self.assertEqual(utc(2026, 9, 9, 23, 40), solar_import.cursor("02932"))
        self.assertEqual(1, len(captured.records))
        self.assertIn("Station 02932 has 2 instants waiting longer than", captured.output[0])
        self.assertIn("2026-09-10 00:00:00+00:00", captured.output[0])

    def test_a_normal_day_reads_the_now_archive_alone_and_publishes_its_tail(self):
        lib = FakeLib()
        requested = []

        def counting_fetch(url):
            requested.append(url)
            return RollingFeed({
                SOLAR_NOW_URL: [solar_row("202609100000"), solar_row("202609100010")],
                TEMPERATURE_NOW_URL: [temperature_row("202609100000"), temperature_row("202609100010")],
            })(url)

        solar_import = SolarImport(lib, [STATION], True, counting_fetch)
        solar_import.seed_cursors(utc(2026, 9, 10, 0, 0))
        self.assertEqual(1, solar_import.import_latest(utc(2026, 9, 10, 12, 0)))
        self.assertEqual([utc(2026, 9, 10, 0, 10)], [date_time for date_time, _ in lib.puts])
        self.assertEqual([SOLAR_NOW_URL, TEMPERATURE_NOW_URL], requested)


OTHER_SOLAR_RECENT_URL = product_url(SOLAR, RECENT, "00427")
OTHER_TEMPERATURE_RECENT_URL = product_url(TEMPERATURE, RECENT, "00427")
OTHER_SOLAR_NOW_URL = product_url(SOLAR, NOW, "00427")
OTHER_TEMPERATURE_NOW_URL = product_url(TEMPERATURE, NOW, "00427")


def backfill_feed() -> RollingFeed:
    '''Both stations have a recent and a now archive; only the temperature recent one of 02932 is empty.'''
    return RollingFeed({
        SOLAR_RECENT_URL: [solar_row("202609092340"), solar_row("202609092350")],
        TEMPERATURE_RECENT_URL: [],
        SOLAR_NOW_URL: [solar_row("202609100000"), solar_row("202609100010")],
        TEMPERATURE_NOW_URL: [temperature_row("202609100000"), temperature_row("202609100010")],
        OTHER_SOLAR_RECENT_URL: [solar_row("202609092340"), solar_row("202609092350")],
        OTHER_TEMPERATURE_RECENT_URL: [temperature_row("202609092340"), temperature_row("202609092350")],
        OTHER_SOLAR_NOW_URL: [solar_row("202609100000")],
        OTHER_TEMPERATURE_NOW_URL: [temperature_row("202609100000")],
    })


class TestRecentBackfill(unittest.TestCase):
    def test_a_temperature_archive_without_rows_leaves_the_backfill_pending(self):
        # Publishing the closed days of the backfill without temperature is not an option: they are past
        # MAX_HOLD from the start and no later run reads their archives again, so the cursor waits instead.
        lib = FakeLib()
        solar_import = SolarImport(lib, [STATION], True, backfill_feed())
        solar_import.seed_cursors(None)
        with self.assertLogs("import.importer", level=logging.ERROR) as captured:
            self.assertFalse(solar_import.import_recent(STATION, utc(2026, 9, 10, 8, 0)))
        self.assertEqual([], lib.puts)
        self.assertIsNone(solar_import.cursor("02932"))
        self.assertTrue(solar_import.backfill_pending("02932"))
        errors = [line for line in captured.output if line.startswith("ERROR")]
        self.assertEqual(1, len(errors))
        self.assertIn("recent air temperature archive of station 02932", errors[0])

    def test_a_missing_temperature_archive_leaves_the_backfill_pending_too(self):
        lib = FakeLib()
        feed = backfill_feed()
        del feed.rows[TEMPERATURE_RECENT_URL]
        solar_import = SolarImport(lib, [STATION], True, feed)
        solar_import.seed_cursors(None)
        with self.assertLogs("import.archive", level=logging.ERROR):
            with self.assertLogs("import.importer", level=logging.ERROR):
                self.assertFalse(solar_import.import_recent(STATION, utc(2026, 9, 10, 8, 0)))
        self.assertEqual([], lib.puts)
        self.assertTrue(solar_import.backfill_pending("02932"))

    def test_a_pending_backfill_is_retried_by_the_next_scheduled_run(self):
        lib = FakeLib()
        feed = backfill_feed()
        requested = []

        def counting_fetch(url):
            requested.append(url)
            return feed(url)

        solar_import = SolarImport(lib, [STATION], True, counting_fetch)
        solar_import.seed_cursors(None)
        with self.assertLogs("import.importer", level=logging.ERROR):
            self.assertFalse(solar_import.import_recent(STATION, utc(2026, 9, 10, 8, 0)))

        # The scheduled run retries the backfill and leaves the now archive alone while it is still pending,
        # because publishing the current day would step the cursor over the days the backfill owes.
        requested.clear()
        with self.assertLogs("import.importer", level=logging.ERROR):
            self.assertEqual(0, solar_import.import_latest(utc(2026, 9, 10, 8, 30)))
        self.assertEqual([], lib.puts)
        self.assertNotIn(SOLAR_NOW_URL, requested)
        self.assertTrue(solar_import.backfill_pending("02932"))

        # DWD answers, so the same scheduled run shape completes the backfill and goes on to the now archive.
        feed.rows[TEMPERATURE_RECENT_URL] = [temperature_row("202609092340"), temperature_row("202609092350")]
        self.assertEqual(4, solar_import.import_latest(utc(2026, 9, 10, 9, 0)))
        self.assertEqual([utc(2026, 9, 9, 23, 40), utc(2026, 9, 9, 23, 50), utc(2026, 9, 10, 0, 0),
                          utc(2026, 9, 10, 0, 10)], [date_time for date_time, _ in lib.puts])
        for _, value in lib.puts:
            self.assertIn("temperature_2m", value)
        self.assertFalse(solar_import.backfill_pending("02932"))
        self.assertEqual(utc(2026, 9, 10, 0, 10), solar_import.cursor("02932"))

    def test_a_pending_backfill_of_one_station_does_not_stop_another(self):
        lib = FakeLib()
        solar_import = SolarImport(lib, [STATION, OTHER_STATION], True, backfill_feed())
        solar_import.seed_cursors(None)
        with self.assertLogs("import.importer", level=logging.ERROR):
            self.assertFalse(solar_import.import_recent(STATION, utc(2026, 9, 10, 8, 0)))
            self.assertTrue(solar_import.import_recent(OTHER_STATION, utc(2026, 9, 10, 8, 0)))
            self.assertEqual(1, solar_import.import_latest(utc(2026, 9, 10, 8, 30)))
        self.assertEqual([utc(2026, 9, 9, 23, 40), utc(2026, 9, 9, 23, 50), utc(2026, 9, 10, 0, 0)],
                         [date_time for date_time, _ in lib.puts])
        self.assertIsNone(solar_import.cursor("02932"))
        self.assertEqual(utc(2026, 9, 10, 0, 0), solar_import.cursor("00427"))

    def test_an_unavailable_temperature_archive_is_no_concern_without_temperature(self):
        lib = FakeLib()

        def fetch(url):
            if url == TEMPERATURE_RECENT_URL:
                return None
            return stub_fetch(url)

        solar_import = SolarImport(lib, [STATION], False, fetch)
        solar_import.seed_cursors(None)
        self.assertTrue(solar_import.import_recent(STATION, utc(2026, 9, 9, 0, 35)))
        self.assertEqual(3, len(lib.puts))
        self.assertFalse(solar_import.backfill_pending("02932"))


class RecordingLogger:
    '''Stands in for the module logger, recording what the publish loop asks of it.'''

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.debugs = []

    def isEnabledFor(self, level) -> bool:
        return self.enabled

    def debug(self, *args):
        self.debugs.append(args)

    def info(self, *args):
        pass

    def warning(self, *args):
        pass

    def error(self, *args):
        pass


class TestDebugLine(unittest.TestCase):
    def publish_with(self, recording: RecordingLogger) -> None:
        saved = importer._logger
        importer._logger = recording
        try:
            solar_import = make_import(FakeLib())
            solar_import.seed_cursors(None)
            self.assertEqual(4, solar_import.import_latest(utc(2026, 9, 9, 12, 0)))
        finally:
            importer._logger = saved

    def test_no_line_is_built_per_datapoint_while_debug_is_off(self):
        # The logger formats and serialises its line before the level check, which a run would otherwise pay for
        # every row it publishes.
        recording = RecordingLogger(False)
        self.publish_with(recording)
        self.assertEqual([], recording.debugs)

    def test_the_line_leaves_its_values_to_the_logger_while_debug_is_on(self):
        recording = RecordingLogger(True)
        self.publish_with(recording)
        self.assertEqual(4, len(recording.debugs))
        # Interpolated by the logger, not concatenated into the message: a value carrying a percent sign in its
        # text would otherwise be read as a format string.
        for message, instant, value in recording.debugs:
            self.assertEqual("%s: %s", message)
            self.assertIsInstance(instant, datetime.datetime)
            self.assertIn("meta", value)


class TestModuleLoggers(unittest.TestCase):
    def test_every_module_logs_under_the_logger_import_lib_configures(self):
        # Only the logger named import carries import-lib's handler, so a module logging outside of it stays
        # out of the pod log.
        for module, expected in ((importer, "import.importer"), (archive, "import.archive"),
                                 (rows, "import.rows"), (stations, "import.stations")):
            self.assertEqual(expected, module.log().name)
            self.assertEqual("import", module.log().parent.name)

    def test_a_module_builds_its_logger_on_first_use_not_at_import(self):
        # import-lib puts its static fields on the logger in ImportLib(), which main.py runs after importing
        # these modules, and getChild copies them once: a logger built at import time carries none of them.
        for module in (importer, archive, rows, stations):
            try:
                with mock.patch("import_lib.import_lib.get_logger") as get_logger:
                    importlib.reload(module)
                    self.assertEqual(0, get_logger.call_count, module.__name__)
                    module.log()
                    module.log()
                    self.assertEqual(1, get_logger.call_count, module.__name__)
            finally:
                # Back to the real logger whatever the assertions did: the reload drops the cached mock.
                importlib.reload(module)


SOLAR_HISTORICAL_ROWS = [
    "       2932;202512310000;    2;   5.0;   6.0;   0.000;  40.0;eor",
    "       2932;202512310010;    2;   7.0;   8.0;   0.000;  41.0;eor",
]

OLDER_SOLAR_HISTORICAL_ROWS = [
    "       2932;201912310000;    2;   9.0;  12.0;   0.000;  50.0;eor",
]

TEMPERATURE_HISTORICAL_ROWS = [
    "       2932;202512310000;    2;  990.0;   1.5;   1.0;  80.0;  -1.0;eor",
]

HISTORICAL_INDEX = (
    '<a href="10minutenwerte_SOLAR_02932_20100101_20191231_hist.zip">a</a>'
    '<a href="10minutenwerte_SOLAR_02932_20200101_20251231_hist.zip">b</a>'
    '<a href="10minutenwerte_SOLAR_00427_19910101_19991231_hist.zip">c</a>'
)

TEMPERATURE_INDEX = '<a href="10minutenwerte_TU_02932_20200101_20251231_hist.zip">d</a>'

HISTORICAL_ARCHIVES = {
    BASE_URL + "solar/historical/": HISTORICAL_INDEX.encode(),
    BASE_URL + "air_temperature/historical/": TEMPERATURE_INDEX.encode(),
    historical_url(SOLAR, "02932", "20100101", "20191231"):
        build_zip("produkt_zehn_min_sd_20100101_20191231_02932.txt", SOLAR_HEADER,
                  OLDER_SOLAR_HISTORICAL_ROWS),
    historical_url(SOLAR, "02932", "20200101", "20251231"):
        build_zip("produkt_zehn_min_sd_20200101_20251231_02932.txt", SOLAR_HEADER, SOLAR_HISTORICAL_ROWS),
    historical_url(TEMPERATURE, "02932", "20200101", "20251231"):
        build_zip("produkt_zehn_min_tu_20200101_20251231_02932.txt", TEMPERATURE_HEADER,
                  TEMPERATURE_HISTORICAL_ROWS),
}


def historical_fetch(url: str) -> Optional[bytes]:
    if url in HISTORICAL_ARCHIVES:
        return HISTORICAL_ARCHIVES[url]
    return stub_fetch(url)


class TestArchive(unittest.TestCase):
    def test_builds_the_now_and_recent_urls(self):
        self.assertEqual(BASE_URL + "solar/now/10minutenwerte_SOLAR_02932_now.zip",
                         product_url(SOLAR, NOW, "02932"))
        self.assertEqual(BASE_URL + "solar/recent/10minutenwerte_SOLAR_02932_akt.zip",
                         product_url(SOLAR, RECENT, "02932"))
        self.assertEqual(BASE_URL + "air_temperature/now/10minutenwerte_TU_02932_now.zip",
                         product_url(TEMPERATURE, NOW, "02932"))

    def test_builds_the_historical_url(self):
        self.assertEqual(
            BASE_URL + "solar/historical/10minutenwerte_SOLAR_02932_20200101_20251231_hist.zip",
            historical_url(SOLAR, "02932", "20200101", "20251231"))

    def test_reads_only_the_product_member_of_an_archive(self):
        texts = read_products(ARCHIVES[product_url(SOLAR, NOW, "02932")])
        self.assertEqual(1, len(texts))
        self.assertTrue(texts[0].startswith("STATIONS_ID;MESS_DATUM;QN;"))

    def test_an_archive_without_a_product_file_is_reported_and_yields_nothing(self):
        # The member name carries a percent sign on purpose: it reaches the logger as an argument, so nothing
        # of it is read as a format string.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as broken:
            broken.writestr("Metadaten_100%_02932.txt", "not an observation file")
        with self.assertLogs("import.archive", level=logging.ERROR):
            self.assertEqual([], read_products(buffer.getvalue()))

    def test_lists_the_historical_blocks_of_the_requested_station_only(self):
        blocks = fetch_historical_blocks(SOLAR, "02932", historical_fetch)
        self.assertEqual([(datetime.date(2010, 1, 1), datetime.date(2019, 12, 31)),
                          (datetime.date(2020, 1, 1), datetime.date(2025, 12, 31))],
                         [(first, last) for first, last, _ in blocks])

    def test_picks_the_blocks_overlapping_a_window(self):
        blocks = fetch_historical_blocks(SOLAR, "02932", historical_fetch)
        self.assertEqual([historical_url(SOLAR, "02932", "20200101", "20251231")],
                         blocks_overlapping(blocks, datetime.date(2025, 12, 31), datetime.date(2025, 12, 31)))
        self.assertEqual(2, len(blocks_overlapping(blocks, datetime.date(2019, 12, 31),
                                                   datetime.date(2020, 1, 1))))


class TestHistorical(unittest.TestCase):
    def test_publishes_the_blocks_oldest_first_with_temperature_joined(self):
        lib = FakeLib()
        solar_import = SolarImport(lib, [STATION], True, historical_fetch)
        solar_import.seed_cursors(None)

        # The older block has to come first: the cursor only ever moves forward, so a block published out of
        # order would drop everything before it.
        self.assertEqual(3, solar_import.import_historical(STATION))
        self.assertEqual([utc(2019, 12, 31, 0, 0), utc(2025, 12, 31, 0, 0), utc(2025, 12, 31, 0, 10)],
                         [date_time for date_time, _ in lib.puts])
        self.assertAlmostEqual(200.0, lib.puts[0][1]["global_irradiance_wm2"], places=9)
        self.assertNotIn("temperature_2m", lib.puts[0][1])
        self.assertAlmostEqual(1.5, lib.puts[1][1]["temperature_2m"], places=9)
        self.assertNotIn("temperature_2m", lib.puts[2][1])
        self.assertAlmostEqual(100.0, lib.puts[1][1]["global_irradiance_wm2"], places=9)

    def test_a_cursor_inside_a_block_keeps_the_newer_rows_only(self):
        lib = FakeLib()
        solar_import = SolarImport(lib, [STATION], True, historical_fetch)
        solar_import.seed_cursors(utc(2025, 12, 31, 0, 0))
        self.assertEqual(1, solar_import.import_historical(STATION))
        self.assertEqual([utc(2025, 12, 31, 0, 10)], [date_time for date_time, _ in lib.puts])


if __name__ == "__main__":
    unittest.main()
