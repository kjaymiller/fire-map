import datetime
import logging

import azure.functions as func
from arcgis import gis
import pandas as pd
import pathlib

g = gis.GIS()
fire_data_item_id = "6db1b46d55dc4145b93d8eb8e525906c"

def main(mytimer: func.TimerRequest) -> None:
    utc_timestamp = datetime.datetime.utcnow().replace(
        tzinfo=datetime.timezone.utc).isoformat()

    if mytimer.past_due:
        logging.info('The timer is past due!')
        fire_data = g.content.get(fire_data_item_id)
        fire_df = pd.DataFrame.spatial.from_layer(fire_data.layers[0])
        fire_df.to_json(pathlib.Path("output.json"))