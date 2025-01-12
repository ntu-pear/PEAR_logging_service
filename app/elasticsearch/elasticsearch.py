from elasticsearch import Elasticsearch, exceptions

class ElasticsearchService:
    def __init__(self, host: str, port: int, username: str = None, password: str = None):
        if username and password:
            self.client = Elasticsearch(
                hosts=[{"host": host, "port": port}],
                http_auth=(username, password),
            )
        else:
            self.client = Elasticsearch(hosts=[{"host": host, "port": port}])

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

    def search_documents(self, index_name: str, query: dict):
        return self.client.search(index=index_name, body=query)

    def delete_document(self, index_name: str, document_id: str):
        self.client.delete(index=index_name, id=document_id)
