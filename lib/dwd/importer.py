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
from typing import Callable, Dict, List, Optional, Tuple

from lib.dwd.archive import NOW, RECENT, SOLAR, TEMPERATURE, blocks_overlapping, fetch_historical_block, \
    fetch_historical_blocks, fetch_product, get_bytes
from lib.dwd.messages import build_message
from lib.dwd.rows import SolarRow, TemperatureRow, join_temperature, parse_solar, parse_temperature
from lib.dwd.stations import Station

logger = logging.getLogger(__name__)

# A cursor this old is not inside the reach of the recent archive any more, so the historical blocks are needed
# to close the gap.
RECENT_REACH = datetime.timedelta(days=500)

# A cursor this old is beyond what a single now archive, which holds the current day, could carry.
NOW_REACH = datetime.timedelta(hours=24)


def as_utc(value: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    '''
    Normalises a datetime to timezone aware UTC

    import_lib returns the last published timestamp without a timezone even though it published it as UTC;
    comparing that against an aware datetime would raise.

    :param value: a datetime or None
    :return: the same instant as an aware UTC datetime, or None
    '''
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def start_of_day(instant: datetime.datetime) -> datetime.datetime:
    '''
    The UTC midnight of an instant's day

    :param instant: an aware UTC datetime
    :return: that day at 00:00 UTC
    '''
    return instant.replace(hour=0, minute=0, second=0, microsecond=0)


class SolarImport:
    '''
    Publishes the ten minute solar observations of a set of stations, each station tracked by its own cursor
    '''

    def __init__(self, lib, stations: List[Station], with_temperature: bool = True,
                 fetch: Callable[[str], Optional[bytes]] = get_bytes):
        self.__lib = lib
        self.__stations = stations
        self.__with_temperature = with_temperature
        self.__fetch = fetch
        # There is a single last published signal at startup, so every station starts from it. From then on the
        # cursors move independently, which means a station that lags behind the others cannot catch up its own
        # backlog once another station has published a newer instant.
        self.__cursors: Dict[str, Optional[datetime.datetime]] = {
            station.station_id: None for station in stations
        }

    def seed_cursors(self, last_published: Optional[datetime.datetime]) -> None:
        '''
        Sets every station's cursor to the last published instant

        :param last_published: last published instant, aware UTC, or None for a fresh import
        :return: None
        '''
        last_published = as_utc(last_published)
        for station_id in self.__cursors:
            self.__cursors[station_id] = last_published

    def cursor(self, station_id: str) -> Optional[datetime.datetime]:
        '''
        The newest instant published for a station so far

        :param station_id: zero padded DWD station id
        :return: the instant, or None if nothing was published for it yet
        '''
        return self.__cursors[station_id]

    def needs_historical(self, station: Station, now: datetime.datetime) -> bool:
        '''
        Whether the historical blocks of a station are needed to close its gap

        :param station: the DWD station
        :param now: current instant, aware UTC
        :return: True if the cursor is unset or outside the reach of the recent archive
        '''
        cursor = self.__cursors[station.station_id]
        return cursor is None or cursor < now - RECENT_REACH

    def needs_recent(self, station: Station, now: datetime.datetime) -> bool:
        '''
        Whether the recent archive of a station is needed to close its gap

        :param station: the DWD station
        :param now: current instant, aware UTC
        :return: True if the cursor is unset or older than a now archive could carry
        '''
        cursor = self.__cursors[station.station_id]
        return cursor is None or cursor < now - NOW_REACH

    def import_historical(self, station: Station) -> int:
        '''
        Publishes all historical observations of a station that are newer than its cursor

        :param station: the DWD station
        :return: number of published data points
        '''
        solar_blocks = fetch_historical_blocks(SOLAR, station.station_id, self.__fetch)
        temperature_blocks = []
        if self.__with_temperature:
            temperature_blocks = fetch_historical_blocks(TEMPERATURE, station.station_id, self.__fetch)
        count = 0
        # Oldest block first, so the cursor only ever moves forward.
        for first, last, url in solar_blocks:
            solar_texts = fetch_historical_block(url, self.__fetch)
            if solar_texts is None:
                continue
            temperature_texts = []
            for temperature_url in blocks_overlapping(temperature_blocks, first, last):
                texts = fetch_historical_block(temperature_url, self.__fetch)
                if texts is not None:
                    temperature_texts.extend(texts)
            count += self.__publish(station, parse_solar(solar_texts), parse_temperature(temperature_texts))
        logger.info("Imported " + str(count) + " historical data points of station " + station.station_id)
        return count

    def import_recent(self, station: Station) -> int:
        '''
        Publishes the observations of the recent archive of a station that are newer than its cursor

        :param station: the DWD station
        :return: number of published data points
        '''
        count = self.__import(station, (RECENT,))
        logger.info("Imported " + str(count) + " recent data points of station " + station.station_id)
        return count

    def import_latest(self, now: Optional[datetime.datetime] = None) -> int:
        '''
        Publishes everything newer than each station's cursor, reaching into the recent archive where the current
        day cannot close the gap. A failing station does not stop the others, so the schedule survives a station
        going offline.

        :param now: current instant, aware UTC; defaults to the wall clock
        :return: number of published data points
        '''
        if now is None:
            now = datetime.datetime.now(datetime.timezone.utc)
        total = 0
        for station in self.__stations:
            try:
                count = self.__import(station, self.kinds_for(station, now))
                total += count
                logger.info("Imported " + str(count) + " latest data points of station " + station.station_id)
            except Exception as e:
                logger.error("Could not import station " + station.station_id + ": " + str(e))
        return total

    def kinds_for(self, station: Station, now: datetime.datetime) -> Tuple[str, ...]:
        '''
        Which archives a scheduled run has to read for a station

        :param station: the DWD station
        :param now: current instant, aware UTC
        :return: Tuple of archive kinds, oldest reaching first
        '''
        cursor = self.__cursors[station.station_id]
        # The now archive holds the current day only. A cursor from before today therefore needs the recent archive
        # as well, even when it is minutes old: right after midnight the tail of yesterday exists nowhere else.
        if cursor is not None and cursor < start_of_day(now):
            return RECENT, NOW
        return (NOW,)

    def __import(self, station: Station, kinds: Tuple[str, ...]) -> int:
        solar_texts = []
        temperature_texts = []
        for kind in kinds:
            texts = fetch_product(SOLAR, kind, station.station_id, self.__fetch)
            if texts is not None:
                solar_texts.extend(texts)
            if not self.__with_temperature:
                continue
            texts = fetch_product(TEMPERATURE, kind, station.station_id, self.__fetch)
            if texts is not None:
                temperature_texts.extend(texts)
        return self.__publish(station, parse_solar(solar_texts), parse_temperature(temperature_texts))

    def __publish(self, station: Station, solar: Dict[datetime.datetime, SolarRow],
                  temperature: Dict[datetime.datetime, TemperatureRow]) -> int:
        cursor = self.__cursors[station.station_id]
        count = 0
        for instant, solar_row, temperature_row in join_temperature(solar, temperature):
            if cursor is not None and instant <= cursor:
                continue
            value = build_message(station, solar_row, temperature_row, self.__with_temperature)
            logger.debug(str(instant) + ": " + str(value))
            self.__lib.put(instant, value)
            cursor = instant
            self.__cursors[station.station_id] = cursor
            count += 1
        return count
