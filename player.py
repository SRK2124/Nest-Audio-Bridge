import asyncio
import logging
from collections import deque
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any

from ytmusicapi import YTMusic
import yt_dlp
import pychromecast

logger = logging.getLogger(__name__)


@dataclass
class QueueTrack:
    """Represents a single track in the playback queue."""
    video_id: str
    title: str
    artist: str
    thumbnail_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class NestMediaStatusListener:
    """Listens to real-time status updates from the Google Nest speaker."""

    def __init__(self, player: "AsyncNestQueuePlayer", loop: asyncio.AbstractEventLoop):
        self.player = player
        self.loop = loop
        self.last_state = None

    def new_media_status(self, status):
        current_state = status.player_state
        idle_reason = status.idle_reason

        logger.debug(f"Media status event: state={current_state}, idle_reason={idle_reason}")

        # Detect natural track completion
        if (
            self.last_state in ("PLAYING", "BUFFERING")
            and current_state == "IDLE"
            and idle_reason == "FINISHED"
        ):
            logger.info("Track finished naturally. Triggering next song in queue...")
            asyncio.run_coroutine_threadsafe(self.player._on_track_finished(), self.loop)

        self.last_state = current_state


class AsyncNestQueuePlayer:
    """Async controller managing Chromecast sessions, audio extraction, and queue state."""

    def __init__(self, ip_address: str = "", port: int = 8009):
        # Can be initialized in the UI 
        if ip_address is not "":
            print("Initialization Failed: Ip address string is empty Please try again")

        self.ip_address = ip_address
        self.port = port
        self.cast: Optional[pychromecast.Chromecast] = None
        self.yt_api = YTMusic()
        
        self.queue: deque[QueueTrack] = deque()
        self.current_track: Optional[QueueTrack] = None
        self.is_playing: bool = False
        self.volume_level: float = 0.5
        
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._listener: Optional[NestMediaStatusListener] = None

    async def connect(self, ip_address: Optional[str] = None) -> bool:
        """Connects or reconnects to the Nest speaker at the target IP."""
        if ip_address:
            self.ip_address = ip_address

        self._loop = asyncio.get_running_loop()
        logger.info(f"Connecting to Google Nest at {self.ip_address}:{self.port}...")

        try:
            host_tuple = (self.ip_address, self.port, None, "", None)
            self.cast = await asyncio.to_thread(pychromecast.get_chromecast_from_host, host_tuple)
            await asyncio.to_thread(self.cast.wait, 10.0)

            # Register media state listener for auto-advancing queue items
            self._listener = NestMediaStatusListener(self, self._loop)
            self.cast.media_controller.register_status_listener(self._listener)

            logger.info(f"Successfully connected to Nest speaker: {self.cast.name}")
            return True
        except Exception as e:
            logger.exception(f"Failed to connect to Nest device at {self.ip_address}: {e}")
            self.cast = None
            return False

    async def search_tracks(self, query: str, limit: int = 5) -> List[QueueTrack]:
        """Searches YouTube Music for tracks matching the query."""
        try:
            results = await asyncio.to_thread(self.yt_api.search, query, filter="songs")
            if not results:
                return []

            tracks = []
            for item in results[:limit]:
                artists = ", ".join([a["name"] for a in item.get("artists", [])])
                thumb = item["thumbnails"][-1]["url"] if "thumbnails" in item and item["thumbnails"] else None
                tracks.append(
                    QueueTrack(
                        video_id=item["videoId"],
                        title=item["title"],
                        artist=artists,
                        thumbnail_url=thumb,
                    )
                )
            return tracks
        except Exception as e:
            logger.error(f"Failed searching YTMusic for query '{query}': {e}")
            return []

    async def _extract_stream_url(self, video_id: str) -> Optional[str]:
        """Extracts direct audio URL just-in-time using yt-dlp."""
        url = f"https://music.youtube.com/watch?v={video_id}"
        ydl_opts: Dict[str, Any] = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
        }

        def _extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            info = await asyncio.to_thread(_extract)
            return info.get("url")
        except Exception as e:
            logger.error(f"Stream extraction failed for video ID '{video_id}': {e}")
            return None

    async def add_to_queue(self, track: QueueTrack) -> QueueTrack:
        """Appends a track to the queue and starts playback if idle."""
        self.queue.append(track)
        logger.info(f"Enqueued: {track.title} by {track.artist}")

        if not self.is_playing:
            await self.play_next()

        return track

    async def play_next(self) -> bool:
        """Pops and plays the next item from the queue."""
        if not self.cast:
            logger.error("Cannot play: Nest device is disconnected.")
            return False

        if not self.queue:
            logger.info("Queue is empty. Resetting playback state.")
            self.current_track = None
            self.is_playing = False
            return False

        track = self.queue.popleft()
        self.current_track = track

        stream_url = await self._extract_stream_url(track.video_id)
        if not stream_url:
            logger.error(f"Skipping unplayable track: {track.title}")
            return await self.play_next()

        try:
            mc = self.cast.media_controller
            logger.info(f"Casting: {track.title} - {track.artist}")

            # Send stream with Metadata (Type 3 = Music Track) to enable Voice Assistant controls
            mc.play_media(
                stream_url,
                "audio/mp4",
                title=track.title,
                metadata={
                    "metadataType": 3,
                    "title": track.title,
                    "artist": track.artist,
                    "images": [{"url": track.thumbnail_url}] if track.thumbnail_url else [],
                },
            )

            await asyncio.to_thread(mc.block_until_active, 10.0)
            self.is_playing = True
            return True
        except Exception as e:
            logger.exception(f"Error casting track {track.title}: {e}")
            return await self.play_next()

    async def _on_track_finished(self):
        self.is_playing = False
        await self.play_next()

    async def skip(self) -> bool:
        """Skips the currently playing song."""
        return await self.play_next()

    def pause(self):
        """Pauses audio playback."""
        if self.cast and self.cast.media_controller:
            self.cast.media_controller.pause()
            self.is_playing = False

    def resume(self):
        """Resumes audio playback."""
        if self.cast and self.cast.media_controller:
            self.cast.media_controller.play()
            self.is_playing = True

    def stop(self):
        """Stops playback and flushes the queue."""
        if self.cast and self.cast.media_controller:
            self.queue.clear()
            self.current_track = None
            self.is_playing = False
            self.cast.media_controller.stop()

    def set_volume(self, level: float):
        """Sets output volume between 0.0 and 1.0."""
        if self.cast:
            self.volume_level = max(0.0, min(1.0, level))
            self.cast.set_volume(self.volume_level)

    def get_status(self) -> Dict[str, Any]:
        """Gets snapshot of state for API status endpoints."""
        return {
            "connected": self.cast is not None,
            "device_name": self.cast.name if self.cast else None,
            "ip_address": self.ip_address,
            "is_playing": self.is_playing,
            "current_track": self.current_track.to_dict() if self.current_track else None,
            "queue": [t.to_dict() for t in self.queue],
            "volume": self.volume_level,
        }

    def disconnect(self):
        """Cleanly stops playback, clears all data, and disconnects from the Nest speaker."""
        logger.info("Server shutting down: Clearing data and disconnecting Nest...")
        
        # 1. Force the Nest to stop playing immediately
        if self.cast and self.cast.media_controller:
            try:
                self.cast.media_controller.stop()
            except Exception as e:
                logger.error(f"Error stopping playback during shutdown: {e}")
        
        # 2. Wipe all internal queue and state data
        self.queue.clear()
        self.current_track = None
        self.is_playing = False
        
        # 3. Disconnect the network socket
        if self.cast:
            self.cast.disconnect()
            self.cast = None
            logger.info("Nest disconnected successfully.")