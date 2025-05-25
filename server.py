from fastapi import FastAPI
from fastapi.responses import FileResponse
import os

app = FastAPI()

#  файл существует
OVERLAY_FILE = "overlay.html"

@app.get("/overlay")
async def get_overlay():
    if not os.path.exists(OVERLAY_FILE):
        return {"error": "Overlay file not found"}, 404
    return FileResponse(OVERLAY_FILE)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
