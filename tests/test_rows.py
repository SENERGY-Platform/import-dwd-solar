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
import logging
import unittest

from lib.dwd.rows import HOURS_TO_SECONDS, JOULE_PER_CM2_TO_W_PER_M2, MISSING, TemperatureRow, join_temperature, \
    parse_mess_datum, parse_solar, parse_temperature, scaled

SOLAR_TEXT = "\n".join([
    "STATIONS_ID;MESS_DATUM;QN;DS_10;GS_10;SD_10;LS_10;eor",
    "2932;202609090000;2;0.0;0.0;0.000;23.7;eor",
    "2932;202609090010;2;12.3;16.7;0.050;24.1;eor",
    "2932;202609090020;1;-999;16.7;-999;-999;eor",
    "",
])

TEMPERATURE_TEXT = "\n".join([
    "STATIONS_ID;MESS_DATUM;QN;PP_10;TT_10;TM5_10;RF_10;TD_10;eor",
    "2932;202609090000;2;988.4;22.4;20.8;55.9;13.2;eor",
    "2932;202609090020;2;988.5;-999;21.0;56.2;13.2;eor",
    "",
])


def utc(year, month, day, hour, minute):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.timezone.utc)


class TestMessDatum(unittest.TestCase):
    def test_is_timezone_aware_utc(self):
        instant = parse_mess_datum("202609090010")
        self.assertIsNotNone(instant.tzinfo)
        self.assertEqual(datetime.timedelta(0), instant.utcoffset())
        self.assertEqual(utc(2026, 9, 9, 0, 10), instant)

    def test_matches_the_utc_instant_not_local_time(self):
        # A naive read would be interpreted as local time by anything downstream and shift the whole series.
        self.assertEqual(1788912600, int(parse_mess_datum("202609090010").timestamp()))

    def test_rejects_a_stamp_of_another_shape(self):
        self.assertRaises(ValueError, parse_mess_datum, "2026-09-09T00:10")


class TestScaled(unittest.TestCase):
    def test_missing_is_not_scaled(self):
        self.assertEqual(MISSING, scaled(MISSING, JOULE_PER_CM2_TO_W_PER_M2))
        self.assertEqual(MISSING, scaled(MISSING, HOURS_TO_SECONDS))

    def test_conversion_uses_one_constant_factor(self):
        # Pinned exactly: the offline dataset builder of the demonstrator multiplies by the same constant, and a
        # mathematically equal formulation such as value * 10000.0 / 600.0 gives a different last bit for some inputs.
        self.assertEqual(16.7 * JOULE_PER_CM2_TO_W_PER_M2, scaled(16.7, JOULE_PER_CM2_TO_W_PER_M2))


class TestParseSolar(unittest.TestCase):
    def test_reads_all_rows_keyed_by_instant(self):
        rows = parse_solar([SOLAR_TEXT])
        self.assertEqual([utc(2026, 9, 9, 0, 0), utc(2026, 9, 9, 0, 10), utc(2026, 9, 9, 0, 20)], sorted(rows))

    def test_keeps_the_values_as_delivered(self):
        row = parse_solar([SOLAR_TEXT])[utc(2026, 9, 9, 0, 10)]
        self.assertEqual(2, row.quality_level)
        self.assertAlmostEqual(12.3, row.diffuse, places=9)
        self.assertAlmostEqual(16.7, row.global_, places=9)
        self.assertAlmostEqual(0.05, row.sunshine, places=9)
        self.assertAlmostEqual(24.1, row.longwave, places=9)

    def test_converts_to_wm2_and_seconds(self):
        row = parse_solar([SOLAR_TEXT])[utc(2026, 9, 9, 0, 10)]
        # 16.7 J/cm2 per 10 min = 16.7 * 10000 / 600 W/m2
        self.assertAlmostEqual(278.3333333333333, scaled(row.global_, JOULE_PER_CM2_TO_W_PER_M2), places=9)
        self.assertAlmostEqual(205.0, scaled(row.diffuse, JOULE_PER_CM2_TO_W_PER_M2), places=9)
        self.assertAlmostEqual(401.6666666666667, scaled(row.longwave, JOULE_PER_CM2_TO_W_PER_M2), places=9)
        # 0.050 h of sunshine = 180 s
        self.assertAlmostEqual(180.0, scaled(row.sunshine, HOURS_TO_SECONDS), places=9)

    def test_a_missing_column_does_not_affect_its_siblings(self):
        row = parse_solar([SOLAR_TEXT])[utc(2026, 9, 9, 0, 20)]
        self.assertEqual(MISSING, scaled(row.diffuse, JOULE_PER_CM2_TO_W_PER_M2))
        self.assertEqual(MISSING, scaled(row.sunshine, HOURS_TO_SECONDS))
        self.assertEqual(MISSING, scaled(row.longwave, JOULE_PER_CM2_TO_W_PER_M2))
        self.assertAlmostEqual(278.3333333333333, scaled(row.global_, JOULE_PER_CM2_TO_W_PER_M2), places=9)

    def test_zero_is_kept_as_zero(self):
        row = parse_solar([SOLAR_TEXT])[utc(2026, 9, 9, 0, 0)]
        self.assertEqual(0.0, scaled(row.global_, JOULE_PER_CM2_TO_W_PER_M2))
        self.assertEqual(0.0, scaled(row.sunshine, HOURS_TO_SECONDS))

    def test_an_empty_cell_counts_as_missing(self):
        text = "\n".join([
            "STATIONS_ID;MESS_DATUM;QN;DS_10;GS_10;SD_10;LS_10;eor",
            "2932;202609090000;2;;0.0;0.000;23.7;eor",
        ])
        self.assertEqual(MISSING, parse_solar([text])[utc(2026, 9, 9, 0, 0)].diffuse)

    def test_a_file_without_the_needed_column_is_ignored(self):
        text = "\n".join([
            "STATIONS_ID;MESS_DATUM;QN;DS_10;SD_10;LS_10;eor",
            "2932;202609090000;2;0.0;0.000;23.7;eor",
        ])
        with self.assertLogs("lib.dwd.rows", level=logging.ERROR):
            self.assertEqual({}, parse_solar([text]))

    def test_an_unparsable_row_is_skipped_without_dropping_the_file(self):
        text = "\n".join([
            "STATIONS_ID;MESS_DATUM;QN;DS_10;GS_10;SD_10;LS_10;eor",
            "2932;nonsense;2;0.0;0.0;0.000;23.7;eor",
            "2932;202609090010;2;0.0;16.7;0.000;23.7;eor",
        ])
        with self.assertLogs("lib.dwd.rows", level=logging.ERROR):
            rows = parse_solar([text])
        self.assertEqual([utc(2026, 9, 9, 0, 10)], sorted(rows))

    def test_several_files_merge_into_one_series(self):
        other = "\n".join([
            "STATIONS_ID;MESS_DATUM;QN;DS_10;GS_10;SD_10;LS_10;eor",
            "2932;202609090030;2;0.0;20.0;0.000;23.7;eor",
        ])
        rows = parse_solar([SOLAR_TEXT, other])
        self.assertEqual(4, len(rows))
        self.assertAlmostEqual(20.0, rows[utc(2026, 9, 9, 0, 30)].global_, places=9)


class TestJoinTemperature(unittest.TestCase):
    def test_pairs_on_the_instant_and_sorts_ascending(self):
        joined = join_temperature(parse_solar([SOLAR_TEXT]), parse_temperature([TEMPERATURE_TEXT]))
        self.assertEqual([utc(2026, 9, 9, 0, 0), utc(2026, 9, 9, 0, 10), utc(2026, 9, 9, 0, 20)],
                         [instant for instant, _, _ in joined])
        self.assertEqual(TemperatureRow(temperature_2m=22.4), joined[0][2])

    def test_an_instant_without_a_temperature_row_stays_unpaired(self):
        joined = join_temperature(parse_solar([SOLAR_TEXT]), parse_temperature([TEMPERATURE_TEXT]))
        self.assertIsNone(joined[1][2])

    def test_a_missing_temperature_reading_is_still_a_row(self):
        # -999 means measured and unusable, which is not the same as no row for that instant.
        joined = join_temperature(parse_solar([SOLAR_TEXT]), parse_temperature([TEMPERATURE_TEXT]))
        self.assertEqual(TemperatureRow(temperature_2m=MISSING), joined[2][2])

    def test_solar_rows_are_never_dropped_for_lack_of_temperature(self):
        joined = join_temperature(parse_solar([SOLAR_TEXT]), {})
        self.assertEqual(3, len(joined))
        self.assertEqual([None, None, None], [temperature for _, _, temperature in joined])


if __name__ == "__main__":
    unittest.main()
