import logging

import azure.functions as func
from api.app import api
from src.db.db import write_to_cosmos
from src.viirs.get_fire_data import get_fire_data
import nest_asyncio

nest_asyncio.apply()
app = func.FunctionApp()

@app.function_name(name="mytimer")
@app.schedule(
    # schedule="0 */10 * * * *",
    # Cron expression for 8-10 and 18-21 every day
    schedule="0 8-10,18-21 * * *",
    arg_name="mytimer",
    use_monitor=True,
) 
def load_cosmos(mytimer: func.TimerRequest) -> None:
    """Fetch the time"""
    logging.info("Python timer trigger function ran at --")
    write_to_cosmos(get_fire_data(country="USA"))

# @app.function_name(name="HttpTrigger")
# @app.route(route="/{*route}")
# async def main(req: func.HttpRequest, context: func.Context) -> func.HttpResponse:
#     logging.info(f"{req=}, {context=}")
#     return func.AsgiMiddleware(api).handle(req, context)


