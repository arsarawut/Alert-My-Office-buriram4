# Check My Office News

สคริปต์นี้ login เข้า My Office แล้วเช็คข่าวในหน้า:

`http://209.15.117.206/myoffice/2569/index.php?name=tkk4&category=132`

และเทียบกับแฟ้มข่าวที่จัดเก็บแล้ว:

`http://209.15.117.206/myoffice/2569/index.php?name=tkk4&file=rub&category=132&Page=1`

ถ้าพบข่าวที่ยังไม่ได้จัดเก็บและยังไม่เคยแจ้งเตือน จะส่งเข้า Telegram แล้วบันทึกสถานะไว้ที่ `data/seen_news.json` เพื่อไม่ส่งข่าวซ้ำ

## ตั้งค่า

```bash
cp .env.example .env
```

จากนั้นแก้ `.env` แล้วใส่ค่า:

```env
TELEGRAM_BOT_TOKEN=ใส่-token-bot
TELEGRAM_CHAT_ID=ใส่-chat-id
```

ค่า username/password ของ My Office ใส่ไว้ใน `.env.example` ตามที่ให้มาแล้ว ถ้ารหัสเปลี่ยนให้แก้ใน `.env`

ค่า `MYOFFICE_ARCHIVED_MAX_PAGES=0` หมายถึงให้ดึงทุกหน้าของแฟ้มรับที่ระบบแสดง ถ้าต้องการลดเวลาเช็คสามารถจำกัดจำนวนหน้าได้ เช่น `MYOFFICE_ARCHIVED_MAX_PAGES=3`

ค่า `RUN_WEBHOOK_TOKEN` ใช้ป้องกัน endpoint สำหรับการรันบน cloud

## ทดสอบรัน

```bash
python3 check_myoffice_news.py --dry-run
```

รันจริง:

```bash
python3 check_myoffice_news.py
```

ทดสอบส่ง Telegram อย่างเดียว:

```bash
python3 check_myoffice_news.py --test-telegram
```

ครั้งแรกสคริปต์จะบันทึกรายการปัจจุบันเป็น baseline ทั้งข่าวที่ยังไม่ได้จัดเก็บและข่าวในแฟ้มรับ แล้วจะยังไม่ส่ง Telegram เพื่อกันการแจ้งเตือนข่าวเก่าทั้งหมด ถ้าต้องการให้ครั้งแรกส่งรายการที่ยังไม่ได้จัดเก็บทั้งหมด ให้ใช้:

```bash
python3 check_myoffice_news.py --notify-existing
```

## ตั้งให้เช็คทุกวันด้วย cron

เปิด crontab:

```bash
crontab -e
```

เพิ่มบรรทัดนี้เพื่อรันทุกวันเวลา 08:00:

```cron
0 8 * * * /Users/arsarawut/Downloads/Project/Check_My-office/run_daily.sh
```

log จะอยู่ที่:

`/Users/arsarawut/Downloads/Project/Check_My-office/check_myoffice_news.log`

## วิธีหา Telegram chat id แบบเร็ว

1. สร้าง bot ผ่าน Telegram `@BotFather`
2. ส่งข้อความหา bot อย่างน้อย 1 ครั้ง
3. เปิด URL นี้ โดยแทน `<TOKEN>` ด้วย token ของ bot:

```text
https://api.telegram.org/bot<TOKEN>/getUpdates
```

ดูค่า `chat.id` แล้วนำไปใส่ใน `.env`

## แก้ปัญหา SSL certificate

ถ้ารันแล้วเจอ:

```text
SSL: CERTIFICATE_VERIFY_FAILED
```

ให้ลองแก้ตามลำดับนี้:

1. ติดตั้ง CA bundle ของ Python:

```bash
python3 -m pip install certifi
```

2. ถ้าเครื่อง/องค์กรมีไฟล์ CA certificate เอง ให้ใส่ path ใน `.env`:

```env
TELEGRAM_CA_FILE=/path/to/company-ca.pem
TELEGRAM_SSL_VERIFY=true
```

3. ถ้ายังติด self-signed chain และต้องการให้ส่ง Telegram ได้ก่อน ให้ตั้งค่านี้ใน `.env`:

```env
TELEGRAM_SSL_VERIFY=false
```

ค่านี้ข้ามการตรวจ certificate เฉพาะตอนส่ง Telegram เท่านั้น ส่วนการ login My Office ยังทำงานเหมือนเดิม

## ใช้งานบน Render + cron-job.org

โปรเจกต์นี้มีไฟล์ [render.yaml](/Users/arsarawut/Downloads/Project/Check_My-office/render.yaml) และ [app.py](/Users/arsarawut/Downloads/Project/Check_My-office/app.py) ให้แล้วสำหรับ deploy แบบ web service

### 1. อัปโหลดโค้ดขึ้น GitHub

สร้าง repo ใหม่ แล้ว push ไฟล์ทั้งหมดขึ้นไป ยกเว้น `.env`

คำสั่งตัวอย่าง:

```bash
git init -b main
git add .
git commit -m "Add My Office news checker with Render webhook"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### 2. Deploy บน Render

1. ไปที่ [Render Dashboard](https://dashboard.render.com/)
2. เลือก `New +` > `Blueprint`
3. เลือก GitHub repo นี้
4. Render จะอ่าน `render.yaml` และสร้าง web service ให้
5. ตั้งค่า environment variables บน Render ให้ครบ:

```text
MYOFFICE_USERNAME
MYOFFICE_PASSWORD
MYOFFICE_NEWS_URL
MYOFFICE_ARCHIVED_NEWS_URL
MYOFFICE_ARCHIVED_MAX_PAGES
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
TELEGRAM_CA_FILE
TELEGRAM_SSL_VERIFY
RUN_WEBHOOK_TOKEN
MYOFFICE_STATE_PATH=/tmp/seen_news.json
MYOFFICE_LOCK_PATH=/tmp/check_myoffice_news.lock
```

ถ้าใช้ `certifi` ปกติบน Render สามารถเว้น `TELEGRAM_CA_FILE` ได้ และตั้ง `TELEGRAM_SSL_VERIFY=true`

หมายเหตุ: Render free tier ไม่รองรับ persistent disk ดังนั้น state ใน `/tmp` อาจหายเมื่อ service restart หรือ redeploy ถ้าต้องการเก็บ state ถาวรบน Render ต้องใช้แผนที่รองรับ disk หรือเปลี่ยนไปเก็บ state ใน external database/storage

### 3. ทดสอบ endpoint

สมมติ Render ให้ URL นี้:

`https://check-myoffice-news.onrender.com`

ทดสอบ health check:

```bash
curl https://check-myoffice-news.onrender.com/healthz
```

ทดสอบรันจริง:

```bash
curl -X POST https://check-myoffice-news.onrender.com/run \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer YOUR_RUN_WEBHOOK_TOKEN' \
  -d '{}'
```

ทดสอบ Telegram:

```bash
curl -X POST https://check-myoffice-news.onrender.com/test-telegram \
  -H 'Authorization: Bearer YOUR_RUN_WEBHOOK_TOKEN'
```

### 4. ตั้ง cron-job.org

1. ไปที่ [cron-job.org](https://console.cron-job.org/)
2. สร้าง job ใหม่
3. ตั้ง URL เป็น:

```text
https://check-myoffice-news.onrender.com/run
```

4. ตั้ง method เป็น `POST`
5. เพิ่ม header:

```text
Authorization: Bearer YOUR_RUN_WEBHOOK_TOKEN
Content-Type: application/json
```

6. ใส่ body:

```json
{}
```

7. ตั้ง schedule ตามเวลาที่ต้องการ

ถ้าต้องการ dry-run จาก cron-job.org สามารถใส่ body ชั่วคราวเป็น:

```json
{"dry_run": true}
```
