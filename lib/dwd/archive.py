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

import io
import logging
import re
import zipfile
from datetime import date
from typing import Callable, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/"

# (directory, file name code) of the two products this import reads.
SOLAR = ("solar", "SOLAR")
TEMPERATURE = ("air_temperature", "TU")

NOW = "now"
RECENT = "recent"
HISTORICAL = "historical"

STATION_LIST_URL = BASE_URL + "solar/recent/zehn_min_sd_Beschreibung_Stationen.txt"

HTTP_TIMEOUT = 300

# The zip member holding the observations; the archives also carry metadata files.
PRODUCT_MEMBER_PREFIX = "produkt_"


def get_bytes(url: str) -> Optional[bytes]:
    '''
    Downloads a URL

    :param url: the URL
    :return: the content, or None if the server did not answer with a success status
    '''
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    if not r.ok:
        return None
    return r.content


def product_url(product: Tuple[str, str], kind: str, station_id: str) -> str:
    '''
    Builds the URL of the now or recent archive of one station

    :param product: SOLAR or TEMPERATURE
    :param kind: NOW or RECENT
    :param station_id: zero padded DWD station id
    :return: the URL
    '''
    directory, code = product
    suffix = {NOW: "now", RECENT: "akt"}[kind]
    return BASE_URL + directory + "/" + kind + "/10minutenwerte_" + code + "_" + station_id + "_" + suffix + ".zip"


def historical_url(product: Tuple[str, str], station_id: str, date_from: str, date_to: str) -> str:
    '''
    Builds the URL of one historical archive block

    :param product: SOLAR or TEMPERATURE
    :param station_id: zero padded DWD station id
    :param date_from: first day of the block as YYYYmmdd
    :param date_to: last day of the block as YYYYmmdd
    :return: the URL
    '''
    directory, code = product
    return BASE_URL + directory + "/" + HISTORICAL + "/10minutenwerte_" + code + "_" + station_id + "_" \
        + date_from + "_" + date_to + "_hist.zip"


def read_products(raw: bytes) -> List[str]:
    '''
    Reads the observation files out of a DWD archive

    Spaces are stripped from every line: the fields are semicolon separated and padded, and no field of these
    products legitimately contains a space.

    :param raw: content of the zip
    :return: List of the decoded contents of all product files in the zip
    '''
    texts = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        for name in names:
            if not name.split("/")[-1].startswith(PRODUCT_MEMBER_PREFIX):
                continue
            texts.append(archive.read(name).decode().replace(" ", ""))
    if not texts:
        logger.error("Archive carries no product file, only " + str(names))
    return texts


def fetch_product(product: Tuple[str, str], kind: str, station_id: str,
                  fetch: Callable[[str], Optional[bytes]] = get_bytes) -> Optional[List[str]]:
    '''
    Downloads the now or recent archive of one station and reads its observation files

    :param product: SOLAR or TEMPERATURE
    :param kind: NOW or RECENT
    :param station_id: zero padded DWD station id
    :param fetch: Callable that turns a URL into its content, or None if unavailable
    :return: List of decoded product file contents, or None if the archive was not available
    '''
    url = product_url(product, kind, station_id)
    raw = fetch(url)
    if raw is None:
        logger.error("No " + kind + " archive at " + url + ". Station still active?")
        return None
    return read_products(raw)


def fetch_historical_blocks(product: Tuple[str, str], station_id: str,
                            fetch: Callable[[str], Optional[bytes]] = get_bytes) \
        -> List[Tuple[date, date, str]]:
    '''
    Lists the historical archive blocks of one station, oldest first

    The block file names carry their day range and the HTTPS directory listing is the only index of them.

    :param product: SOLAR or TEMPERATURE
    :param station_id: zero padded DWD station id
    :param fetch: Callable that turns a URL into its content, or None if unavailable
    :return: List of (first day, last day, URL), sorted by first day
    '''
    directory, code = product
    index_url = BASE_URL + directory + "/" + HISTORICAL + "/"
    raw = fetch(index_url)
    if raw is None:
        logger.error("Could not list " + index_url)
        return []
    index = raw.decode("utf-8", "replace")
    pattern = re.compile(r"10minutenwerte_" + code + "_" + re.escape(station_id) + r"_(\d{8})_(\d{8})_hist\.zip")
    blocks = []
    for date_from, date_to in sorted(set(pattern.findall(index))):
        try:
            first = _as_date(date_from)
            last = _as_date(date_to)
        except ValueError as e:
            logger.error(e)
            continue
        blocks.append((first, last, historical_url(product, station_id, date_from, date_to)))
    return blocks


def fetch_historical_block(url: str, fetch: Callable[[str], Optional[bytes]] = get_bytes) -> Optional[List[str]]:
    '''
    Downloads one historical archive block and reads its observation files

    :param url: URL of the block, from fetch_historical_blocks
    :param fetch: Callable that turns a URL into its content, or None if unavailable
    :return: List of decoded product file contents, or None if the block was not available
    '''
    raw = fetch(url)
    if raw is None:
        logger.error("No historical archive at " + url)
        return None
    return read_products(raw)


def blocks_overlapping(blocks: List[Tuple[date, date, str]], first: date, last: date) -> List[str]:
    '''
    Picks the blocks whose day range overlaps a window, used to find the temperature blocks belonging to a solar one

    :param blocks: List of (first day, last day, URL)
    :param first: first day of the window
    :param last: last day of the window
    :return: List of URLs
    '''
    return [url for block_first, block_last, url in blocks if block_last >= first and block_first <= last]


def _as_date(stamp: str) -> date:
    return date(int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]))
