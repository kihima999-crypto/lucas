import os

import nextcord
from nextcord.ext import commands, tasks
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("COMMAND_PREFIX", "!")

# ID ห้องเสียงสำหรับระบบเข้าห้องอัตโนมัติ (ตั้งในไฟล์ .env)
AUTO_VOICE_CHANNEL_ID = int(os.getenv("AUTO_VOICE_CHANNEL_ID", "0"))

intents = nextcord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# เปิด/ปิดระบบเข้าห้องอัตโนมัติ
# ถูกปิดเมื่อใช้ !leave หรือ !join ไปห้องอื่น และเปิดใหม่ด้วย !autojoin
auto_rejoin_enabled = True


async def connect_voice(channel):
    """เข้าห้องเสียง แล้วตั้งสถานะปิดไมค์/ปิดหูฟังของบอท
    (nextcord บางเวอร์ชันไม่รับ self_mute/self_deaf ใน connect() จึงแยกตั้งทีหลัง)"""
    voice = await channel.connect()
    try:
        await channel.guild.change_voice_state(
            channel=channel, self_mute=True, self_deaf=True
        )
    except Exception as e:
        print(f"ตั้งสถานะปิดไมค์/หูฟังไม่สำเร็จ: {e}")
    return voice


async def ensure_connected():
    """เข้าห้องอัตโนมัติ ถ้ายังไม่อยู่ หรืออยู่ผิดห้อง"""
    if not auto_rejoin_enabled or not AUTO_VOICE_CHANNEL_ID:
        return

    channel = bot.get_channel(AUTO_VOICE_CHANNEL_ID)
    if not isinstance(channel, nextcord.VoiceChannel):
        return

    voice = channel.guild.voice_client

    try:
        if voice is None:
            await connect_voice(channel)
            print(f"เข้าห้องเสียงอัตโนมัติแล้ว: {channel.name}")
        elif not voice.is_connected():
            await voice.disconnect(force=True)
            await connect_voice(channel)
            print(f"เชื่อมต่อห้องเสียงใหม่แล้ว: {channel.name}")
        elif voice.channel.id != channel.id:
            await voice.move_to(channel)
            print(f"ย้ายกลับห้อง: {channel.name}")
    except nextcord.Forbidden:
        print("บอทไม่มีสิทธิ์ Connect/View Channel ในห้องเสียงนี้")
    except Exception as e:
        print(f"เข้าห้องเสียงไม่สำเร็จ: {e}")


web_runner = None


async def start_web_server():
    """เปิดเว็บเซิร์ฟเวอร์เล็กๆ ให้ Render (แบบ Web Service) เห็นว่ามีพอร์ตเปิดอยู่
    ทำงานเฉพาะเมื่อมีตัวแปร PORT (Render ตั้งให้เอง) ถ้ารันบนคอมตัวเองจะข้าม"""
    global web_runner

    port = os.getenv("PORT")
    if not port or web_runner is not None:
        return

    async def handle(_request):
        return web.Response(text="Bot is running")

    app = web.Application()
    app.router.add_get("/", handle)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(port))
    await site.start()
    web_runner = runner
    print(f"เว็บเซิร์ฟเวอร์ทำงานที่พอร์ต {port}")


@tasks.loop(minutes=1)
async def keep_alive():
    await ensure_connected()


@bot.event
async def on_ready():
    print(f"บอทออนไลน์แล้ว: {bot.user}")
    await start_web_server()
    await ensure_connected()

    # กันการสตาร์ท loop ซ้ำเมื่อบอท reconnect
    if not keep_alive.is_running():
        keep_alive.start()


@bot.event
async def on_voice_state_update(member, before, after):
    """ถ้าบอทถูกเตะ/หลุดจากห้อง ให้กลับเข้าห้องอัตโนมัติ"""
    if bot.user and member.id == bot.user.id and after.channel is None:
        await ensure_connected()


@bot.command()
@commands.guild_only()
async def join(ctx):
    """ให้บอทเข้าห้องเสียงที่คุณอยู่"""
    global auto_rejoin_enabled

    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.send("กรุณาเข้าห้องเสียงก่อน")

    channel = ctx.author.voice.channel
    voice = ctx.guild.voice_client

    try:
        if voice is None:
            await connect_voice(channel)
        else:
            await voice.move_to(channel)
    except nextcord.Forbidden:
        return await ctx.send("บอทไม่มีสิทธิ์เข้าห้องนี้")
    except Exception as e:
        return await ctx.send(f"เข้าห้องไม่สำเร็จ: {e}")

    # ถ้าไปห้องอื่นที่ไม่ใช่ห้องอัตโนมัติ ให้หยุดดึงกลับ
    auto_rejoin_enabled = channel.id == AUTO_VOICE_CHANNEL_ID
    await ctx.send("เข้าห้องเสียงเรียบร้อยแล้ว")


@bot.command()
@commands.guild_only()
async def leave(ctx):
    """ให้บอทออกจากห้องเสียง (และหยุดเข้าห้องอัตโนมัติ)"""
    global auto_rejoin_enabled

    voice = ctx.guild.voice_client
    if voice is None:
        return await ctx.send("บอทยังไม่ได้อยู่ในห้องเสียง")

    auto_rejoin_enabled = False
    await voice.disconnect()
    await ctx.send(f"ออกจากห้องเสียงแล้ว (พิมพ์ {PREFIX}autojoin เพื่อเปิดระบบเข้าห้องอัตโนมัติอีกครั้ง)")


@bot.command()
@commands.guild_only()
async def autojoin(ctx):
    """เปิดระบบเข้าห้องอัตโนมัติอีกครั้ง"""
    global auto_rejoin_enabled

    if not AUTO_VOICE_CHANNEL_ID:
        return await ctx.send("ยังไม่ได้ตั้ง AUTO_VOICE_CHANNEL_ID ในไฟล์ .env")

    auto_rejoin_enabled = True
    await ensure_connected()
    await ctx.send("เปิดระบบเข้าห้องอัตโนมัติแล้ว")


if not TOKEN:
    raise RuntimeError("ไม่พบ DISCORD_TOKEN ในไฟล์ .env")

bot.run(TOKEN)
