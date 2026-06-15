import discord
from discord.ext import tasks
import json, os, asyncio
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

# === 設定読み込み ===
with open('config.json', 'r', encoding='utf-8') as f:
    cfg = json.load(f)

DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN') or cfg['discord_token']
CATEGORY_NAMES = cfg['category_names']
STAFF_ROLES = cfg['staff_roles']
ALERT_CHANNEL_ID = int(cfg['alert_channel_id'])
MENTOR_ROLE_ID = int(cfg['mentor_role_id'])

NOTIFIED_FILE = 'notified.json'
MAPPING_FILE = 'staff_mapping.json'


def load_notified():
    if not os.path.exists(NOTIFIED_FILE):
        return {}
    with open(NOTIFIED_FILE, encoding='utf-8') as f:
        data = json.load(f)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    return {k: v for k, v in data.items() if v > cutoff}


def save_notified(data):
    with open(NOTIFIED_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_mapping():
    if not os.path.exists(MAPPING_FILE):
        return {}
    with open(MAPPING_FILE, encoding='utf-8') as f:
        return json.load(f)


notified = load_notified()


def get_first_emoji(text):
    if not text:
        return None
    first = text[0]
    return first if ord(first) > 127 else None


# === Flask ===
flask_app = Flask(__name__)

@flask_app.route('/', methods=['GET'])
def health():
    return 'Bot is running!', 200


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
    return any(role.name in STAFF_ROLES for role in member.roles)


async def send_discord_notification(guild_name, channel_name, link, staff_info):
    alert_channel = client.get_channel(ALERT_CHANNEL_ID)

    if alert_channel is None:
        try:
            alert_channel = await client.fetch_channel(ALERT_CHANNEL_ID)
        except Exception as e:
            print(f"❌ 通知先チャンネル取得エラー: {e}")
            return

    if staff_info and staff_info.get("discord_user_id"):
        mention = f"<@{staff_info['discord_user_id']}>"
        assignee = staff_info.get("display_name", "担当者")
    else:
        mention = f"<@&{MENTOR_ROLE_ID}>"
        assignee = "未登録のためメンター全体"

    text = (
        f"{mention}\n\n"
        f"⚠️ **返信漏れの可能性があります**\n\n"
        f"**サーバー：** {guild_name}\n"
        f"**チャンネル：** {channel_name}\n"
        f"**状況：** 24時間以上スタンプ・返信なし\n\n"
        f"**該当メッセージ：**\n{link}"
    )

    try:
        await alert_channel.send(text)
        print(f"✅ Discord通知送信: #{channel_name} → {assignee}")
    except Exception as e:
        print(f"❌ Discord通知エラー: {e}")


@tasks.loop(minutes=10)
async def check_unanswered():
    global notified

    now = datetime.now(timezone.utc)
    check_before = now - timedelta(hours=24)
    check_after = now - timedelta(hours=48)

    mapping = load_mapping()

    for guild in client.guilds:
        for category_name in CATEGORY_NAMES:
            category = discord.utils.get(guild.categories, name=category_name)

            if not category:
                print(f"⚠️ {guild.name}: カテゴリ '{category_name}' が見つかりません")
                continue

            for channel in category.text_channels:
                first_missed_link = None

                try:
                    async for msg in channel.history(
                        after=check_after,
                        before=check_before,
                        oldest_first=True
                    ):
                        if msg.author.bot:
                            continue

                        if str(msg.id) in notified:
                            continue

                        author = guild.get_member(msg.author.id)

                        if is_staff(author):
                            notified[str(msg.id)] = now.isoformat()
                            continue

                        staff_responded = False

                        # この投稿より後にスタッフが投稿したか確認
                        async for later in channel.history(
                            after=msg.created_at,
                            oldest_first=True,
                            limit=500
                        ):
                            later_author = guild.get_member(later.author.id)
                            if is_staff(later_author):
                                staff_responded = True
                                break

                        # スタッフのリアクション確認
                        if not staff_responded:
                            try:
                                full_msg = await channel.fetch_message(msg.id)

                                for reaction in full_msg.reactions:
                                    async for user in reaction.users():
                                        member = guild.get_member(user.id)
                                        if is_staff(member):
                                            staff_responded = True
                                            break

                                    if staff_responded:
                                        break

                            except Exception as e:
                                print(f"⚠️ リアクション確認エラー: #{channel.name}: {e}")

                        notified[str(msg.id)] = now.isoformat()

                        if not staff_responded and not first_missed_link:
                            first_missed_link = msg.jump_url

                    if first_missed_link:
                        emoji = get_first_emoji(channel.name)
                        staff_info = mapping.get(emoji) if emoji else None

                        await send_discord_notification(
                            guild.name,
                            channel.name,
                            first_missed_link,
                            staff_info
                        )

                        await asyncio.sleep(0.5)

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

    if not check_unanswered.is_running():
        check_unanswered.start()


client.run(DISCORD_TOKEN)
