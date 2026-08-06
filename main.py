from fastapi import FastAPI

from config import settings

app = FastAPI(title="Competitor Agent")


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=settings.web_host, port=settings.web_port, reload=True)
