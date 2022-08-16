# MODIS Fire Detection Azure Functions

MODIS (Moderate Resolution Imaging Spectroradiometer) is a sensor that measures brightness and radiation on the Earth's surface. This sensor exists on two satellites: Aqua and Terra and can be used to hotspots and fires around the globe.

This project allows you to search for poetential fires detected by MODIS.

Results are pulled from [the MODIS_Thermal_v1 (FeatureServer)][MODIS Dataset]. The FeatureServer is updated every 30 minutes but it can take take up to 4 hours for data to process. This project checks for new data every 30 minutes storing the results in a CosmosDB Container. Data is removed from the every 30 minutes hour.

## License and Usage
> **Warning**
> This is for education/demo/noncommercial purposes only and should not be used for fire prevention/avoidance.

The code to build these functions are licensed under the [MIT License](./LICENSE.md).

The Example Data is the Copyright (c) of Esri and NASA. For questions on data usage, visit [Esri's Terms of Use](https://www.esri.com/en-us/legal/overview).


## Azure Functions
There are two Azure Functions that are used to pull data from the FeatureServer, store the results in a CosmosDB Container, and serve data to the user.

### Timer Trigger
The first is the [timer trigger](Get-fire-data/readme.md) that checks for new data at every half hour. This data is pulled from the [MODIS Dataset] and stored in a CosmosDB Container. This allows us to query the data as much as possible without running up against rate-limits from Esri.

### HTTP Trigger
Users access the data using the [HTTP Trigger](HttpTrigger/readme.md). This uses FastAPI to serve the data to the user either as HTTP or JSON.

A user needs to provide their location and a distance range in meters. The service polls the CosmosDB Container for data within the range.

## CosmosDB

[CosmosDB] is a NoSQL database that is used to store data. It is a document database that stores data in JSON format and can be queried using SQL-like queries or with other APIs like MongoDB, Cassandra, Gremlin, or Table API.

[MODIS Dataset]: https://services9.arcgis.com/RHVPKKiFTONKtxq3/ArcGIS/rest/services/MODIS_Thermal_v1/FeatureServer
[COSMOSDB]: https://docs.microsoft.com/en-us/azure/cosmos-db/