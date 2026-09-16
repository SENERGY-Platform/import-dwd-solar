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
import sys
import time

import schedule
from import_lib.import_lib import ImportLib, get_logger

from lib.dwd.importer import SolarImport, as_utc
from lib.dwd.stations import fetch_station_list, stations_for

if __name__ == '__main__':
    lib = ImportLib("github.com/SENERGY-Platform/import-dwd-solar")
    logger = get_logger(__name__)

    station_ids = lib.get_config("STATION_IDS", None)
    if station_ids is not None and not isinstance(station_ids, list):
        # a bare string here is a typo, and reading it as "unset" would quietly
        # import every station instead of the one that was meant
        logger.error("Config STATION_IDS must be a list of DWD station ids, for example [\"02932\"], or unset for all")
        sys.exit(1)

    with_temperature = lib.get_config("WITH_TEMPERATURE", True)

    stations, missing = stations_for(fetch_station_list(), station_ids)
    for station_id in missing:
        logger.error("Station %s is not in the DWD solar station list and will be skipped", station_id)
    if len(stations) == 0:
        logger.error("None of the configured station ids is in the DWD solar station list")
        sys.exit(1)
    if station_ids:
        logger.info("Importing %s stations: %s", len(stations),
                    ", ".join([station.station_id + " " + station.name for station in stations]))
    else:
        # naming all of them would be a log line of several thousand characters
        logger.info("No STATION_IDS configured, importing all %s stations of the DWD solar list", len(stations))

    last_published, _ = lib.get_last_published_datetime()
    last_published = as_utc(last_published)
    if last_published is None:
        logger.info("Import is starting fresh")
    else:
        logger.info("Import is continuing previous import at %s", last_published)

    solar_import = SolarImport(lib, stations, with_temperature)
    solar_import.seed_cursors(last_published)

    now = datetime.datetime.now(datetime.timezone.utc)

    if lib.get_config("HISTORIC", False):
        for station in stations:
            if solar_import.needs_historical(station, now):
                logger.info("Importing historic data of station %s...", station.station_id)
                solar_import.import_historical(station)
            else:
                logger.info("Skipping historic data of station %s (already done)", station.station_id)
    else:
        logger.info("Skipping historic data (not configured)")

    if lib.get_config("RECENT", False):
        for station in stations:
            if solar_import.needs_recent(station, now):
                logger.info("Importing recent data of station %s...", station.station_id)
                solar_import.import_recent(station)
            else:
                logger.info("Skipping recent data of station %s (already done)", station.station_id)
    else:
        logger.info("Skipping recent data (not configured)")

    solar_import.import_latest()

    logger.info("Setting schedule to run every 30 minutes")
    schedule.every(30).minutes.do(solar_import.import_latest)

    while True:
        schedule.run_pending()
        time.sleep(1)
