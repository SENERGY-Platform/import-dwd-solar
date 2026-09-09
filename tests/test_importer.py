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
import io
import logging
import unittest
import zipfile
from typing import Dict, List, Optional, Tuple

from lib.dwd.archive import BASE_URL, NOW, RECENT, SOLAR, TEMPERATURE, blocks_overlapping, \
    fetch_historical_blocks, historical_url, product_url, read_products
from lib.dwd.importer import NOW_REACH, RECENT_REACH, SolarImport, as_utc, start_of_day
from lib.dwd.rows import MISSING
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
    def test_only_instants_strictly_newer_than_the_cursor_are_published(self):
        lib = FakeLib()
        solar_import = make_import(lib)
        solar_import.seed_cursors(utc(2026, 9, 8, 23, 40))
        self.assertEqual(2, solar_import.import_recent(STATION))
        self.assertEqual([utc(2026, 9, 8, 23, 50), utc(2026, 9, 9, 0, 0)],
                         [date_time for date_time, _ in lib.puts])

    def test_an_instant_equal_to_the_cursor_is_not_republished(self):
        lib = FakeLib()
        solar_import = make_import(lib)
        solar_import.seed_cursors(utc(2026, 9, 9, 0, 0))
        self.assertEqual(0, solar_import.import_recent(STATION))
        self.assertEqual([], lib.puts)

    def test_the_cursor_advances_so_a_second_run_republishes_nothing(self):
        lib = FakeLib()
        solar_import = make_import(lib)
        solar_import.seed_cursors(None)
        self.assertEqual(3, solar_import.import_recent(STATION))
        self.assertEqual(utc(2026, 9, 9, 0, 0), solar_import.cursor("02932"))
        self.assertEqual(0, solar_import.import_recent(STATION))
        self.assertEqual(3, len(lib.puts))

    def test_a_station_cursor_does_not_hold_back_another_station(self):
        lib = FakeLib()
        solar_import = make_import(lib, [STATION, OTHER_STATION])
        solar_import.seed_cursors(None)
        solar_import.import_recent(STATION)
        self.assertEqual(utc(2026, 9, 9, 0, 0), solar_import.cursor("02932"))
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
        with self.assertLogs("lib.dwd.importer", level=logging.ERROR):
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

        third = lib.puts[2][1]
        self.assertEqual(MISSING, third["global_irradiance_wm2"])
        self.assertEqual(MISSING, third["diffuse_irradiance_wm2"])
        self.assertEqual(MISSING, third["sunshine_seconds"])
        self.assertEqual(MISSING, third["longwave_wm2"])
        self.assertEqual(MISSING, third["global_irradiance_raw"])
        self.assertNotIn("temperature_2m", third)
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
        with self.assertLogs("lib.dwd.archive", level=logging.ERROR):
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
        with self.assertLogs("lib.dwd.importer", level=logging.ERROR):
            self.assertEqual(4, solar_import.import_latest(utc(2026, 9, 9, 12, 0)))
        self.assertEqual(4, len(lib.puts))


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
