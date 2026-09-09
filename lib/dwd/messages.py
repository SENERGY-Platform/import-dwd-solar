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

from typing import Dict, Optional

from lib.dwd.rows import HOURS_TO_SECONDS, JOULE_PER_CM2_TO_W_PER_M2, SolarRow, TemperatureRow, scaled
from lib.dwd.stations import Station

UNITS = {
    "global_irradiance_wm2": "W/m2",
    "diffuse_irradiance_wm2": "W/m2",
    "sunshine_seconds": "s",
    "longwave_wm2": "W/m2",
    "global_irradiance_raw": "J/cm2 per 10 min",
    "temperature_2m": "°C",
}


def build_message(station: Station, solar_row: SolarRow, temperature_row: Optional[TemperatureRow],
                  with_temperature: bool) -> Dict:
    '''
    Builds the published value of one instant

    temperature_2m is left out entirely rather than sent as null when the temperature of that instant is unknown,
    so a consumer can tell "not measured" apart from a measured value.

    :param station: the DWD station
    :param solar_row: the solar observation
    :param temperature_row: the temperature observation of the same instant, or None
    :param with_temperature: whether temperature is part of this import
    :return: the value dict
    '''
    message = {
        "global_irradiance_wm2": scaled(solar_row.global_, JOULE_PER_CM2_TO_W_PER_M2),
        "diffuse_irradiance_wm2": scaled(solar_row.diffuse, JOULE_PER_CM2_TO_W_PER_M2),
        "sunshine_seconds": scaled(solar_row.sunshine, HOURS_TO_SECONDS),
        "longwave_wm2": scaled(solar_row.longwave, JOULE_PER_CM2_TO_W_PER_M2),
        "global_irradiance_raw": solar_row.global_,
    }
    if with_temperature and temperature_row is not None:
        message["temperature_2m"] = temperature_row.temperature_2m
    message["meta"] = {
        # See https://www.dwd.de/DE/leistungen/klimadatendeutschland/qualitaetsniveau.html
        "quality_level": solar_row.quality_level,
        "name": station.name,
        "id": station.station_id,
        "lat": station.lat,
        "long": station.long,
        "height": station.height,
        "units": dict(UNITS),
    }
    return message
