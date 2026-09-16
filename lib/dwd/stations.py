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
from typing import Callable, List, Optional, Tuple

from import_lib.import_lib import get_logger

from lib.dwd.archive import STATION_LIST_URL, get_bytes

_logger = None


def log() -> logging.Logger:
    # Built on first use: import-lib's logger only carries its static fields after ImportLib() ran init_logging.
    global _logger
    if _logger is None:
        _logger = get_logger(__name__)
    return _logger


# The name column is the only one that may contain spaces. Bundesland and Abgabe
# are one token each and trail it, so the name is everything between the sixth
# field and those two.
FIRST_NAME_FIELD = 6
TRAILING_FIELDS = 2

# The station list carries German names; DWD serves it as ISO-8859-1.
STATION_LIST_ENCODING = "latin-1"


class Station:
    '''
    Class to store DWD station metadata
    '''
    __slots__ = ('station_id', 'name', 'lat', 'long', 'height')

    def __init__(self, station_id: str, name: str, lat: float, long: float, height: float):
        self.station_id = station_id
        self.name = name
        self.lat = lat
        self.long = long
        self.height = height

    def __repr__(self) -> str:
        return "Station(" + self.station_id + ", " + self.name + ")"


def parse_station_list(raw: bytes) -> List[Station]:
    '''
    Parses the solar station description file

    :param raw: content of zehn_min_sd_Beschreibung_Stationen.txt
    :return: List of Stations
    '''
    lines = raw.decode(STATION_LIST_ENCODING).splitlines()
    lines = lines[2:]  # Remove header and separator

    stations = []
    for line in lines:
        fields = [field for field in line.split() if field != ""]
        if len(fields) < FIRST_NAME_FIELD + TRAILING_FIELDS + 1:
            log().error("Station list line has too few fields and will be ignored: %s", line)
            continue
        try:
            stations.append(Station(
                # Keep the zero padded id as delivered, it is what the archive URLs use.
                station_id=fields[0],
                name=" ".join(fields[FIRST_NAME_FIELD:-TRAILING_FIELDS]),
                lat=float(fields[4]),
                long=float(fields[5]),
                height=float(fields[3]),
            ))
        except ValueError as e:
            log().error("%s", e)
            raise Exception("Could not parse station list")
    return stations


def fetch_station_list(fetch: Callable[[str], Optional[bytes]] = get_bytes) -> List[Station]:
    '''
    Downloads the list of all DWD stations that deliver ten minute solar data

    :param fetch: Callable that turns a URL into its content, or None if unavailable
    :return: List of Stations
    '''
    raw = fetch(STATION_LIST_URL)
    if raw is None:
        raise Exception("Could not get station list. Network OK?")
    return parse_station_list(raw)


def select_stations(stations: List[Station], station_ids: List[str]) -> Tuple[List[Station], List[str]]:
    '''
    Picks the configured stations out of the station list

    :param stations: List of all Stations
    :param station_ids: configured station ids, as zero padded strings
    :return: Tuple of the selected Stations in configured order and the ids that are unknown
    '''
    by_id = {station.station_id: station for station in stations}
    selected = []
    missing = []
    seen = set()
    for station_id in station_ids:
        station_id = str(station_id).strip()
        if station_id in seen:
            continue
        seen.add(station_id)
        if station_id in by_id:
            selected.append(by_id[station_id])
        else:
            missing.append(station_id)
    return selected, missing


def stations_for(stations: List[Station], station_ids: Optional[List[str]]) -> Tuple[List[Station], List[str]]:
    '''
    The stations one import covers: the configured ones, or every station of the list when none are configured.

    Covering all of them is what lets a single instance serve consumers that pick their station themselves - by
    id, or by distance to a coordinate - instead of one instance per station and one export each.

    :param stations: List of all Stations
    :param station_ids: configured station ids, empty or None for all of them
    :return: Tuple of the Stations to import and the configured ids that are unknown
    '''
    if not station_ids:
        return list(stations), []
    return select_stations(stations, station_ids)
