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

import logging
import unittest

from lib.dwd.archive import STATION_LIST_URL
from lib.dwd.stations import STATION_LIST_ENCODING, fetch_station_list, parse_station_list, \
    select_stations, stations_for

# Verbatim shape of zehn_min_sd_Beschreibung_Stationen.txt, including the trailing padding and the multi word
# station names that the column layout allows.
STATION_LIST = "\n".join([
    "Stations_id von_datum bis_datum Stationshoehe geoBreite geoLaenge Stationsname Bundesland Abgabe",
    "----------- --------- --------- ------------- --------- --------- ---------------------------- ---------- ------",
    "00044 20070402 20260303             44     52.9336    8.2370 Großenkneten                             "
    "Niedersachsen                            Frei                    ",
    "00427 19920218 20250407             46     52.3805   13.5304 Berlin Brandenburg                       "
    "Brandenburg                              Frei                    ",
    "02932 19940117 20260909            131     51.4347   12.2396 Leipzig/Halle                            "
    "Sachsen                                  Frei                    ",
    "03032 19951201 20260310             25     55.0110    8.4125 List auf Sylt                            "
    "Schleswig-Holstein                       Frei                    ",
    "04928 19980604 20260909            314     48.8281    9.2000 Stuttgart (Schnarrenberg)                "
    "Baden-Württemberg                        Frei                    ",
])

# Encoded with the literal encoding DWD serves, so a change to the module constant shows up here.
STATION_LIST_BYTES = STATION_LIST.encode("latin-1")


class TestParseStationList(unittest.TestCase):
    def test_reads_every_station(self):
        stations = parse_station_list(STATION_LIST_BYTES)
        self.assertEqual(["00044", "00427", "02932", "03032", "04928"],
                         [station.station_id for station in stations])

    def test_keeps_the_id_zero_padded(self):
        # The archive URLs and the configured ids both use the padded form; the row data does not.
        self.assertEqual("00427", parse_station_list(STATION_LIST_BYTES)[1].station_id)

    def test_a_multi_word_name_is_not_merged_with_the_bundesland(self):
        stations = {station.station_id: station for station in parse_station_list(STATION_LIST_BYTES)}
        self.assertEqual("Berlin Brandenburg", stations["00427"].name)
        self.assertEqual("List auf Sylt", stations["03032"].name)
        self.assertEqual("Stuttgart (Schnarrenberg)", stations["04928"].name)

    def test_a_single_word_name_keeps_its_umlauts(self):
        stations = {station.station_id: station for station in parse_station_list(STATION_LIST_BYTES)}
        self.assertEqual("Großenkneten", stations["00044"].name)
        self.assertEqual("Leipzig/Halle", stations["02932"].name)

    def test_reads_the_coordinates_and_the_height(self):
        station = parse_station_list(STATION_LIST_BYTES)[2]
        self.assertAlmostEqual(51.4347, station.lat, places=6)
        self.assertAlmostEqual(12.2396, station.long, places=6)
        self.assertAlmostEqual(131.0, station.height, places=6)

    def test_a_short_line_is_skipped(self):
        raw = (STATION_LIST + "\n00001 20070402").encode("latin-1")
        with self.assertLogs("import.stations", level=logging.ERROR):
            self.assertEqual(5, len(parse_station_list(raw)))

    def test_an_unparsable_number_is_fatal(self):
        broken = STATION_LIST.replace("51.4347", "east-ish").encode("latin-1")
        with self.assertLogs("import.stations", level=logging.ERROR):
            self.assertRaises(Exception, parse_station_list, broken)

    def test_an_unparsable_number_that_looks_like_a_format_string_is_still_reported(self):
        # The configured logger interpolates its message against its arguments, so the text of the exception,
        # which quotes the unparsable field, must not reach the message itself.
        broken = STATION_LIST.replace("51.4347", "  100%").encode("latin-1")
        with self.assertLogs("import.stations", level=logging.ERROR):
            self.assertRaisesRegex(Exception, "Could not parse station list", parse_station_list, broken)


class TestEncoding(unittest.TestCase):
    def test_the_station_list_is_read_as_the_encoding_dwd_serves(self):
        self.assertEqual("latin-1", STATION_LIST_ENCODING)


class TestFetchStationList(unittest.TestCase):
    def test_reads_the_solar_station_description(self):
        requested = []

        def fetch(url):
            requested.append(url)
            return STATION_LIST_BYTES

        self.assertEqual(5, len(fetch_station_list(fetch)))
        self.assertEqual([STATION_LIST_URL], requested)

    def test_an_unavailable_list_is_fatal(self):
        self.assertRaises(Exception, fetch_station_list, lambda url: None)


class TestSelectStations(unittest.TestCase):
    def setUp(self):
        self.stations = parse_station_list(STATION_LIST_BYTES)

    def test_selects_in_configured_order(self):
        selected, missing = select_stations(self.stations, ["02932", "00044"])
        self.assertEqual(["02932", "00044"], [station.station_id for station in selected])
        self.assertEqual([], missing)

    def test_reports_unknown_ids_and_keeps_the_others(self):
        selected, missing = select_stations(self.stations, ["02932", "99999"])
        self.assertEqual(["02932"], [station.station_id for station in selected])
        self.assertEqual(["99999"], missing)

    def test_an_unpadded_id_does_not_match(self):
        # 2932 is how the row data names the station; the station list and the URLs use 02932.
        selected, missing = select_stations(self.stations, ["2932"])
        self.assertEqual([], selected)
        self.assertEqual(["2932"], missing)

    def test_a_repeated_id_is_selected_once(self):
        selected, _ = select_stations(self.stations, ["02932", "02932"])
        self.assertEqual(["02932"], [station.station_id for station in selected])

    def test_surrounding_whitespace_is_tolerated(self):
        selected, missing = select_stations(self.stations, [" 02932 "])
        self.assertEqual(["02932"], [station.station_id for station in selected])
        self.assertEqual([], missing)


class TestStationsFor(unittest.TestCase):
    def setUp(self):
        self.stations = parse_station_list(STATION_LIST_BYTES)

    def test_nothing_configured_covers_every_station(self):
        # one instance for all of them is the point: a consumer picks its station out of the export, by id or
        # by distance, instead of an instance and an export existing per station
        for station_ids in (None, []):
            selected, missing = stations_for(self.stations, station_ids)
            self.assertEqual([station.station_id for station in self.stations],
                             [station.station_id for station in selected])
            self.assertEqual([], missing)

    def test_configured_ids_still_select(self):
        selected, missing = stations_for(self.stations, ["02932", "99999"])
        self.assertEqual(["02932"], [station.station_id for station in selected])
        self.assertEqual(["99999"], missing)

    def test_the_list_it_returns_is_its_own(self):
        # the caller keeps the result for the life of the import and the station list is read again on a reload
        selected, _ = stations_for(self.stations, None)
        selected.clear()
        self.assertEqual(5, len(self.stations))


if __name__ == "__main__":
    unittest.main()
