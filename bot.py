import discord
from discord.ext import tasks
import json, os, aiohttp, asyncio
from datetime import datetime, timedelta, timezone
from flask import Flask, request
import threading
import requests as req_lib

# === 設定読み込み ===
with open('config.json', 'r', encoding='utf-8') as f:
    cfg = json.load(f)

DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN') or cfg['discord_token']
LINE_TOKEN    = os.environ.get('LINE_TOKEN') or cfg['line_token']
LINE_GROUP_ID = os.environ.get('LINE_GROUP_ID', cfg.get('line_group_id', ''))
CATEGORY_NAME = cfg['category_name']
STAFF_ROLES   = cfg['staff_roles']

NOTIFIED_FILE = 'notified.json'
MAPPING_FILE  = 'staff_mapping.json'

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

def load_mapping():
    if not os.path.exists(MAPPING_FILE):
        return {}
    with open(MAPPING_FILE) as f:
        return json.load(f)

def save_mapping(data):
    with open(MAPPING_FILE, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

notified = load_notified()

def get_first_emoji(text):
    """チャンネル名先頭の絵文字を取得"""
    if not text:
        return None
    first = text[0]
    return first if ord(first) > 127 else None

# === Flask ===
flask_app = Flask(__name__)

@flask_app.route('/', methods=['GET'])
def health():
    return 'Bot is running!', 200

@flask_app.route('/webhook', methods=['POST'])
def webhook():
    body = request.get_json(silent=True) or {}
    for event in body.get('events', []):
        source = event.get('source', {})

        # グループID表示（初回設定用）
        gid = source.get('groupId')
        if gid:
            print(f"[LINE] グループID: {gid}")

        # 登録コマンド処理
        if event.get('type') == 'message' and event['message']['type'] == 'text':
            text = event['message']['text'].strip()
            user_id = source.get('userId', '')
            reply_token = event.get('replyToken', '')

            # コマンド形式: 「登録 🌷 すず」
            if text.startswith('登録 ') and user_id:
                parts = text[3:].strip().split(' ', 1)
                emoji_key    = parts[0]
                display_name = parts[1] if len(parts) > 1 else emoji_key

                if emoji_key:
                    mapping = load_mapping()
                    mapping[emoji_key] = {
                        "line_user_id": user_id,
                        "display_name": display_name
                    }
                    save_mapping(mapping)
                    print(f"[登録完了] {emoji_key}（{display_name}）→ {user_id}")

                    if reply_token:
                        req_lib.post(
                            "https://api.line.me/v2/bot/message/reply",
                            json={
                                "replyToken": reply_token,
                                "messages": [{
                                    "type": "text",
                                    "text": f"✅ {emoji_key}（{display_name}）の担当者として登録しました！"
                                }]
                            },
                            headers={
                                "Authorization": f"Bearer {LINE_TOKEN}",
                                "Content-Type": "application/json"
                            }
                        )

            # 登録一覧確認コマンド
            elif text.strip() == '登録一覧':
                mapping = load_mapping()
                reply_token = event.get('replyToken', '')
                if mapping:
                    lines = [f"{emoji}：{info['display_name']}" for emoji, info in mapping.items()]
                    reply_text = "📋 現在の担当者登録一覧:\n\n" + "\n".join(lines)
                else:
                    reply_text = "まだ登録されていません。\n「登録 🌷 すず」の形式で登録してください。"

                if reply_token:
                    req_lib.post(
                        "https://api.line.me/v2/bot/message/reply",
                        json={
                            "replyToken": reply_token,
                            "messages": [{"type": "text", "text": reply_text}]
                        },
                        headers={
                            "Authorization": f"Bearer {LINE_TOKEN}",
                            "Content-Type": "application/json"
                        }
                    )

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

async def send_line_notification(channel_name, link, staff_info):
    if not LINE_GROUP_ID:
        print("⚠️ LINE_GROUP_IDが未設定です")
        return

    if staff_info:
        display = f"@{staff_info['display_name']}"
        text = f"{display}\n\n⚠️ 24時間以上未返信のメッセージがあります\n\n【{channel_name}】\n{link}"
        mentionees = [{
            "index": 0,
            "length": len(display),
            "type": "user",
            "userId": staff_info['line_user_id']
        }]
    else:
        # 担当者未登録の場合は全員に通知
        text = f"@all\n\n⚠️ 24時間以上未返信のメッセージがあります\n\n【{channel_name}】\n{link}"
        mentionees = [{"index": 0, "length": 4, "type": "all"}]

    payload = {
        "to": LINE_GROUP_ID,
        "messages": [{
            "type": "text",
            "text": text,
            "mention": {"mentionees": mentionees}
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
            name = staff_info['display_name'] if staff_info else '@all'
            if resp.status == 200:
                print(f"✅ LINE送信: #{channel_name} → {name}")
            else:
                print(f"❌ LINE送信エラー {resp.status}: {result}")

@tasks.loop(minutes=10)
async def check_unanswered():
    global notified
    now = datetime.now(timezone.utc)
    check_before = now - timedelta(hours=24)
    check_after  = now - timedelta(hours=48)

    mapping = load_mapping()

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

                    # スタンプ（リアクション）確認
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
                    emoji = get_first_emoji(channel.name)
                    staff_info = mapping.get(emoji) if emoji else None
                    await send_line_notification(channel.name, first_missed_link, staff_info)
                    await asyncio.sleep(0.5)  # 連続送信の間隔

            except discord.Forbidden:
                print(f"⚠️ アクセス権限なし: #{channel.name}")
            except Exception as e:
                print(f"❌ エラー (#{channel.name}): {e}")

    save_notified(notified)
    print(f"✅ チェック完了 ({now.strftime('%Y-%m-%d %H:%M')} UTC)")

@client.event
async def on_ready():
    print(f"✅ ボット起動: {client.user}")
    threading.Thread(target=start_flask, daemon=True).start()
    check_unanswered.start()

client.run(DISCORD_TOKEN)
