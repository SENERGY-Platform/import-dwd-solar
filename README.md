# import-dwd-solar

Allows you to import the ten minute solar observations of configured DWD weather stations, optionally together with
the air temperature of the same stations. If configured in that way, historic and recent data will be imported first.
Afterwards, the latest data will be imported every 30 minutes.

The event time handed to the platform is the UTC instant of the observation's `MESS_DATUM`. The ten minute products
are stamped in UTC, unlike DWD's hourly and daily products.

## Outputs
*A column DWD marks as invalid or missing is left out of the message entirely, so a consumer can tell "not measured"
apart from a measured value. Each column is checked on its own, so one missing column does not affect the others of
the same instant.*
* global_irradiance_wm2 (float): mean global irradiance over the ten minute interval (W/m2), converted from GS_10
* diffuse_irradiance_wm2 (float): mean diffuse irradiance over the ten minute interval (W/m2), converted from DS_10
* sunshine_seconds (float): sunshine duration within the ten minute interval (s), converted from SD_10
* longwave_wm2 (float): mean downward longwave radiation over the ten minute interval (W/m2), converted from LS_10
* global_irradiance_raw (float): global irradiance as delivered by DWD (J/cm2 per 10 min), unconverted
* temperature_2m (float): temperature in 2 m height (°C). Only present if WITH_TEMPERATURE is set and DWD delivered a
  measured value for that instant.
* meta (Object):
  + quality_level (int): DWD quality level, see [explanation](https://www.dwd.de/DE/leistungen/klimadatendeutschland/qualitaetsniveau.html).
    Only present if DWD delivered one for that instant.
  + name (string): station name
  + id (string): station id, zero padded as in the DWD station list
  + lat (float): station latitude
  + long (float): station longitude
  + height (float): station height
  + units (Object): the unit of every value above, by name

## Configs
 * STATION_IDS (List of strings): the DWD station ids to import, zero padded as in the
   [station list](https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/solar/recent/zehn_min_sd_Beschreibung_Stationen.txt),
   for example ["02932"]. **Leave it empty to import every station of the list.** One instance covering all of them
   is what lets a consumer pick its station out of the export, by id or by distance to a coordinate, instead of one
   instance and one export per station. A map of all DWD stations is
   available [here](https://www.dwd.de/DE/leistungen/klimadatendeutschland/mnetzkarten/messnetz_solar.pdf?__blob=publicationFile&v=6).
 * HISTORIC (bool): If true, all available historic data will be imported - every block DWD keeps, which is decades
   per station. Default: false
 * RECENT (bool): If true, the recent archive will be imported. It reaches about 550 days back, measured against the
   published archive, so it covers a replay of the last year on its own. Default: false
 * WITH_TEMPERATURE (bool): If true, the air temperature of the same stations is joined onto the solar
   observations. Default: true

While the temperature is joined, a scheduled run publishes the longest unbroken run of solar instants above the
station's cursor for which DWD's air temperature archives carry a row, and stops at the first instant they do not.
Everything from there on waits for a later run. An instant is published only once and the cursor never goes back,
so a solar row sent before its temperature exists would stay without it forever. A row DWD delivers with its
missing marker counts as carried: the `temperature_2m` field is left out, because the meter did report at that
instant.

That wait is bounded at two hours. An instant older than that is published without `temperature_2m`, so a hole DWD
never fills costs one field rather than stalling the solar series behind it. The export therefore runs up to two
hours behind DWD in the worst case and one archive refresh behind in the normal one.

Around midnight the tail of the previous day waits longer than that, until DWD regenerates the recent archives at
about 01:15 UTC: the now archives have rolled over to the new day by then, so between midnight and the
regeneration the tail is in no archive at all and the cursor must not step over it. A run that is still waiting
after two hours says so once, naming the oldest instant it holds.

A row the regenerated recent archive itself does not carry cannot be recovered, because the cursor has to move on
past it. Normally that is the last row of a day and costs one instant. A recent archive that ends further back
than that loses the tail of the previous day above the point it ends, because the check compares whole days rather
than single steps: a legitimately absent last row must not stall the series for a day.

At startup a station whose recent air temperature archive does not answer, or answers without rows, keeps its
cursor where it is and is logged as an error. Its backfill counts as pending: every scheduled run retries it and
leaves the station's now archive alone until it succeeds, so a missing archive neither restarts the import in a
loop nor silently skips the days the backfill owes. The other stations keep running.

The historical import is not affected by any of this, it reads the temperature blocks of a day alongside the solar
ones.

---

This tool uses publicly available data provided by Deutscher Wetterdienst.
