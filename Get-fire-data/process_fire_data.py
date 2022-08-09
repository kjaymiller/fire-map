import json
import logging
import os   
from uuid import uuid4
import asyncio


from azure.cosmos.aio import CosmosClient
from azure.cosmos import PartitionKey
import pandas as pd
from arcgis import gis

g = gis.GIS()
fire_data_item_id = "6db1b46d55dc4145b93d8eb8e525906c"

async def clean_container(
    database: str,
    container: str,
):
    """Delete the existing container and create a new one with the same name"""
    async with CosmosClient.from_connection_string(
        os.environ.get("COSMOS_CONNECTION_STRING", None)
    ) as client:
        database = "recent-fire"
        logging.info(f"Cleaning {container=}")

        try:
            await client.get_database_client(database).delete_container(container=container)
        except Exception as e:
            pass

        await client.get_database_client(database).create_container(id=container, partition_key=PartitionKey(path="/id", kind="Hash"))
        logging.info(f"Finished cleaning {container=}")
    

def write_new_file_data() -> str:
    """Returns a JSON String of the Dataframe"""
    logging.info("Loading data from ArcGIS")
    fire_data = g.content.get(fire_data_item_id)
    logging.info("Converting to Pandas Dataframe")
    fire_df = pd.DataFrame.spatial.from_layer(fire_data.layers[0])
    logging.info("Converting to JSON")
    return fire_df.to_json(orient="records")


async def write_to_cosmos(
    json_string: str,
    database: str,
    container: str
):
    """Add the given JSON string to COSMOSDB"""
    logging.info("Connecting to CosmosDB")
    
    async with CosmosClient.from_connection_string(
        os.environ.get("COSMOS_CONNECTION_STRING", None)
    ) as client:
        logging.info(f"Writing to CosmosDB {database=} in {container=}") 
        container = client.get_database_client(database=database).get_container_client(container=container)

        logging.info("Writing to CosmosDB Container")
        data = json.loads(json_string)
        logging.info(f"Writing {len(data)} records to {container=}")
        
        for record in data:
            record["id"] = str(uuid4())
            await container.upsert_item(record)
        logging.info(f"Finished writing {len(data)} records to {container=}")
    return


async def load_and_write(database: str, container: str):
    """Load the data from arcgis and write to cosmos"""
    await clean_container(database=database, container=container)
    json_string = write_new_file_data()
    await write_to_cosmos(json_string, database=database, container=container)


if __name__ == "__main__":
    database=os.environ.get("COSMOS_DATABASE", None)
    container=os.environ.get("COSMOS_CONTAINER", None)
    asyncio.run(load_and_write(database=database, container=container))
