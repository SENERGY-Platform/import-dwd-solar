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

import json
import unittest

from lib.dwd.messages import UNITS, build_message
from lib.dwd.rows import MISSING, SolarRow, TemperatureRow
from lib.dwd.stations import Station

STATION = Station(station_id="02932", name="Leipzig/Halle", lat=51.4347, long=12.2396, height=131.0)

ROW = SolarRow(quality_level=2, diffuse=12.3, global_=16.7, sunshine=0.05, longwave=24.1)


class TestBuildMessage(unittest.TestCase):
    def test_converts_the_solar_columns(self):
        message = build_message(STATION, ROW, None, False)
        self.assertAlmostEqual(278.3333333333333, message["global_irradiance_wm2"], places=9)
        self.assertAlmostEqual(205.0, message["diffuse_irradiance_wm2"], places=9)
        self.assertAlmostEqual(401.6666666666667, message["longwave_wm2"], places=9)
        self.assertAlmostEqual(180.0, message["sunshine_seconds"], places=9)

    def test_keeps_the_raw_global_irradiance(self):
        self.assertAlmostEqual(16.7, build_message(STATION, ROW, None, False)["global_irradiance_raw"], places=9)

    def test_a_missing_column_is_left_out_of_the_message(self):
        # The marker is a number to every consumer that averages or interpolates the series, so it is not sent.
        row = ROW._replace(global_=MISSING, sunshine=MISSING)
        message = build_message(STATION, row, None, False)
        self.assertNotIn("global_irradiance_wm2", message)
        self.assertNotIn("global_irradiance_raw", message)
        self.assertNotIn("sunshine_seconds", message)
        self.assertAlmostEqual(205.0, message["diffuse_irradiance_wm2"], places=9)
        self.assertAlmostEqual(401.6666666666667, message["longwave_wm2"], places=9)

    def test_a_row_of_nothing_but_markers_still_carries_its_metadata(self):
        row = SolarRow(quality_level=1, diffuse=MISSING, global_=MISSING, sunshine=MISSING, longwave=MISSING)
        message = build_message(STATION, row, TemperatureRow(temperature_2m=MISSING), True)
        self.assertEqual(["meta"], list(message))
        self.assertEqual(1, message["meta"]["quality_level"])

    def test_a_missing_quality_level_is_left_out_of_the_metadata(self):
        # DWD marks QN with -999 like any other column, and a consumer reading meta.quality_level as a number
        # would take the marker for a quality level of its own.
        row = ROW._replace(quality_level=int(MISSING))
        meta = build_message(STATION, row, None, False)["meta"]
        self.assertNotIn("quality_level", meta)
        # The rest of the metadata is unaffected.
        self.assertEqual("02932", meta["id"])

    def test_carries_the_station_metadata(self):
        meta = build_message(STATION, ROW, None, False)["meta"]
        self.assertEqual(2, meta["quality_level"])
        self.assertEqual("Leipzig/Halle", meta["name"])
        self.assertEqual("02932", meta["id"])
        self.assertAlmostEqual(51.4347, meta["lat"], places=6)
        self.assertAlmostEqual(12.2396, meta["long"], places=6)
        self.assertAlmostEqual(131.0, meta["height"], places=6)
        self.assertEqual(UNITS, meta["units"])

    def test_temperature_is_joined_when_configured(self):
        message = build_message(STATION, ROW, TemperatureRow(temperature_2m=22.4), True)
        self.assertAlmostEqual(22.4, message["temperature_2m"], places=9)

    def test_temperature_is_absent_without_a_matching_row(self):
        message = build_message(STATION, ROW, None, True)
        self.assertNotIn("temperature_2m", message)

    def test_temperature_is_absent_when_not_configured(self):
        message = build_message(STATION, ROW, TemperatureRow(temperature_2m=22.4), False)
        self.assertNotIn("temperature_2m", message)

    def test_a_missing_temperature_reading_is_left_out_of_the_message(self):
        message = build_message(STATION, ROW, TemperatureRow(temperature_2m=MISSING), True)
        self.assertNotIn("temperature_2m", message)
        # The solar columns of the same instant are unaffected by the missing temperature.
        self.assertAlmostEqual(278.3333333333333, message["global_irradiance_wm2"], places=9)

    def test_the_message_is_json_serialisable(self):
        json.loads(json.dumps(build_message(STATION, ROW, TemperatureRow(temperature_2m=22.4), True)))

    def test_the_units_are_not_shared_between_messages(self):
        first = build_message(STATION, ROW, None, False)
        first["meta"]["units"]["global_irradiance_wm2"] = "changed"
        self.assertEqual("W/m2", build_message(STATION, ROW, None, False)["meta"]["units"]["global_irradiance_wm2"])


if __name__ == "__main__":
    unittest.main()
