from .discordtime import DiscordTime

async def setup(bot):
    await bot.add_cog(DiscordTime(bot))
