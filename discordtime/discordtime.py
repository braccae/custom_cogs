import datetime
import logging
import re
from zoneinfo import ZoneInfo, available_timezones
import discord
from discord import app_commands
from redbot.core import commands, Config
import dateutil.parser

log = logging.getLogger("red.discordtime")

class DiscordTime(commands.Cog):
    """Interpret local times in your timezone and show them as Discord timestamps."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=9283748291)
        self.config.register_user(timezone=None)
        
        # Build set of timezones for fast validation and matching
        self._all_timezones = sorted(available_timezones())
        self._lower_tz_map = {tz.lower(): tz for tz in self._all_timezones}

    @commands.hybrid_command(name="settz")
    @app_commands.describe(timezone="Your local timezone (e.g., America/New_York)")
    async def settz(self, ctx: commands.Context, timezone: str):
        """Set your local timezone so /time interprets inputs correctly."""
        # Sanitize input timezone
        tz_clean = timezone.strip().replace(" ", "_")
        tz_lower = tz_clean.lower()
        matched_tz = self._lower_tz_map.get(tz_lower)
        
        if not matched_tz:
            # Try to find a partial match
            matches = [tz for tz in self._all_timezones if tz_lower in tz.lower()]
            if len(matches) == 1:
                matched_tz = matches[0]
            else:
                suggestions = ", ".join(f"`{m}`" for m in matches[:5])
                err_msg = f"Invalid timezone `{timezone}`. Please use a standard IANA timezone name."
                if suggestions:
                    err_msg += f"\nDid you mean one of these: {suggestions}?"
                await ctx.send(err_msg, ephemeral=True)
                return

        await self.config.user(ctx.author).timezone.set(matched_tz)
        await ctx.send(f"Your timezone has been set to `{matched_tz}`.", ephemeral=True)

    @settz.autocomplete("timezone")
    async def settz_autocomplete(self, interaction: discord.Interaction, current: str):
        """Autocomplete timezone names based on search query."""
        search = current.lower().strip().replace(" ", "_")
        choices = []
        for tz in self._all_timezones:
            if search in tz.lower():
                choices.append(app_commands.Choice(name=tz.replace("_", " "), value=tz))
                if len(choices) >= 25:
                    break
        return choices

    @commands.hybrid_command(name="time")
    @app_commands.describe(
        time_input="The time to convert (e.g. 3:00 PM, tomorrow 15:30, Saturday 9am)",
        debug="Show detailed interpretation and timezone info if True"
    )
    async def time(self, ctx: commands.Context, time_input: str, debug: bool = False):
        """Convert a local time in your timezone into copyable Discord timestamps."""
        # 1. Retrieve timezone
        tz_name = await self.config.user(ctx.author).timezone()
        tz_suffix_msg = ""
        if not tz_name:
            tz_name = "UTC"
            tz_suffix_msg = "\n*Note: You haven't set a timezone using `/settz` yet. Defaulting to UTC.*"

        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
            tz_suffix_msg = f"\n*Note: Your stored timezone `{tz_name}` is invalid. Defaulting to UTC. Use `/settz` to set a valid one.*"

        # 2. Preprocess time input for relative day offset words
        clean_input = time_input.lower().strip()
        day_offset = 0
        
        # Word boundaries matching for today/tomorrow/yesterday
        if re.search(r'\btomorrow\b', clean_input):
            day_offset = 1
            clean_input = re.sub(r'\btomorrow\b', '', clean_input).strip()
        elif re.search(r'\byesterday\b', clean_input):
            day_offset = -1
            clean_input = re.sub(r'\byesterday\b', '', clean_input).strip()
        elif re.search(r'\btoday\b', clean_input):
            clean_input = re.sub(r'\btoday\b', '', clean_input).strip()

        # If string is empty after removing today/tomorrow/yesterday, default to noon (12:00)
        if not clean_input:
            clean_input = "12:00"

        # 3. Parse date
        try:
            # Clean current local date in the user's timezone (time parts zeroed out)
            now_tz = datetime.datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
            
            parsed_dt = dateutil.parser.parse(clean_input, default=now_tz)
            if day_offset != 0:
                parsed_dt += datetime.timedelta(days=day_offset)
                
        except Exception:
            await ctx.send(
                f"Could not parse `{time_input}`. Please use a standard time format like:\n"
                f"- `15:30` or `3:30 PM`\n"
                f"- `tomorrow 5:00 PM`\n"
                f"- `Saturday 9am`\n"
                f"- `May 30 15:00`",
                ephemeral=True
            )
            return

        # 4. Determine matching style
        # Check if the original input contains references to relative dates, weekdays, months, or year/date patterns
        low_input = time_input.lower().strip()
        
        has_relative_day = any(word in low_input for word in ["tomorrow", "yesterday", "today"])
        
        weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                    "mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        has_weekday = any(re.search(rf"\b{day}\b", low_input) for day in weekdays)
        
        months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
                  "january", "february", "march", "april", "june", "july", "august", "september", "october", "november", "december"]
        has_month = any(re.search(rf"\b{month}\b", low_input) for month in months)
        
        has_date_pattern = bool(re.search(r'\b\d{1,4}[-/]\d{1,2}([-/]\d{1,4})?\b', low_input))
        
        has_date = has_relative_day or has_weekday or has_month or has_date_pattern
        has_time_pattern = bool(re.search(r'\b\d{1,2}:\d{2}\b', low_input)) or "am" in low_input or "pm" in low_input or re.search(r'\b\d{1,2}\s*(am|pm)\b', low_input)

        if has_date and has_time_pattern:
            style, style_name = "F", "Long Date/Time"
        elif has_date:
            style, style_name = "D", "Long Date"
        else:
            style, style_name = "t", "Short Time"

        # 5. Generate Discord markdown timestamp
        timestamp = int(parsed_dt.timestamp())
        code = f"<t:{timestamp}:{style}>"
        
        if debug:
            response_lines = [
                f"**Input:** `{time_input}` (interpreted as `{parsed_dt.strftime('%Y-%m-%d %H:%M:%S')} {tz_name}`)",
                f"**Matched Style ({style_name}):** `{code}` ➔ {code}"
            ]
            if tz_suffix_msg:
                response_lines.append(tz_suffix_msg)
            response_lines.append("\n*You can copy-paste the code box above to show that formatted time to others.*")
            await ctx.send("\n".join(response_lines))
        else:
            await ctx.send(code)


