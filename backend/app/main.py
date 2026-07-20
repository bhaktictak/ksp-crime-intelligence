from fastapi import FastAPI

app = FastAPI(
    title="KSP Crime Intelligence API",
    version="1.0.0"
)

@app.get("/")
def home():
    return {
        "message": "KSP Crime Intelligence Backend Running "
    }