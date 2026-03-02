"""InsightForge - AI-Powered Market Research & Business Analysis Platform.

Product of Autonomous AI Corporation.
"""

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.database import init_db

load_dotenv(override=True)

app = FastAPI(
    title="InsightForge",
    description="AI-powered market research and business analysis",
    version="0.4.0",
)

# Mount static files if directory exists
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(router)


@app.on_event("startup")
def startup():
    init_db()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        reload=True,
    )
