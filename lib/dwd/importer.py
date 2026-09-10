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
from typing import Callable, Collection, Dict, List, Optional, Tuple

from import_lib.import_lib import get_logger

from lib.dwd.archive import NOW, RECENT, SOLAR, TEMPERATURE, blocks_overlapping, fetch_historical_block, \
    fetch_historical_blocks, fetch_product, get_bytes
from lib.dwd.messages import build_message
from lib.dwd.rows import SolarRow, TemperatureRow, join_temperature, parse_solar, parse_temperature
from lib.dwd.stations import Station

_logger = None

# A cursor this old is not inside the reach of the recent archive any more, so the historical blocks are needed
# to close the gap.
RECENT_REACH = datetime.timedelta(days=500)

# A cursor this old is beyond what a single now archive, which holds the current day, could carry.
NOW_REACH = datetime.timedelta(hours=24)

# An instant DWD's air temperature archives have not caught up with within this span goes out without the
# temperature, so a hole in one product costs latency and a single field instead of stalling the series for good.
MAX_HOLD = datetime.timedelta(hours=2)


def log() -> logging.Logger:
    # Built on first use: import-lib's logger only carries its static fields after ImportLib() ran init_logging.
    global _logger
    if _logger is None:
        _logger = get_logger(__name__)
    return _logger


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


def covered_until(instants: Collection[datetime.datetime],
                  recent_instants: Optional[Collection[datetime.datetime]]) -> Optional[datetime.datetime]:
    '''
    The newest instant of one product a run may rely on

    The now archive holds the current day and the recent archive whole closed days, so once the now archive has
    rolled over to a day the recent archive does not reach into, everything above the recent archive sits behind
    a gap that only the next regeneration of the recent archive fills.

    :param instants: every instant the run parsed for the product, the recent ones included
    :param recent_instants: the instants the recent archive contributed, or None if the run did not read it
    :return: the instant, or None if the run may rely on nothing
    '''
    if not instants:
        return None
    if recent_instants is None:
        return max(instants)
    if not recent_instants:
        return None
    newest_recent = max(recent_instants)
    above = [instant for instant in instants if instant > newest_recent]
    # Whole days, not single steps: a legitimately absent last row of the recent archive is not a gap.
    if above and start_of_day(min(above)) > start_of_day(newest_recent) + datetime.timedelta(days=1):
        return newest_recent
    return max(instants)


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
        # A station whose recent temperature archive did not answer keeps its backfill pending, and every
        # scheduled run retries it instead of reading the now archive over the days it should have filled.
        self.__backfill_pending: Dict[str, bool] = {station.station_id: False for station in stations}

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

    def backfill_pending(self, station_id: str) -> bool:
        '''
        Whether the recent archive of a station still has to be read before its scheduled runs may go on

        :param station_id: zero padded DWD station id
        :return: True while the backfill has to be retried
        '''
        return self.__backfill_pending[station_id]

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
        log().info("Imported %s historical data points of station %s", count, station.station_id)
        return count

    def import_recent(self, station: Station, now: Optional[datetime.datetime] = None) -> bool:
        '''
        Publishes the observations of the recent archive of a station that are newer than its cursor

        :param station: the DWD station
        :param now: current instant, aware UTC; defaults to the wall clock
        :return: True if the backfill is done, False if a later run has to retry it
        '''
        complete, _ = self.__backfill(station, now)
        return complete

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
                if self.__backfill_pending[station.station_id]:
                    # The backfill decides where the cursor starts, so the now archive of this station has to
                    # wait for it rather than publish the current day over the gap it left.
                    complete, count = self.__backfill(station, now)
                    total += count
                    if not complete:
                        continue
                count = self.__import(station, self.kinds_for(station, now), now)
                total += count
                log().info("Imported %s latest data points of station %s", count, station.station_id)
            except Exception as e:
                log().error("Could not import station %s: %s", station.station_id, e)
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

    def __backfill(self, station: Station, now: Optional[datetime.datetime]) -> Tuple[bool, int]:
        if now is None:
            now = datetime.datetime.now(datetime.timezone.utc)
        solar, temperature, recent_solar = self.__read(station, (RECENT,))
        # Without any temperature the whole backfill would go out bare once MAX_HOLD has passed on its closed
        # days, and no later run reads them again, so the cursor stays put and a later run retries instead.
        if self.__with_temperature and not temperature:
            self.__backfill_pending[station.station_id] = True
            log().error("The recent air temperature archive of station %s carries no rows, so its backfill and "
                        "its now archive wait for a later run", station.station_id)
            self.__log_waiting(station, self.__cursors[station.station_id], solar, {}, [], now)
            return False, 0
        count = self.__run(station, solar, temperature, recent_solar, now)
        self.__backfill_pending[station.station_id] = False
        log().info("Imported %s recent data points of station %s", count, station.station_id)
        return True, count

    def __import(self, station: Station, kinds: Tuple[str, ...], now: datetime.datetime) -> int:
        solar, temperature, recent_solar = self.__read(station, kinds)
        return self.__run(station, solar, temperature, recent_solar, now)

    def __read(self, station: Station, kinds: Tuple[str, ...]) \
            -> Tuple[Dict[datetime.datetime, SolarRow], Dict[datetime.datetime, TemperatureRow],
                     Optional[Dict[datetime.datetime, SolarRow]]]:
        solar: Dict[datetime.datetime, SolarRow] = {}
        temperature: Dict[datetime.datetime, TemperatureRow] = {}
        # What the recent archive contributes decides how far this run may publish; None means it was not read.
        recent_solar: Optional[Dict[datetime.datetime, SolarRow]] = None
        for kind in kinds:
            kind_solar: Dict[datetime.datetime, SolarRow] = {}
            texts = fetch_product(SOLAR, kind, station.station_id, self.__fetch)
            if texts is not None:
                kind_solar = parse_solar(texts)
            if kind == RECENT:
                recent_solar = kind_solar
            if self.__with_temperature:
                texts = fetch_product(TEMPERATURE, kind, station.station_id, self.__fetch)
                if texts is not None:
                    temperature.update(parse_temperature(texts))
            # The kinds are read oldest reaching first, so the now archive wins where the two overlap.
            solar.update(kind_solar)
        return solar, temperature, recent_solar

    def __run(self, station: Station, solar: Dict[datetime.datetime, SolarRow],
              temperature: Dict[datetime.datetime, TemperatureRow],
              recent_solar: Optional[Dict[datetime.datetime, SolarRow]], now: datetime.datetime) -> int:
        cursor = self.__cursors[station.station_id]
        publishable, bare = self.__complete_prefix(cursor, self.__before_the_gap(solar, recent_solar),
                                                   temperature, now)
        count = self.__publish(station, publishable, temperature)
        self.__log_waiting(station, cursor, solar, publishable, bare, now)
        return count

    def __before_the_gap(self, solar: Dict[datetime.datetime, SolarRow],
                         recent_solar: Optional[Dict[datetime.datetime, SolarRow]]) \
            -> Dict[datetime.datetime, SolarRow]:
        '''
        Drops the solar instants that sit behind a gap in this run's archives

        Around midnight the now archive rolls over to the new day hours before the recent archive is regenerated,
        so the tail of the previous day is in no archive and the cursor must not move past it.

        :param solar: Dict of instant to SolarRow as parsed, both kinds merged
        :param recent_solar: Dict of instant to SolarRow the recent archive contributed, or None if not read
        :return: the same Dict without the instants above the gap
        '''
        covered = covered_until(solar, recent_solar)
        if covered is None:
            return {}
        return {instant: row for instant, row in solar.items() if instant <= covered}

    def __complete_prefix(self, cursor: Optional[datetime.datetime], solar: Dict[datetime.datetime, SolarRow],
                          temperature: Dict[datetime.datetime, TemperatureRow], now: datetime.datetime) \
            -> Tuple[Dict[datetime.datetime, SolarRow], List[datetime.datetime]]:
        '''
        The longest run of instants above the cursor that this run may publish

        An instant is complete once the temperature of this run carries a row for it, and after MAX_HOLD it
        counts as complete without one. The first incomplete instant ends the run, because the cursor only moves
        forward: an instant published now can never be handed its temperature later.

        :param cursor: the newest published instant of the station, or None
        :param solar: Dict of instant to SolarRow this run may rely on
        :param temperature: Dict of instant to TemperatureRow as parsed, both kinds merged
        :param now: current instant, aware UTC
        :return: the prefix to publish, and the instants in it that go out without a temperature
        '''
        prefix: Dict[datetime.datetime, SolarRow] = {}
        bare: List[datetime.datetime] = []
        expired = now - MAX_HOLD
        for instant in sorted(solar):
            if cursor is not None and instant <= cursor:
                continue
            # A row carrying DWD's missing marker counts as covered: the field is left out of the message, and
            # a station whose sensor only delivers the marker must not stall its solar series.
            if not self.__with_temperature or instant in temperature:
                prefix[instant] = solar[instant]
                continue
            if instant < expired:
                prefix[instant] = solar[instant]
                bare.append(instant)
                continue
            break
        return prefix, bare

    def __log_waiting(self, station: Station, cursor: Optional[datetime.datetime],
                      solar: Collection[datetime.datetime], publishable: Collection[datetime.datetime],
                      bare: List[datetime.datetime], now: datetime.datetime) -> None:
        '''One line per run and station, never one per instant: what went out bare, and what is still waiting.'''
        if bare:
            log().warning("Published %s instants of station %s without a temperature, the oldest at %s: no "
                          "archive carried one within %s", len(bare), station.station_id, min(bare), MAX_HOLD)
        waiting = [instant for instant in solar
                   if (cursor is None or instant > cursor) and instant not in publishable]
        if not waiting:
            return
        oldest = min(waiting)
        if oldest < now - MAX_HOLD:
            # Only the midnight seam or a pending backfill can hold an instant this long; the temperature alone
            # would have let it go out bare.
            log().warning("Station %s has %s instants waiting longer than %s, the oldest at %s: this run's "
                          "archives do not carry them", station.station_id, len(waiting), MAX_HOLD, oldest)
        else:
            log().info("Holding back %s instants of station %s for a later run, the oldest at %s",
                       len(waiting), station.station_id, oldest)

    def __publish(self, station: Station, solar: Dict[datetime.datetime, SolarRow],
                  temperature: Dict[datetime.datetime, TemperatureRow]) -> int:
        cursor = self.__cursors[station.station_id]
        count = 0
        # The logger formats and serialises its line before the level check, which a historical block would pay
        # per row, so the level is read once here instead.
        debug = log().isEnabledFor(logging.DEBUG)
        for instant, solar_row, temperature_row in join_temperature(solar, temperature):
            if cursor is not None and instant <= cursor:
                continue
            value = build_message(station, solar_row, temperature_row, self.__with_temperature)
            if debug:
                log().debug("%s: %s", instant, value)
            self.__lib.put(instant, value)
            cursor = instant
            self.__cursors[station.station_id] = cursor
            count += 1
        return count
