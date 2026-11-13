import asyncio
import logging
import os
import time

import nextcord
from aiohttp import ClientSession, ClientTimeout, web
from dotenv import load_dotenv
from nextcord.ext import commands, tasks

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("voicebot")

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("COMMAND_PREFIX", "!")


def parse_ids(*names):
    """อ่าน ID ห้องเสียงจากตัวแปรสภาพแวดล้อม รองรับหลายห้องคั่นด้วยจุลภาค (,)"""
    ids = []
    for name in names:
        for part in os.getenv(name, "").replace(" ", "").split(","):
            if part.isdigit() and int(part) not in ids:
                ids.append(int(part))
    return ids


# ห้องเสียงที่ให้บอทเข้าอัตโนมัติ (ใช้ชื่อเดิม AUTO_VOICE_CHANNEL_ID ได้
# และใส่หลายห้องได้ เช่น 111,222,333 ห้องละ 1 เซิร์ฟเวอร์)
AUTO_CHANNEL_IDS = parse_ids("AUTO_VOICE_CHANNEL_IDS", "AUTO_VOICE_CHANNEL_ID")

# ตรวจสอบสถานะการเชื่อมต่อทุกกี่วินาที
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "20"))

# Render ตั้งค่านี้ให้เองบน Web Service ใช้ยิงตัวเองเพื่อลดโอกาสโดนพักเครื่อง
SELF_URL = os.getenv("RENDER_EXTERNAL_URL")

intents = nextcord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

started_at = time.time()
paused_guilds = set()   # เซิร์ฟเวอร์ที่หยุดเข้าห้องอัตโนมัติ (ใช้ !leave หรือ !join ไปห้องอื่น)
guild_locks = {}        # กันการเชื่อมต่อซ้อนกันในเซิร์ฟเวอร์เดียวกัน
fail_counts = {}        # จำนวนครั้งที่เชื่อมต่อล้มเหลวติดกัน
retry_after = {}        # เวลาที่จะลองใหม่ได้ (backoff)
ghost_strikes = {}      # นับครั้งที่ Discord มองว่าบอทไม่อยู่ในห้อง ทั้งที่บอทคิดว่าอยู่
web_runner = None


def get_lock(guild_id):
    lock = guild_locks.get(guild_id)
    if lock is None:
        lock = guild_locks[guild_id] = asyncio.Lock()
    return lock


def auto_channel_for(guild):
    for cid in AUTO_CHANNEL_IDS:
        channel = guild.get_channel(cid)
        if isinstance(channel, nextcord.VoiceChannel):
            return channel
    return None


async def connect_voice(channel):
    """เข้าห้องเสียง แล้วตั้งสถานะปิดไมค์/ปิดหูฟังของบอท
    (nextcord ไม่รับ self_mute/self_deaf ใน connect() จึงตั้งทีหลัง)"""
    voice = await channel.connect(timeout=30.0, reconnect=True)
    try:
        await channel.guild.change_voice_state(
            channel=channel, self_mute=True, self_deaf=True
        )
    except Exception as e:
        log.warning("ตั้งสถานะปิดไมค์/หูฟังไม่สำเร็จ: %s", e)
    return voice


async def reset_voice(guild):
    """ล้างการเชื่อมต่อเสียงที่ค้าง/เสีย เพื่อเริ่มต่อใหม่ให้สะอาด"""
    vc = guild.voice_client
    if vc is None:
        return
    try:
        await vc.disconnect(force=True)
    except Exception as e:
        log.warning("ล้างการเชื่อมต่อเสียงไม่สำเร็จ: %s", e)


async def join_channel(channel, reset=False):
    """เข้า/ย้ายไปห้องที่ต้องการ จัดการสถานะค้างให้เอง"""
    guild = channel.guild
    async with get_lock(guild.id):
        vc = guild.voice_client

        if vc is not None and reset:
            await reset_voice(guild)
            vc = None

        if vc is not None and vc.is_connected():
            if vc.channel is None or vc.channel.id != channel.id:
                await vc.move_to(channel)
            return

        if vc is not None:
            await reset_voice(guild)

        await connect_voice(channel)


async def ensure_connected(channel):
    """ตรวจว่าบอทอยู่ในห้องที่ตั้งไว้ ถ้าหลุดหรือค้างให้เข้าใหม่
    ถ้าเชื่อมต่อล้มเหลวจะหน่วงเวลาแล้วลองใหม่เรื่อยๆ ไม่ยอมแพ้"""
    guild = channel.guild
    gid = guild.id

    if gid in paused_guilds or time.time() < retry_after.get(gid, 0):
        return

    reset = False
    vc = guild.voice_client

    if (
        vc is not None
        and vc.is_connected()
        and vc.channel is not None
        and vc.channel.id == channel.id
    ):
        me = guild.me
        ghost = me is not None and (me.voice is None or me.voice.channel is None)
        if not ghost:
            ghost_strikes[gid] = 0
            fail_counts[gid] = 0
            return

        # Discord ไม่ถือว่าบอทอยู่ในห้อง ทั้งที่บอทคิดว่าเชื่อมต่ออยู่ (การเชื่อมต่อค้าง)
        # รอให้เกิดติดกัน 3 รอบก่อน กันตรวจผิดพลาด
        ghost_strikes[gid] = ghost_strikes.get(gid, 0) + 1
        if ghost_strikes[gid] < 3:
            return
        log.warning("การเชื่อมต่อเสียงค้างใน %s กำลังเชื่อมต่อใหม่", guild.name)
        ghost_strikes[gid] = 0
        reset = True

    try:
        await join_channel(channel, reset=reset)
        fail_counts[gid] = 0
        log.info("อยู่ในห้องเสียงแล้ว: %s (%s)", channel.name, guild.name)
    except nextcord.Forbidden:
        retry_after[gid] = time.time() + 300
        log.error(
            "บอทไม่มีสิทธิ์ View Channel/Connect ในห้อง %s (%s)",
            channel.name,
            guild.name,
        )
    except Exception as e:
        n = fail_counts.get(gid, 0) + 1
        fail_counts[gid] = n
        wait = min(300, 5 * 2 ** min(n, 6))
        retry_after[gid] = time.time() + wait
        log.warning(
            "เข้าห้องเสียงไม่สำเร็จ (%s ครั้งติดกัน) จะลองใหม่ใน %s วินาที: %s",
            n,
            wait,
            e,
        )
        await reset_voice(guild)


async def check_all():
    for cid in AUTO_CHANNEL_IDS:
        channel = bot.get_channel(cid)
        if isinstance(channel, nextcord.VoiceChannel):
            await ensure_connected(channel)
        else:
            log.warning("ไม่พบห้องเสียง ID %s (ตรวจ ID และสิทธิ์ของบอท)", cid)


@tasks.loop(seconds=CHECK_INTERVAL)
async def keep_alive():
    try:
        await check_all()
    except Exception:
        log.exception("keep_alive ผิดพลาด")


@keep_alive.before_loop
async def before_keep_alive():
    await bot.wait_until_ready()


@tasks.loop(minutes=4)
async def self_ping():
    """ยิง HTTP เข้าหาตัวเอง ช่วยลดโอกาสที่ Render แบบฟรีจะพักเครื่อง
    (เป็นตัวช่วยเสริม ไม่รับประกัน ควรใช้ UptimeRobot คู่กันด้วย)"""
    if not SELF_URL:
        return
    try:
        async with ClientSession(timeout=ClientTimeout(total=15)) as session:
            async with session.get(SELF_URL) as resp:
                await resp.read()
    except Exception as e:
        log.warning("self ping ล้มเหลว: %s", e)


async def start_web_server():
    """เปิดเว็บเซิร์ฟเวอร์เล็กๆ ให้ Render (แบบ Web Service) เห็นว่ามีพอร์ตเปิดอยู่
    ทำงานเฉพาะเมื่อมีตัวแปร PORT (Render ตั้งให้เอง) ถ้ารันบนคอมตัวเองจะข้าม"""
    global web_runner

    port = os.getenv("PORT")
    if not port or web_runner is not None:
        return

    async def handle(_request):
        connected = sum(
            1
            for g in bot.guilds
            if g.voice_client is not None and g.voice_client.is_connected()
        )
        uptime = int(time.time() - started_at)
        return web.Response(
            text=f"Bot is running | voice connections: {connected} | uptime: {uptime}s"
        )

    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/health", handle)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(port))
    await site.start()
    web_runner = runner
    log.info("เว็บเซิร์ฟเวอร์ทำงานที่พอร์ต %s", port)


@bot.event
async def on_ready():
    log.info("บอทออนไลน์แล้ว: %s", bot.user)
    if not AUTO_CHANNEL_IDS:
        log.warning("ยังไม่ได้ตั้ง AUTO_VOICE_CHANNEL_ID ระบบเข้าห้องอัตโนมัติจะไม่ทำงาน")

    await start_web_server()
    await check_all()

    # กันการสตาร์ท loop ซ้ำเมื่อบอท reconnect
    if not keep_alive.is_running():
        keep_alive.start()
    if SELF_URL and not self_ping.is_running():
        self_ping.start()


async def delayed_check(guild):
    try:
        await asyncio.sleep(3)
        channel = auto_channel_for(guild)
        if channel is not None:
            await ensure_connected(channel)
    except Exception:
        log.exception("delayed_check ผิดพลาด")


@bot.event
async def on_voice_state_update(member, before, after):
    """ถ้าบอทถูกเตะ ถูกย้าย หรือหลุดจากห้อง ให้ตรวจและกลับเข้าห้องทันที"""
    if bot.user is None or member.id != bot.user.id:
        return

    before_id = before.channel.id if before.channel else None
    after_id = after.channel.id if after.channel else None

    if after_id is None or after_id != before_id:
        bot.loop.create_task(delayed_check(member.guild))


@bot.command()
@commands.guild_only()
async def join(ctx):
    """ให้บอทเข้าห้องเสียงที่คุณอยู่"""
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.send("กรุณาเข้าห้องเสียงก่อน")

    channel = ctx.author.voice.channel

    try:
        await join_channel(channel)
    except nextcord.Forbidden:
        return await ctx.send("บอทไม่มีสิทธิ์เข้าห้องนี้")
    except Exception as e:
        return await ctx.send(f"เข้าห้องไม่สำเร็จ: {e}")

    # ถ้าไปห้องอื่นที่ไม่ใช่ห้องอัตโนมัติ ให้หยุดดึงกลับของเซิร์ฟเวอร์นี้
    auto_channel = auto_channel_for(ctx.guild)
    if auto_channel is not None and channel.id == auto_channel.id:
        paused_guilds.discard(ctx.guild.id)
    else:
        paused_guilds.add(ctx.guild.id)

    await ctx.send("เข้าห้องเสียงเรียบร้อยแล้ว")


@bot.command()
@commands.guild_only()
async def leave(ctx):
    """ให้บอทออกจากห้องเสียง (และหยุดเข้าห้องอัตโนมัติในเซิร์ฟเวอร์นี้)"""
    voice = ctx.guild.voice_client
    if voice is None:
        return await ctx.send("บอทยังไม่ได้อยู่ในห้องเสียง")

    paused_guilds.add(ctx.guild.id)
    await voice.disconnect()
    await ctx.send(
        f"ออกจากห้องเสียงแล้ว (พิมพ์ {PREFIX}autojoin เพื่อเปิดระบบเข้าห้องอัตโนมัติอีกครั้ง)"
    )


@bot.command()
@commands.guild_only()
async def autojoin(ctx):
    """เปิดระบบเข้าห้องอัตโนมัติอีกครั้งในเซิร์ฟเวอร์นี้"""
    channel = auto_channel_for(ctx.guild)
    if channel is None:
        return await ctx.send("เซิร์ฟเวอร์นี้ยังไม่ได้ตั้งห้องเสียงอัตโนมัติ (AUTO_VOICE_CHANNEL_ID)")

    paused_guilds.discard(ctx.guild.id)
    retry_after.pop(ctx.guild.id, None)
    await ensure_connected(channel)
    await ctx.send("เปิดระบบเข้าห้องอัตโนมัติแล้ว")


@bot.command()
@commands.guild_only()
async def status(ctx):
    """ดูสถานะของบอท"""
    vc = ctx.guild.voice_client
    if vc is not None and vc.is_connected() and vc.channel is not None:
        where = vc.channel.name
    else:
        where = "ไม่ได้อยู่ในห้องเสียง"

    if auto_channel_for(ctx.guild) is None:
        auto = "ไม่ได้ตั้งค่า"
    elif ctx.guild.id in paused_guilds:
        auto = "ปิดอยู่"
    else:
        auto = "เปิดอยู่"

    hours, rem = divmod(int(time.time() - started_at), 3600)
    minutes = rem // 60
    await ctx.send(
        f"ห้องเสียง: {where}\n"
        f"เข้าห้องอัตโนมัติ: {auto}\n"
        f"ออนไลน์ต่อเนื่อง: {hours} ชม. {minutes} นาที\n"
        f"ปิง: {round(bot.latency * 1000)} ms"
    )


if not TOKEN:
    raise RuntimeError("ไม่พบ DISCORD_TOKEN ในไฟล์ .env")

bot.run(TOKEN)
