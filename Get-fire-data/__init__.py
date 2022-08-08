import datetime
import logging

import azure.functions as func
from arcgis import gis
import pandas as pd
import pathlib

g = gis.GIS()
fire_data_item_id = "6db1b46d55dc4145b93d8eb8e525906c"


def write_new_file_data() -> str:
    """Returns a JSON String of the Dataframe"""
    fire_data = g.content.get(fire_data_item_id)
    fire_df = pd.DataFrame.spatial.from_layer(fire_data.layers[0])
    return fire_df.to_json(orient="records")


async def write_to_cosmos(json_string: str):
    """"""
    pass # TODO: Write data to CosmosDB



def main(mytimer: func.TimerRequest) -> None:
    utc_timestamp = datetime.datetime.utcnow().replace(
        tzinfo=datetime.timezone.utc).isoformat()

    if mytimer.past_due:
        logging.info('The timer is past due!')

    return write_new_file_data()

if __name__ == "__main__":
    write_new_file_data()
    # main()