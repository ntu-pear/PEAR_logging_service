import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    ES_HOST: str = os.getenv("ES_HOST", "localhost")
    ES_PORT: int = int(os.getenv("ES_PORT", 9200))
    ES_USERNAME: str = os.getenv("ES_USERNAME", None)
    ES_PASSWORD: str = os.getenv("ES_PASSWORD", None)

settings = Settings()
