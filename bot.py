import discord
from discord.ext import tasks
import json, os, aiohttp
from datetime import datetime, timedelta, timezone
from flask import Flask, request
import threading

# === 設定読み込み ===
with open('config.json', 'r', encoding='utf-8') as f:
    cfg = json.load(f)

DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN') or cfg['discord_token']
LINE_TOKEN    = os.environ.get('LINE_TOKEN') or cfg['line_token']
LINE_GROUP_ID = os.environ.get('LINE_GROUP_ID', cfg.get('line_group_id', ''))
CATEGORY_NAME = cfg['category_name']
STAFF_ROLES   = cfg['staff_roles']
NOTIFIED_FILE = 'notified.json'

def load_notified():
    if not os.path.exists(NOTIFIED_FILE):
        return {}
    with open(NOTIFIED_FILE) as f:
        data = json.load(f)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    return {k: v for k, v in data.items() if v > cutoff}

def save_notified(data):
    with open(NOTIFIED_FILE, 'w') as f:
        json.dump(data, f)

notified = load_notified()

# === Flask（LINEグループID取得用） ===
flask_app = Flask(__name__)

@flask_app.route('/', methods=['GET'])
def health():
    return 'Bot is running!', 200

@flask_app.route('/webhook', methods=['POST'])
def webhook():
    body = request.get_json(silent=True) or {}
    for event in body.get('events', []):
        gid = event.get('source', {}).get('groupId')
        if gid:
            print(f"[LINE] グループID: {gid}")
    return 'OK', 200

def start_flask():
    port = int(os.environ.get('PORT', 8080))
    flask_app.run(host='0.0.0.0', port=port, use_reloader=False)

# === Discord ===
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
client = discord.Client(intents=intents)

def is_staff(member):
    if not member:
        return False
    return any(r.name in STAFF_ROLES for r in member.roles)

async def send_line(missed_list):
    if not LINE_GROUP_ID:
        print("⚠️ LINE_GROUP_IDが未設定です")
        return

    text = "@all\n\n⚠️ 24時間以上未返信のメッセージがあります\n\n"
    for item in missed_list:
        text += f"【{item['channel']}】\n{item['link']}\n\n"
    text = text.strip()

    payload = {
        "to": LINE_GROUP_ID,
        "messages": [{
            "type": "text",
            "text": text,
            "mention": {
                "mentionees": [{"index": 0, "length": 4, "type": "all"}]
            }
        }]
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_TOKEN}"
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.line.me/v2/bot/message/push",
            json=payload, headers=headers
        ) as resp:
            result = await resp.text()
            if resp.status == 200:
                print(f"✅ LINE送信完了: {len(missed_list)}チャンネル")
            else:
                print(f"❌ LINE送信エラー {resp.status}: {result}")

@tasks.loop(minutes=10)
async def check_unanswered():
    global notified
    now = datetime.now(timezone.utc)
    check_before = now - timedelta(hours=24)
    check_after  = now - timedelta(hours=48)

    missed_list = []

    for guild in client.guilds:
        category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
        if not category:
            print(f"⚠️ カテゴリ '{CATEGORY_NAME}' が見つかりません")
            continue

        for channel in category.text_channels:
            first_missed_link = None
            try:
                async for msg in channel.history(
                    after=check_after, before=check_before, oldest_first=True
                ):
                    if msg.author.bot:
                        continue
                    if str(msg.id) in notified:
                        continue

                    author = guild.get_member(msg.author.id)
                    if is_staff(author):
                        notified[str(msg.id)] = now.isoformat()
                        continue

                    # この投稿より後に運営が投稿したか確認
                    staff_responded = False
                    async for later in channel.history(
                        after=msg.created_at, oldest_first=True, limit=500
                    ):
                        if is_staff(guild.get_member(later.author.id)):
                            staff_responded = True
                            break

                    # スタンプ確認
                    if not staff_responded:
                        try:
                            full_msg = await channel.fetch_message(msg.id)
                            for reaction in full_msg.reactions:
                                async for user in reaction.users():
                                    if is_staff(guild.get_member(user.id)):
                                        staff_responded = True
                                        break
                                if staff_responded:
                                    break
                        except Exception:
                            pass

                    notified[str(msg.id)] = now.isoformat()

                    if not staff_responded and not first_missed_link:
                        first_missed_link = msg.jump_url

                if first_missed_link:
                    missed_list.append({
                        'channel': channel.name,
                        'link': first_missed_link
                    })

            except discord.Forbidden:
                print(f"⚠️ アクセス権限なし: #{channel.name}")
            except Exception as e:
                print(f"❌ エラー (#{channel.name}): {e}")

    save_notified(notified)

    if missed_list:
        print(f"🔔 未返信 {len(missed_list)}チャンネル → LINE通知送信")
        await send_line(missed_list)
    else:
        print(f"✅ 未返信なし ({now.strftime('%Y-%m-%d %H:%M')} UTC)")

@client.event
async def on_ready():
    print(f"✅ ボット起動: {client.user}")
    threading.Thread(target=start_flask, daemon=True).start()
    check_unanswered.start()

client.run(DISCORD_TOKEN)
