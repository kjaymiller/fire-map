import logging
from arcgis import GIS
from arcgis.features.feature import FeatureSet
g = GIS()

def write_new_file_data(gis_id:str, layer:int=0) -> FeatureSet:
    """Returns a JSON String of the Dataframe"""
    logging.info("Loading Featured Layer from ArcGIS")
    fire_data = g.content.get(gis_id)
    feature = fire_data.layers[layer]
    logging.info("Performing Query on Feature Layer")
    q = feature.query(
        where="confidence >= 65 AND hours_old  <= 4",
        return_distince_values=True,
        out_fields="confidence, hours_old",
        out_sr=4326,
    )
    return q    


if __name__ == "__main__":
    fire_data_item_id = "b8f4033069f141729ffb298b7418b653"
    print(write_new_file_data(fire_data_item_id))