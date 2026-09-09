# import-dwd-solar

Allows you to import the ten minute solar observations of configured DWD weather stations, optionally together with
the air temperature of the same stations. If configured in that way, historic and recent data will be imported first.
Afterwards, the latest data will be imported every 30 minutes.

The event time handed to the platform is the UTC instant of the observation's `MESS_DATUM`. The ten minute products
are stamped in UTC, unlike DWD's hourly and daily products.

## Outputs
*Outputs may contain the value -999 to indicate invalid or missing data. Each column carries that marker on its own,
so one missing column does not affect the others of the same instant.*
* global_irradiance_wm2 (float): mean global irradiance over the ten minute interval (W/m2), converted from GS_10
* diffuse_irradiance_wm2 (float): mean diffuse irradiance over the ten minute interval (W/m2), converted from DS_10
* sunshine_seconds (float): sunshine duration within the ten minute interval (s), converted from SD_10
* longwave_wm2 (float): mean downward longwave radiation over the ten minute interval (W/m2), converted from LS_10
* global_irradiance_raw (float): global irradiance as delivered by DWD (J/cm2 per 10 min), unconverted
* temperature_2m (float): temperature in 2 m height (°C). Only present if WITH_TEMPERATURE is set and DWD has an air
  temperature row for that instant.
* meta (Object):
  + quality_level (int): DWD quality level, see [explanation](https://www.dwd.de/DE/leistungen/klimadatendeutschland/qualitaetsniveau.html)
  + name (string): station name
  + id (string): station id, zero padded as in the DWD station list
  + lat (float): station latitude
  + long (float): station longitude
  + height (float): station height
  + units (Object): the unit of every value above, by name

## Configs
 * STATION_IDS (List of strings): the DWD station ids to import, zero padded as in the
   [station list](https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/solar/recent/zehn_min_sd_Beschreibung_Stationen.txt),
   for example ["02932"]. Required; the import refuses to start without at least one id. A map of all DWD stations is
   available [here](https://www.dwd.de/DE/leistungen/klimadatendeutschland/mnetzkarten/messnetz_solar.pdf?__blob=publicationFile&v=6).
 * HISTORIC (bool): If true, all available historic data will be imported. Default: false
 * RECENT (bool): If true, all recent data (roughly the last 500 days) will be imported. Default: false
 * WITH_TEMPERATURE (bool): If true, the air temperature of the same stations is joined onto the solar
   observations. Default: true

---

This tool uses publicly available data provided by Deutscher Wetterdienst.
