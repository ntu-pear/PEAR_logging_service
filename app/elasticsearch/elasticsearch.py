from elasticsearch import Elasticsearch, exceptions
import logging
import os
from dotenv import load_dotenv
logger = logging.getLogger("uvicorn")
load_dotenv()

ES_HOST= os.getenv("ES_HOST")
ES_PORT = int(os.getenv("ES_PORT"))
ES_USERNAME=os.getenv("ES_USERNAME")
ES_PASSWORD=os.getenv("ES_PASSWORD")
class ElasticsearchService:
    def __init__(self, host: str, port: int, username: str, password: str ):
        self.client = Elasticsearch(
            hosts=[{"host": host, "port": port, "scheme": "http"}],
            http_auth=(username, password),
        )

    def is_connected(self):
        try:
            return self.client.ping()
        except exceptions.ConnectionError:
            return False
    
    def create_index(self, index_name: str, mappings: dict):
        if not self.client.indices.exists(index=index_name):
            self.client.indices.create(index=index_name, body=mappings)

    def insert_document(self, index_name: str, document_id: str, document: dict):
        self.client.index(index=index_name, id=document_id, body=document)

    def get_document(self, index_name: str, document_id: str):
        return self.client.get(index=index_name, id=document_id)

    def search_documents(self, index: str, body: dict, headers: str):
        return self.client.search(index=index, body=body, headers = headers)

    def delete_document(self, index_name: str, document_id: str):
        self.client.delete(index=index_name, id=document_id)

es_service = ElasticsearchService(
    host=ES_HOST,
    port=ES_PORT,
    username=ES_USERNAME,
    password=ES_PASSWORD,
)