import os 
import dotenv
from azure.cosmos.aio import CosmosClient as AsyncCosmosClient
from azure.cosmos import CosmosClient


dotenv.load_dotenv()

COSMOS_DB = os.environ.get('COSMOS_DATABASE', None)
COSMOS_CONTAINER = os.environ.get('COSMOS_CONTAINER', None)
COSMOS_CONNECTION_STRING = os.environ.get('COSMOS_CONNECTION_STRING', None)

client = CosmosClient.from_connection_string(conn_str=COSMOS_CONNECTION_STRING)
database = client.get_database_client(database=COSMOS_DB)
container = database.get_container_client(container=COSMOS_CONTAINER)

async_client = AsyncCosmosClient.from_connection_string(conn_str=COSMOS_CONNECTION_STRING)