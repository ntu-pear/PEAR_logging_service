from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers.logs import router as logs_router

app = FastAPI()
app.include_router(logs_router, prefix="/api")

origins = [
    "http://localhost:5173",
    "http://10.96.188.172:5173/"
]

# Middleware for CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"message": "Welcome to PEAR Logging Service!"}