# Discord Voice Bot (nextcord)

บอทเข้าห้องเสียงอัตโนมัติ พร้อมคำสั่งควบคุมเอง

## คำสั่ง

| คำสั่ง | หน้าที่ |
|---|---|
| `!join` | ให้บอทเข้าห้องเสียงที่คุณอยู่ |
| `!leave` | ให้บอทออกจากห้อง และหยุดระบบเข้าห้องอัตโนมัติ |
| `!autojoin` | เปิดระบบเข้าห้องอัตโนมัติอีกครั้ง |

## ติดตั้ง

1. ติดตั้ง Python 3.9 ขึ้นไป
2. ติดตั้งไลบรารี
   ```
   pip install -r requirements.txt
   ```
3. คัดลอก `.env.example` เป็น `.env` แล้วกรอกค่า
   - `DISCORD_TOKEN` โทเคนบอทจาก Developer Portal
   - `AUTO_VOICE_CHANNEL_ID` ID ห้องเสียงที่ให้บอทเข้าอัตโนมัติ
4. รันบอท
   ```
   python bot.py
   ```

## ตั้งค่าบอทใน Discord Developer Portal

1. ไปที่ https://discord.com/developers/applications แล้วกด New Application
2. แท็บ **Bot** กด Reset Token เพื่อคัดลอกโทเคน
3. เลื่อนลงที่ Privileged Gateway Intents แล้วเปิด **Message Content Intent**
4. แท็บ **OAuth2 > URL Generator** เลือก scope `bot` และสิทธิ์
   `View Channels`, `Send Messages`, `Connect`
5. เปิดลิงก์ที่ได้เพื่อเชิญบอทเข้าเซิร์ฟเวอร์

## หา ID ห้องเสียง

เปิด Developer Mode ใน Discord (ตั้งค่า > ขั้นสูง) แล้วคลิกขวาที่ห้องเสียง เลือก Copy Channel ID

## หมายเหตุ

- บอทตรวจสอบการเชื่อมต่อทุก 1 นาที และกลับเข้าห้องเองเมื่อหลุดหรือถูกเตะ
- ต้องปล่อยให้โปรแกรมรันตลอดเวลาจึงจะอยู่ในห้องได้ (เช่น รันบน VPS หรือ Raspberry Pi)
