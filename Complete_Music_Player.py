import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

from ytmusicapi import YTMusic
import yt_dlp
import pychromecast
from pychromecast.controllers.media import MediaController

logger = logging.getLogger(__name__)

@dataclass
class QueueTrack:
    """Represents a song item in the queue."""
    video_id: str
    title: str
    artist: str
    thumbnail_url: Optional[str] = None


class NestMediaStatusListener:
    """Listens for state changes from the Google Nest speaker."""

    def __init__(self, player: 'AsyncNestQueuePlayer', loop: asyncio.AbstractEventLoop):
        self.player = player
        self.loop = loop
        self.last_state = None

    def new_media_status(self, status):
        """Callback invoked by PyChromecast on a background thread when media state changes."""
        current_state = status.player_state
        idle_reason = status.idle_reason

        logger.debug(f"Media status updated: State={current_state}, IdleReason={idle_reason}")

        # Track transitioned to IDLE because it finished playing naturally
        if (
            self.last_state in ("PLAYING", "BUFFERING") 
            and current_state == "IDLE" 
            and idle_reason == "FINISHED"
        ):
            logger.info("Current track finished. Triggering next track in queue...")
            # Safely schedule the async play_next method on the main event loop
            asyncio.run_coroutine_threadsafe(self.player._on_track_finished(), self.loop)

        self.last_state = current_state


class AsyncNestQueuePlayer:
    """Production-ready asynchronous queue-enabled player for Google Nest."""

    def __init__(self, ip_address: str, port: int = 8009):
        self.ip_address = ip_address
        self.port = port
        self.cast: Optional[pychromecast.Chromecast] = None
        self.yt_api = YTMusic()
        
        # Queue state
        self.queue: deque[QueueTrack] = deque()
        self.current_track: Optional[QueueTrack] = None
        self.is_playing: bool = False
        
        # Async event loop reference for thread-safe callbacks
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._listener: Optional[NestMediaStatusListener] = None

    async def connect(self) -> bool:
        """Connects to the Nest device and registers state listeners."""
        self._loop = asyncio.get_running_loop()
        logger.info(f"Connecting to Nest at {self.ip_address}:{self.port}...")
        
        try:
            host_tuple = (self.ip_address, self.port, None, '', None)
            self.cast = await asyncio.to_thread(pychromecast.get_chromecast_from_host, host_tuple)
            await asyncio.to_thread(self.cast.wait, 10.0)
            
            # Register media status listener for auto-advancing tracks
            self._listener = NestMediaStatusListener(self, self._loop)
            self.cast.media_controller.register_status_listener(self._listener)
            
            logger.info(f"Connected to {self.cast.name} with Queue & Auto-Play active!")
            return True
        except Exception as e:
            logger.exception(f"Failed to connect to Nest at {self.ip_address}: {e}")
            self.cast = None
            return False

    async def search_track(self, query: str) -> Optional[QueueTrack]:
        """Searches YTMusic for metadata without extracting heavy stream URLs yet."""
        try:
            results = await asyncio.to_thread(self.yt_api.search, query, filter="songs")
            if not results:
                logger.warning(f"No results found for query: '{query}'")
                return None

            match = results[0]
            artists = ", ".join([a['name'] for a in match.get('artists', [])])
            thumb = match['thumbnails'][-1]['url'] if 'thumbnails' in match else None
            
            return QueueTrack(
                video_id=match['videoId'],
                title=match['title'],
                artist=artists,
                thumbnail_url=thumb
            )
        except Exception as e:
            logger.error(f"Search failed for '{query}': {e}")
            return None

    async def _extract_stream_url(self, video_id: str) -> Optional[str]:
        """Just-in-time extraction of the stream URL using yt-dlp."""
        youtube_url = f"https://music.youtube.com/watch?v={video_id}"
        ydl_opts: Dict[str, Any] = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False
        }

        def extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(youtube_url, download=False)

        try:
            info = await asyncio.to_thread(extract)
            return info.get('url')
        except Exception as e:
            logger.error(f"Stream extraction failed for video ID {video_id}: {e}")
            return None

    async def add_to_queue(self, query: str) -> Optional[QueueTrack]:
        """Adds a track to the end of the queue. If nothing is playing, starts immediately."""
        track = await self.search_track(query)
        if not track:
            return None

        self.queue.append(track)
        logger.info(f"Enqueued: {track.title} by {track.artist}")

        # If idle, start playback immediately
        if not self.is_playing:
            await self.play_next()

        return track

    async def play_next(self) -> bool:
        """Plays the next track in the queue."""
        if not self.cast:
            logger.error("Cannot play: Not connected to Nest.")
            return False

        if not self.queue:
            logger.info("Queue is empty. Playback stopped.")
            self.current_track = None
            self.is_playing = False
            return False

        # Pop next track from queue
        track = self.queue.popleft()
        self.current_track = track

        # Extract direct stream URL
        stream_url = await self._extract_stream_url(track.video_id)
        if not stream_url:
            logger.error(f"Skipping unplayable track: {track.title}")
            return await self.play_next()

        try:
            mc: MediaController = self.cast.media_controller
            logger.info(f"Now Playing: {track.title} by {track.artist}")
            
            mc.play_media(
                stream_url,
                'audio/mp4',
                title=track.title,
                metadata={
                    'metadataType': 3,  # Music Track metadata type
                    'title': track.title,
                    'artist': track.artist,
                    'images': [{'url': track.thumbnail_url}] if track.thumbnail_url else []
                }
            )
            
            await asyncio.to_thread(mc.block_until_active, 10.0)
            self.is_playing = True
            return True
            
        except Exception as e:
            logger.exception(f"Error casting track {track.title}: {e}")
            return await self.play_next()

    async def _on_track_finished(self):
        """Internal callback when current track finishes."""
        self.is_playing = False
        await self.play_next()

    async def skip(self) -> bool:
        """Skips the currently playing track."""
        logger.info("Skipping current track...")
        return await self.play_next()

    def pause(self):
        """Pauses current playback (Synchronous)."""
        if self.cast and self.cast.media_controller:
            logger.info("Pausing playback...")
            self.cast.media_controller.pause()

    def resume(self):
        """Resumes playback (Synchronous)."""
        if self.cast and self.cast.media_controller:
            logger.info("Resuming playback...")
            self.cast.media_controller.play()

    def stop(self):
        """Stops playback and clears state (Synchronous)."""
        if self.cast and self.cast.media_controller:
            logger.info("Stopping playback and resetting queue.")
            self.queue.clear()
            self.current_track = None
            self.is_playing = False
            self.cast.media_controller.stop()

    def set_volume(self, level: float):
        """Sets volume level between 0.0 and 1.0 (Synchronous)."""
        if self.cast:
            safe_level = max(0.0, min(1.0, level))
            self.cast.set_volume(safe_level)

    def get_queue(self) -> List[Dict[str, str]]:
        """Returns a list of items currently in the queue."""
        return [
            {"title": t.title, "artist": t.artist, "video_id": t.video_id}
            for t in self.queue
        ]

    def disconnect(self):
        """Cleanly disconnects from the Nest speaker."""
        if self.cast:
            self.cast.disconnect()
            self.cast = None