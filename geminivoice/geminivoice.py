import asyncio
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

# Store reference to the true original _decode_packet (before any monkeypatching)
_ORIGINAL_DECODE_PACKET = None

def _apply_dave_monkeypatch():
    """Apply DAVE E2EE monkeypatch to voice_recv. Safe to call multiple times."""
    global _ORIGINAL_DECODE_PACKET
    try:
        import davey
        import discord.ext.voice_recv.opus as voice_recv_opus
        from discord.ext.voice_recv.rtp import FakePacket
        
        def patched_decode_packet(self, packet):
            """Direct replacement for PacketDecoder._decode_packet.
            Handles DAVE supplemental byte stripping inline instead of wrapping."""
            assert self._decoder is not None
            
            if not hasattr(self, '_dave_conn'):
                self._dave_conn = None
            if not hasattr(self, '_dave_uid'):
                self._dave_uid = None
            if not hasattr(self, '_diag_count'):
                self._diag_count = 0

            # Real packet path
            if packet:
                # Try to cache DAVE connection on first real packet
                if self._dave_conn is None:
                    if hasattr(self, 'sink') and self.sink:
                        vc = getattr(self.sink, 'voice_client', None)
                        if vc:
                            conn = getattr(vc, '_connection', None)
                            if conn:
                                self._dave_conn = conn
                                uid = vc._get_id_from_ssrc(self.ssrc)
                                if uid:
                                    self._dave_uid = uid
                                    log.info(f"Cached DAVE conn for ssrc={self.ssrc}, uid={uid}")

                # Try DAVE decrypt first, or fallback to stripping
                data = packet.decrypted_data
                dave_result = "skip"
                if data and self._dave_conn and self._dave_uid and hasattr(self._dave_conn, 'dave_session') and self._dave_conn.dave_session and self._dave_conn.dave_session.ready:
                    try:
                        decrypted = self._dave_conn.dave_session.decrypt(self._dave_uid, davey.MediaType.audio, bytes(data))
                        data = decrypted
                        dave_result = "decrypted"
                    except Exception as e:
                        err_str = str(e)
                        if 'Unencrypted' in err_str or 'Passthrough' in err_str:
                            # It's already pure Opus audio without DAVE framing!
                            # Do not strip any bytes, or else we corrupt the Opus frame.
                            dave_result = f"unencrypted_err:{err_str[:20]}"
                        else:
                            dave_result = f"err:{err_str[:40]}"
                elif data and len(data) > 1:
                    # Fallback stripping if DAVE is completely unavailable
                    supp_len = data[-1]
                    strip_total = supp_len + 1
                    if 0 < strip_total < len(data):
                        data = data[:-strip_total]
                        dave_result = f"fallback_stripped_{strip_total}"
                
                # Diagnostic logging
                self._diag_count += 1
                if self._diag_count <= 5 or self._diag_count % 500 == 0:
                    orig_len = len(packet.decrypted_data) if packet.decrypted_data else 0
                    new_len = len(data) if data else 0
                    log.info(f"DIAG pkt#{self._diag_count} seq={packet.sequence} orig={orig_len} stripped={new_len} dave={dave_result}")

                try:
                    pcm = self._decoder.decode(data, fec=False)
                    return packet, pcm
                except Exception as e:
                    import time as _time
                    now = _time.monotonic()
                    if not hasattr(self, '_last_opus_err') or (now - self._last_opus_err) > 10.0:
                        self._last_opus_err = now
                        log.warning("Opus decode failed (pkt#%d, dave=%s, orig=%d, stripped=%d): %s",
                                    self._diag_count, dave_result,
                                    len(packet.decrypted_data) if packet.decrypted_data else 0,
                                    len(data) if data else 0, e)
                    # Return silence frame
                    return packet, b'\x00' * 3840
            
            # Fake packet path - use FEC from next packet
            self._diag_count += 1
            next_packet = self._buffer.peek_next()
            if next_packet is not None:
                nextdata = next_packet.decrypted_data
                # Also strip DAVE bytes from FEC source
                if nextdata and len(nextdata) > 1:
                    supp_len = nextdata[-1]
                    strip_total = supp_len + 1
                    if 0 < strip_total < len(nextdata):
                        nextdata = nextdata[:-strip_total]
                try:
                    pcm = self._decoder.decode(nextdata, fec=True)
                except Exception:
                    pcm = self._decoder.decode(None, fec=False)
            else:
                pcm = self._decoder.decode(None, fec=False)
            
            return packet, pcm
        
        patched_decode_packet._is_dave_patch = True
        voice_recv_opus.PacketDecoder._decode_packet = patched_decode_packet
        log.info("Applied DAVE stripping monkeypatch to discord.ext.voice_recv")
    except Exception as e:
        log.exception("Failed to apply DAVE monkeypatch:")


class GeminiVoiceSink(voice_recv.AudioSink):
    def __init__(self, session, loop):
        super().__init__()
        self.session = session
        self.loop = loop
        self._send_count = 0
        self._last_send_log = 0
        self._last_speech_time = 0
        self._is_speaking = False
        
    def wants_opus(self) -> bool:
        return False

    def write(self, user, data):
        if self.session and data.pcm:
            self._send_count += 1
            # Use RMS to detect actual audio energy
            try:
                rms = audioop.rms(data.pcm, 2)
            except Exception:
                rms = 0
            
            import time as _time
            now = _time.monotonic()
            
            # Track speech state for sending trailing silence
            if rms > 30:  # Speech threshold (low to catch quiet speech)
                self._last_speech_time = now
                self._is_speaking = True
            
            if now - self._last_send_log > 5.0:
                self._last_send_log = now
                log.info(f"Sink.write: {self._send_count} calls in 5s, rms={rms}, speaking={self._is_speaking}, user={user}")
                self._send_count = 0
            
            # Only send to Gemini if there's speech or we're within 1 second of last speech
            # (trailing silence helps Gemini detect end of utterance)
            if self._is_speaking:
                if rms <= 30 and (now - self._last_speech_time) > 1.0:
                    self._is_speaking = False
                    log.info("Speech ended, stopping audio send to Gemini")
                else:
                    asyncio.run_coroutine_threadsafe(
                        self._send_audio(data.pcm),
                        self.loop
                    )
            
    async def _send_audio(self, pcm_data):
        try:
            # Convert stereo to mono (using 0.5, 0.5 to avoid clipping)
            mono = audioop.tomono(pcm_data, 2, 0.5, 0.5)
            # Downsample 48000 to 16000
            resampled, _ = audioop.ratecv(mono, 2, 1, 48000, 16000, None)
            
            await self.session.send_realtime_input(
                media=types.Blob(
                    data=resampled,
                    mime_type="audio/pcm;rate=16000"
                )
            )
        except Exception as e:
            log.exception("Error sending audio to Gemini Live API:")

    def cleanup(self):
        pass


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


class GeminiVoice(commands.Cog):
    """Voice assistant cog using Gemini Multimodal Live API."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=8372649510)
        self.config.register_guild(voice_channel=None)
        
        self.active_sessions = {}
        self.active_tasks = {}
        self.audio_sources = {}
        
        # Apply monkeypatch on every cog load/reload
        _apply_dave_monkeypatch()

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
