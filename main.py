import logging
from contextlib import asynccontextmanager
import os
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from player import AsyncNestQueuePlayer, QueueTrack

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

player = AsyncNestQueuePlayer(ip_address=os.getenv("NEST_IP"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP PHASE ---
    # Runs the moment uvicorn starts
    await player.connect()
    
    yield  # The server is now actively running and accepting requests
    
    # --- SHUTDOWN PHASE ---
    # Runs immediately when you press Ctrl+C or kill the server
    player.disconnect()


app = FastAPI(title="Google Nest Music Controller", lifespan=lifespan)


class ConnectRequest(BaseModel):
    ip_address: str


class SearchRequest(BaseModel):
    query: str


class AddQueueRequest(BaseModel):
    video_id: str
    title: str
    artist: str
    thumbnail_url: Optional[str] = None


class VolumeRequest(BaseModel):
    level: float


@app.get("/")
async def get_index():
    return FileResponse("index.html")


@app.post("/api/connect")
async def connect_device(req: ConnectRequest):
    success = await player.connect(req.ip_address)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to connect to Nest speaker")
    return player.get_status()


@app.post("/api/search")
async def search_music(req: SearchRequest):
    results = await player.search_tracks(req.query)
    return {"results": [t.to_dict() for t in results]}


@app.post("/api/queue/add")
async def add_to_queue(req: AddQueueRequest):
    track = QueueTrack(
        video_id=req.video_id,
        title=req.title,
        artist=req.artist,
        thumbnail_url=req.thumbnail_url,
    )
    enqueued = await player.add_to_queue(track)
    return {"status": "enqueued", "track": enqueued.to_dict()}


@app.get("/api/status")
async def get_status():
    return player.get_status()


@app.post("/api/control/pause")
async def pause_playback():
    player.pause()
    return {"status": "paused"}


@app.post("/api/control/resume")
async def resume_playback():
    player.resume()
    return {"status": "resumed"}


@app.post("/api/control/skip")
async def skip_track():
    await player.skip()
    return {"status": "skipped"}


@app.post("/api/control/stop")
async def stop_playback():
    player.stop()
    return {"status": "stopped"}


@app.post("/api/control/volume")
async def set_volume(req: VolumeRequest):
    player.set_volume(req.level)
    return {"status": "volume_updated", "level": req.level}