import os
import re
import time
import json
import asyncio
import aiohttp
import discord
from discord.ext import commands

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
RAILWAY_BACKEND   = os.environ.get("RAILWAY_BACKEND", "https://web-production-654e0.up.railway.app")
GUILD_ID          = 1427249989066690684
CHANNEL_NAME      = "bss-trade"
ODYSSEY_BOT_NAME  = "OdysseyAI"

POLL_INTERVAL     = 300
ODYSSEY_TIMEOUT   = 20
COMMAND_DELAY     = 3.5

intents = discord.Intents.default()
intents.message_content = True
intents.guilds           = True
intents.messages         = True

bot = commands.Bot(command_prefix="!", intents=intents)

pending = {}


def parse_gentext(gen_text: str):
    if not gen_text:
        return 0.0, "M/s"
    gen_text = gen_text.replace(",", "").strip()
    match = re.match(r'\$?([\d.]+)\s*([KMBT]?)/s', gen_text, re.IGNORECASE)
    if not match:
        return 0.0, "M/s"
    num  = float(match.group(1))
    unit = (match.group(2) or "M").upper() + "/s"
    return num, unit


def parse_price_from_embed(embed: discord.Embed) -> str | None:
    full_text = ""
    if embed.description:
        full_text += embed.description + "\n"
    for field in embed.fields:
        full_text += field.name + "\n" + field.value + "\n"

    patterns = [
        r'[Cc]urrent\s+competitor[^\$\d]*\$?([\d,.]+)',
        r'[Cc]urrent[^\$\d]*\$?([\d,.]+)',
        r'\$?([\d,.]+)',
    ]
    for pat in patterns:
        m = re.search(pat, full_text)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if 0.01 <= val <= 100000:
                    return f"${val:.2f}"
            except ValueError:
                continue
    return None


async def submit_price(session: aiohttp.ClientSession, name: str, price_text: str, url: str = ""):
    try:
        payload = {
            "prices": [{
                "name": name,
                "price": float(price_text.replace("$", "").replace(",", "")),
                "price_text": price_text,
                "url": url,
            }]
        }
        async with session.post(f"{RAILWAY_BACKEND}/submit-price", json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                print(f"[SUBMIT] {name} → {price_text}")
            else:
                print(f"[SUBMIT] Failed for {name}: HTTP {resp.status}")
    except Exception as e:
        print(f"[SUBMIT] Error for {name}: {e}")


async def fetch_scan_pets(session: aiohttp.ClientSession) -> list:
    try:
        async with session.get(f"{RAILWAY_BACKEND}/results", timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("pets", [])
    except Exception as e:
        print(f"[FETCH] Error fetching pets: {e}")
    return []


async def run_odyfind_for_pets(channel: discord.TextChannel, pets: list):
    seen_names = set()
    unique_pets = []
    for pet in pets:
        name = pet.get("name", "").strip()
        if name and name not in seen_names:
            seen_names.add(name)
            unique_pets.append(pet)

    top = unique_pets[:15]
    print(f"[ODYFIND] Running for {len(top)} pets")

    async with aiohttp.ClientSession() as session:
        for pet in top:
            name     = pet.get("name", "").strip()
            gen_text = pet.get("genText", "1M/s")
            if not name:
                continue

            income, unit = parse_gentext(gen_text)
            if income == 0.0:
                income = 1.0
                unit   = "M/s"

            unit_clean = unit.replace("/s", "") + "/s"

            pending[name] = asyncio.get_event_loop().create_future()

            try:
                await channel.send(f"/odyfind brainrot:{name} income:{income} unit:{unit_clean}")
                print(f"[ODYFIND] Sent for: {name} ({income} {unit_clean})")

            except Exception as e:
                print(f"[ODYFIND] Failed to send for {name}: {e}")
                pending.pop(name, None)
                continue

            try:
                price_text = await asyncio.wait_for(pending[name], timeout=ODYSSEY_TIMEOUT)
                if price_text:
                    await submit_price(session, name, price_text)
            except asyncio.TimeoutError:
                print(f"[ODYFIND] Timeout waiting for OdysseyAI response for: {name}")
            except Exception as e:
                print(f"[ODYFIND] Error waiting for {name}: {e}")
            finally:
                pending.pop(name, None)

            await asyncio.sleep(COMMAND_DELAY)


@bot.event
async def on_ready():
    print(f"[BOT] Logged in as {bot.user} (ID: {bot.user.id})")
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        print(f"[BOT] Guild {GUILD_ID} not found")
        return
    print(f"[BOT] Connected to guild: {guild.name}")
    bot.loop.create_task(price_refresh_loop())


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None or message.guild.id != GUILD_ID:
        return
    if message.channel.name != CHANNEL_NAME:
        return

    if message.author.name != ODYSSEY_BOT_NAME and not (
        hasattr(message.author, 'display_name') and ODYSSEY_BOT_NAME in message.author.display_name
    ):
        await bot.process_commands(message)
        return

    if not message.embeds:
        return

    embed = message.embeds[0]
    embed_title = embed.title or ""

    matched_name = None
    for name in list(pending.keys()):
        name_lower  = name.lower().replace(" ", "")
        title_lower = embed_title.lower().replace(" ", "")
        if name_lower in title_lower or title_lower in name_lower:
            matched_name = name
            break

    if not matched_name:
        for name in list(pending.keys()):
            words = name.lower().split()
            if all(w in embed_title.lower() for w in words):
                matched_name = name
                break

    if not matched_name:
        print(f"[BOT] OdysseyAI responded but couldn't match: '{embed_title}' to pending {list(pending.keys())}")
        return

    price_text = parse_price_from_embed(embed)
    print(f"[BOT] OdysseyAI → {matched_name}: {price_text}")

    fut = pending.get(matched_name)
    if fut and not fut.done():
        fut.set_result(price_text)


async def price_refresh_loop():
    await asyncio.sleep(10)
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        print("[LOOP] Guild not found, stopping loop")
        return

    channel = discord.utils.get(guild.text_channels, name=CHANNEL_NAME)
    if not channel:
        print(f"[LOOP] Channel #{CHANNEL_NAME} not found")
        return

    print(f"[LOOP] Price refresh loop started in #{CHANNEL_NAME}")

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                pets = await fetch_scan_pets(session)

            if pets:
                print(f"[LOOP] Fetched {len(pets)} pets from backend")
                await run_odyfind_for_pets(channel, pets)
            else:
                print("[LOOP] No pets in backend yet, waiting...")

        except Exception as e:
            print(f"[LOOP] Error in refresh loop: {e}")

        print(f"[LOOP] Sleeping {POLL_INTERVAL}s until next refresh")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("[ERROR] DISCORD_BOT_TOKEN not set")
        exit(1)
    bot.run(DISCORD_BOT_TOKEN)
