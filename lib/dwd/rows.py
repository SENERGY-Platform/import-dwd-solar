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
from collections import namedtuple
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# DWD marks a missing numeric value with -999. It is checked per column, because the columns of one row can be
# missing independently of each other.
MISSING = -999.0

# DS_10, GS_10 and LS_10 are sums over the ten minute interval in J/cm2. 1 J/cm2 = 10 kJ/m2, so the mean irradiance
# is that over 600 s. Kept as one constant, applied by multiplication, so this import and the offline dataset
# builder of the demonstrator produce bit identical numbers for the same observation.
JOULE_PER_CM2_TO_W_PER_M2 = 10000.0 / 600.0

# SD_10 is sunshine duration within the interval in hours.
HOURS_TO_SECONDS = 3600.0

TIME_COLUMN = "MESS_DATUM"
QUALITY_COLUMN = "QN"

SOLAR_COLUMNS = (QUALITY_COLUMN, TIME_COLUMN, "DS_10", "GS_10", "SD_10", "LS_10")
TEMPERATURE_COLUMNS = (TIME_COLUMN, "TT_10")

# Only the columns this import publishes are kept; a historical block holds hundreds of thousands of rows.
SolarRow = namedtuple("SolarRow", ("quality_level", "diffuse", "global_", "sunshine", "longwave"))
TemperatureRow = namedtuple("TemperatureRow", ("temperature_2m",))


def parse_mess_datum(stamp: str) -> datetime.datetime:
    '''
    Parses a DWD MESS_DATUM stamp

    The ten minute products are stamped in UTC, unlike the hourly and daily ones, and the result carries that
    offset explicitly so nothing downstream has to assume it.

    :param stamp: the stamp as YYYYmmddHHMM
    :return: timezone aware datetime at UTC
    '''
    return datetime.datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=datetime.timezone.utc)


def scaled(raw: float, factor: float) -> float:
    '''
    Applies a unit conversion, leaving a missing value untouched

    :param raw: the value as delivered
    :param factor: the conversion factor
    :return: the converted value, or MISSING if raw is missing
    '''
    if raw == MISSING:
        return MISSING
    return raw * factor


def parse_solar(texts: Iterable[str]) -> Dict[datetime.datetime, SolarRow]:
    '''
    Reads solar observations, keyed by their instant

    :param texts: contents of product files, spaces already stripped
    :return: Dict of instant to SolarRow
    '''
    rows = {}
    for text in texts:
        _parse(text, SOLAR_COLUMNS, _build_solar, rows)
    return rows


def parse_temperature(texts: Iterable[str]) -> Dict[datetime.datetime, TemperatureRow]:
    '''
    Reads air temperature observations, keyed by their instant

    :param texts: contents of product files, spaces already stripped
    :return: Dict of instant to TemperatureRow
    '''
    rows = {}
    for text in texts:
        _parse(text, TEMPERATURE_COLUMNS, _build_temperature, rows)
    return rows


def join_temperature(solar: Dict[datetime.datetime, SolarRow],
                     temperature: Dict[datetime.datetime, TemperatureRow]) \
        -> List[Tuple[datetime.datetime, SolarRow, Optional[TemperatureRow]]]:
    '''
    Joins temperature onto solar observations on their instant, oldest first

    The two products are published separately and their coverage differs, so a solar instant without a temperature
    row is normal and stays unpaired rather than being dropped.

    :param solar: Dict of instant to SolarRow
    :param temperature: Dict of instant to TemperatureRow
    :return: List of (instant, SolarRow, TemperatureRow or None), sorted by instant
    '''
    return [(instant, solar[instant], temperature.get(instant)) for instant in sorted(solar)]


def _parse(text: str, columns: Tuple[str, ...], build, into: Dict) -> None:
    lines = text.splitlines()
    if not lines:
        return
    header = lines[0].split(";")
    indices = {}
    for column in columns:
        if column not in header:
            logger.error("Product file does not contain column " + column + " and will be ignored")
            return
        indices[column] = header.index(column)
    highest = max(indices.values())
    for line in lines[1:]:
        if line == "":
            continue
        fields = line.split(";")
        if len(fields) <= highest:
            logger.error("Product row has too few fields and will be ignored: " + line)
            continue
        try:
            instant = parse_mess_datum(fields[indices[TIME_COLUMN]])
        except ValueError:
            logger.error("Could not parse datetime from product row. Format changed? Ignoring row")
            continue
        try:
            into[instant] = build(fields, indices)
        except ValueError:
            logger.error("Could not parse values from product row. Format changed? Ignoring row")
            continue


def _build_solar(fields: List[str], indices: Dict[str, int]) -> SolarRow:
    return SolarRow(
        quality_level=int(_number(fields[indices[QUALITY_COLUMN]])),
        diffuse=_number(fields[indices["DS_10"]]),
        global_=_number(fields[indices["GS_10"]]),
        sunshine=_number(fields[indices["SD_10"]]),
        longwave=_number(fields[indices["LS_10"]]),
    )


def _build_temperature(fields: List[str], indices: Dict[str, int]) -> TemperatureRow:
    return TemperatureRow(temperature_2m=_number(fields[indices["TT_10"]]))


def _number(field: str) -> float:
    # An empty cell means the same as -999: DWD has no observation for it.
    if field == "":
        return MISSING
    return float(field)
