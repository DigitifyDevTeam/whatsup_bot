from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI

from app.api.routes import router
from app.utils.logger import get_logger

logger = get_logger(__name__)

app = FastAPI(
    title="WhatsApp Task Processor",
    description="Converts WhatsApp messages into structured Teamwork tasks",
    version="1.0.0",
)

app.include_router(router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.on_event("startup")
async def startup_event() -> None:
    logger.info("FastAPI backend started")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
