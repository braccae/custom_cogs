import asyncio
import queue
import io
import discord
from discord import app_commands
from redbot.core import commands, Config
import discord.ext.voice_recv as voice_recv
from google import genai
from google.genai import types
import audioop
import logging

log = logging.getLogger("red.geminivoice")

def _restore_original_voice_recv():
    """Restores the original _decode_packet method to discord-ext-voice-recv if reloaded."""
    try:
        import importlib
        import discord.ext.voice_recv.opus as voice_recv_opus
        importlib.reload(voice_recv_opus)
        log.info("Restored original discord.ext.voice_recv.opus implementation (no monkeypatches).")
    except Exception:
        log.exception("Failed to restore original voice_recv:")



class GeminiVoiceSink(voice_recv.AudioSink):
    """Sink that receives Opus audio from voice_recv and streams PCM to Gemini Live API.
    
    Uses wants_opus()=True because voice_recv's internal decoder crashes
    (unhandled OpusError) on DAVE-supplemented packets. We handle Opus
    decoding ourselves with proper error recovery.
    
    All decoded audio is streamed to Gemini continuously — Gemini's 
    server-side VAD handles speech detection and turn-taking.
    """
    
    def __init__(self, session, loop):
        super().__init__()
        self.session = session
        self.loop = loop
        self._send_count = 0
        self._last_send_log = 0
        self.decoders = {}
        self._consecutive_errors = {}  # Track consecutive decode failures per SSRC
        self.audio_queue = queue.Queue()
        self.sender_task = asyncio.run_coroutine_threadsafe(self._sender_loop(), self.loop)
        
    def wants_opus(self) -> bool:
        return True

    def _strip_dave_supplemental(self, opus_data):
        """Strip DAVE supplemental data from Opus payload.
        
        DAVE appends supplemental data with a 1-byte length suffix.
        The last byte indicates how many supplemental bytes (including itself)
        to remove. We validate the suffix is reasonable before stripping.
        """
        if not opus_data or len(opus_data) < 2:
            return opus_data
            
        supp_len = opus_data[-1]
        # The supplemental section is supp_len bytes + 1 byte for the length itself
        strip_total = supp_len + 1
        
        # Only strip if it's a plausible supplemental data size
        # (typically small, and must leave some actual opus data)
        if 1 <= strip_total < len(opus_data) and strip_total <= 20:
            return opus_data[:-strip_total]
        
        return opus_data

    def write(self, user, data):
        opus_data = data.opus
        if not opus_data:
            return

        if not self.session:
            return

        ssrc = data.packet.ssrc
        
        # Skip FakePackets (packet loss concealment)
        if data.packet.__class__.__name__ == 'FakePacket':
            return

        # Get or create decoder for this SSRC
        if ssrc not in self.decoders:
            try:
                self.decoders[ssrc] = discord.opus.Decoder()
                self._consecutive_errors[ssrc] = 0
            except Exception:
                log.exception(f"Failed to create Opus decoder for SSRC {ssrc}")
                return
                
        decoder = self.decoders[ssrc]
        
        # Try decoding the raw opus data first (works when DAVE is not active)
        pcm = None
        try:
            pcm = decoder.decode(opus_data, fec=False)
            self._consecutive_errors[ssrc] = 0
        except Exception:
            # Raw decode failed — try stripping DAVE supplemental data
            stripped = self._strip_dave_supplemental(opus_data)
            if stripped != opus_data:
                try:
                    pcm = decoder.decode(stripped, fec=False)
                    self._consecutive_errors[ssrc] = 0
                except Exception:
                    pass
            
            if pcm is None:
                self._consecutive_errors[ssrc] = self._consecutive_errors.get(ssrc, 0) + 1
                
                # If we've had many consecutive errors, the decoder state is likely
                # corrupted. Reset it so future valid frames can succeed.
                if self._consecutive_errors[ssrc] >= 10:
                    import time as _time
                    now = _time.monotonic()
                    if not hasattr(self, '_last_reset_log') or (now - self._last_reset_log) > 5.0:
                        self._last_reset_log = now
                        log.warning(f"Resetting Opus decoder for SSRC {ssrc} after {self._consecutive_errors[ssrc]} consecutive errors")
                    try:
                        self.decoders[ssrc] = discord.opus.Decoder()
                        self._consecutive_errors[ssrc] = 0
                    except Exception:
                        pass
                return  # Drop this frame entirely rather than sending silence

        self._send_count += 1

        import time as _time
        now = _time.monotonic()

        if now - self._last_send_log > 5.0:
            self._last_send_log = now
            try:
                rms = audioop.rms(pcm, 2)
            except Exception:
                rms = 0
            log.info(f"Sink.write: {self._send_count} calls in 5s, rms={rms}, user={user}")
            self._send_count = 0

        # Send all successfully decoded audio to Gemini
        self.audio_queue.put(pcm)
            
    async def _sender_loop(self):
        import time
        log.info("Gemini Live audio sender loop started.")
        buffer = bytearray()
        last_data_time = time.monotonic()
        send_count = 0
        last_send_log = time.monotonic()
        
        while True:
            try:
                try:
                    pcm_data = self.audio_queue.get_nowait()
                    buffer.extend(pcm_data)
                    last_data_time = time.monotonic()
                except queue.Empty:
                    # Flush buffer if we have unsent audio and no new audio for 50ms
                    if len(buffer) > 0 and (time.monotonic() - last_data_time) > 0.05:
                        chunk = bytes(buffer)
                        buffer.clear()
                        mono = audioop.tomono(chunk, 2, 0.5, 0.5)
                        resampled, _ = audioop.ratecv(mono, 2, 1, 48000, 16000, None)
                        await self.session.send_realtime_input(
                            media=types.Blob(
                                data=resampled,
                                mime_type="audio/pcm;rate=16000"
                            )
                        )
                        send_count += 1
                    await asyncio.sleep(0.01)
                    continue
                
                # Send 100ms chunks (19200 bytes for 48000Hz stereo PCM)
                while len(buffer) >= 19200:
                    chunk = bytes(buffer[:19200])
                    del buffer[:19200]
                    mono = audioop.tomono(chunk, 2, 0.5, 0.5)
                    resampled, _ = audioop.ratecv(mono, 2, 1, 48000, 16000, None)
                    await self.session.send_realtime_input(
                        media=types.Blob(
                            data=resampled,
                            mime_type="audio/pcm;rate=16000"
                        )
                    )
                    send_count += 1

                # Periodic log of sender activity
                now = time.monotonic()
                if now - last_send_log > 5.0:
                    last_send_log = now
                    log.info(f"Sender loop: {send_count} chunks sent in last 5s, queue_size={self.audio_queue.qsize()}")
                    send_count = 0

            except asyncio.CancelledError:
                log.info("Gemini Live audio sender loop cancelled.")
                break
            except Exception:
                log.exception("Error in Gemini Live audio sender loop:")
                await asyncio.sleep(0.1)

    def cleanup(self):
        self.decoders.clear()
        if self.sender_task:
            self.sender_task.cancel()


class RawAudioBuffer(discord.AudioSource):
    def __init__(self):
        self.buffer = bytearray()
        self.empty_count = 0
        
    def read(self):
        if len(self.buffer) >= 3840:
            self.empty_count = 0
            ret = bytes(self.buffer[:3840])
            del self.buffer[:3840]
            return ret
        else:
            self.empty_count += 1
            if self.empty_count > 75:  # 1.5 seconds of silence
                return b''
            return bytes(3840)
            
    def add_data(self, data):
        self.empty_count = 0
        self.buffer.extend(data)

    def interrupt(self):
        self.buffer.clear()
        self.empty_count = 80


class GeminiVoice(commands.Cog):
    """Voice assistant cog using Gemini Multimodal Live API."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=8372649510)
        self.config.register_guild(voice_channel=None)
        
        self.active_sessions = {}
        self.active_tasks = {}
        self.audio_sources = {}
        
        # Ensure we restore original voice_recv library implementation (no monkeypatches)
        _restore_original_voice_recv()
        
        # Reduce spam from voice_recv reader logger
        logging.getLogger("discord.ext.voice_recv").setLevel(logging.WARNING)

    def cog_unload(self):
        log.info("Unloading GeminiVoice cog: cancelling active tasks...")
        for guild_id, task in list(self.active_tasks.items()):
            task.cancel()

    async def get_gemini_client(self):
        tokens = await self.bot.get_shared_api_tokens("gemini")
        api_key = tokens.get("api_key")
        if not api_key:
            return None
        return genai.Client(api_key=api_key)

    @commands.hybrid_command(name="gemini_set_channel")
    @app_commands.describe(channel="The voice channel to join")
    async def set_channel(self, ctx: commands.Context, channel: discord.VoiceChannel):
        """Set the voice channel for Gemini to join."""
        await self.config.guild(ctx.guild).voice_channel.set(channel.id)
        await ctx.send(f"Voice channel set to {channel.mention}")

    @commands.hybrid_command(name="gemini_join")
    async def join(self, ctx: commands.Context, channel: discord.VoiceChannel = None):
        """Force Gemini to join a voice channel."""
        target_channel = channel
        if not target_channel:
            channel_id = await self.config.guild(ctx.guild).voice_channel()
            if channel_id:
                target_channel = ctx.guild.get_channel(channel_id)
            elif ctx.author.voice:
                target_channel = ctx.author.voice.channel
                
        if not target_channel:
            return await ctx.send("No voice channel provided and none set.")

        client = await self.get_gemini_client()
        if not client:
            return await ctx.send("Gemini API key not set. Use `[p]set api gemini api_key,YOUR_KEY`")

        # Cancel any existing runner task for this guild first
        if ctx.guild.id in self.active_tasks:
            log.info(f"Runner task already exists for guild {ctx.guild.id}. Cancelling it first.")
            self.active_tasks[ctx.guild.id].cancel()
            try:
                await self.active_tasks[ctx.guild.id]
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Error cancelling previous task:")
            if ctx.guild.id in self.active_tasks:
                del self.active_tasks[ctx.guild.id]

        voice_client = ctx.guild.voice_client
        if not voice_client:
            voice_client = await target_channel.connect(cls=voice_recv.VoiceRecvClient)
        elif getattr(voice_client, 'channel', None) != target_channel:
            await voice_client.move_to(target_channel)
            
        system_instruction = types.Content(parts=[types.Part.from_text(
            text="You are a helpful voice assistant in a Discord channel. You are receiving continuous audio from multiple users. ONLY respond when a user addresses you (e.g., 'Hey Gemini', 'Gemini', 'Hey Jiminy', or similar). Be very lenient with how they pronounce your name - if they sound like they are talking to you, respond. Otherwise, ignore the speech and say nothing."
        )])
        config = types.LiveConnectConfig(
            system_instruction=system_instruction,
            response_modalities=["AUDIO"]
        )
        
        connected_event = asyncio.Event()
        error_holder = []
        
        async def gemini_runner():
            loop = asyncio.get_running_loop()
            try:
                log.info("Connecting to Gemini Live API...")
                async with client.aio.live.connect(model="gemini-2.5-flash-native-audio-latest", config=config) as session:
                    log.info("Gemini Live API connected successfully.")
                    self.active_sessions[ctx.guild.id] = session
                    
                    # Stop any existing listener/player to avoid ClientExceptions
                    if voice_client.is_listening():
                        try:
                            voice_client.stop_listening()
                        except Exception:
                            pass
                    if voice_client.is_playing():
                        try:
                            voice_client.stop()
                        except Exception:
                            pass
                    
                    sink = GeminiVoiceSink(session, loop)
                    voice_client.listen(sink)
                    
                    audio_buffer = RawAudioBuffer()
                    self.audio_sources[ctx.guild.id] = audio_buffer
                    # Don't play yet - wait until we have audio data from Gemini
                    
                    connected_event.set()
                    
                    while True:
                        import websockets
                        if getattr(session, '_ws', None) and getattr(session._ws, 'state', None) != websockets.State.OPEN:
                            log.info("Gemini Live API WebSocket connection closed or not open.")
                            break
                        
                        log.info("Starting new session.receive() iteration...")
                        received_any = False
                        try:
                            async for response in session.receive():
                                received_any = True
                                server_content = response.server_content
                                if server_content:
                                    if server_content.interrupted:
                                        log.info("Gemini response was interrupted by user speech.")
                                        audio_buffer.interrupt()
                                    if server_content.model_turn:
                                        for part in server_content.model_turn.parts:
                                            if part.inline_data and part.inline_data.data:
                                                audio_data = part.inline_data.data
                                                log.info(f"Received {len(audio_data)} bytes of audio from Gemini")
                                                # Gemini returns 24000Hz 16-bit PCM.
                                                # Upsample to 48000Hz, and convert mono to stereo.
                                                resampled, _ = audioop.ratecv(audio_data, 2, 1, 24000, 48000, None)
                                                stereo = audioop.tostereo(resampled, 2, 1, 1)
                                                audio_buffer.add_data(stereo)
                                                if not voice_client.is_playing():
                                                    log.info(f"Starting/restarting audio playback (buffer={len(audio_buffer.buffer)} bytes)")
                                                    try:
                                                        voice_client.play(audio_buffer, after=lambda e: log.info(f"Playback finished (error={e})") if e else None)
                                                    except Exception:
                                                        log.exception("Error starting audio playback:")
                                    if server_content.turn_complete:
                                        log.info("Gemini turn_complete received. Will start new receive() loop.")
                                elif response.tool_call:
                                    log.info(f"Received tool_call from Gemini: {response.tool_call}")
                                else:
                                    log.info(f"Received other response type from Gemini: {response}")
                        except Exception as e:
                            log.exception("Error in session.receive() loop:")
                            # If the websocket is closed, break out
                            if 'close' in str(e).lower() or 'connection' in str(e).lower():
                                break
                            await asyncio.sleep(0.5)
                            continue
                                        
                        log.info(f"session.receive() loop ended. received_any={received_any}")
                        if not received_any:
                            await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                log.info("Gemini runner task cancelled.")
            except Exception as e:
                log.exception("Error in Gemini live runner loop:")
                error_holder.append(e)
                connected_event.set()
            finally:
                if ctx.guild.id in self.active_sessions:
                    del self.active_sessions[ctx.guild.id]
                if ctx.guild.id in self.audio_sources:
                    del self.audio_sources[ctx.guild.id]
                if voice_client:
                    try:
                        voice_client.stop_listening()
                    except Exception:
                        pass
                    try:
                        voice_client.stop()
                    except Exception:
                        pass

        task = asyncio.create_task(gemini_runner())
        self.active_tasks[ctx.guild.id] = task
        
        await connected_event.wait()
        if error_holder:
            if ctx.guild.id in self.active_tasks:
                del self.active_tasks[ctx.guild.id]
            return await ctx.send(f"Failed to connect to Gemini Live API: {error_holder[0]}")
            
        await ctx.send(f"Joined {target_channel.mention} and listening!")

    @commands.hybrid_command(name="gemini_leave")
    async def leave(self, ctx: commands.Context):
        """Make Gemini leave the voice channel."""
        if ctx.guild.id in self.active_tasks:
            self.active_tasks[ctx.guild.id].cancel()
            try:
                await self.active_tasks[ctx.guild.id]
            except asyncio.CancelledError:
                pass
            del self.active_tasks[ctx.guild.id]
            
        if ctx.guild.voice_client:
            await ctx.guild.voice_client.disconnect()
            await ctx.send("Left the voice channel.")
        else:
            await ctx.send("Not in a voice channel.")
