import os
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "webapp.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("WEBAPP_PORT", "8080")),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )
