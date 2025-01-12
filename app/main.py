from fastapi import FastAPI
from app.routers.logs import router as logs_router

app = FastAPI()
app.include_router(logs_router, prefix="/api")

@app.get("/")
async def root():
    return {"message": "Welcome to PEAR Logging Service!"}