import os
import discord
from .geminivoice import GeminiVoice

async def setup(bot):
    if not discord.opus.is_loaded():
        cog_dir = os.path.dirname(__file__)
        opus_path = os.path.join(cog_dir, "libopus.so")
        try:
            discord.opus.load_opus(opus_path)
        except Exception as e:
            print(f"Failed to load opus from {opus_path}: {e}")
            
    await bot.add_cog(GeminiVoice(bot))
