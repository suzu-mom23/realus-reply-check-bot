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

REPLY_NOTIFIED_FILE = 'notified.json'
FOLLOWUP_NOTIFIED_FILE = 'followup_notified.json'
MAPPING_FILE = 'staff_mapping.json'


def load_notified(filename, keep_days):
    if not os.path.exists(filename):
        return {}
    with open(filename, encoding='utf-8') as f:
        data = json.load(f)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    return {k: v for k, v in data.items() if v > cutoff}


def save_notified(filename, data):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_mapping():
    if not os.path.exists(MAPPING_FILE):
        return {}
    with open(MAPPING_FILE, encoding='utf-8') as f:
        return json.load(f)


reply_notified = load_notified(REPLY_NOTIFIED_FILE, keep_days=3)
followup_notified = load_notified(FOLLOWUP_NOTIFIED_FILE, keep_days=14)


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


def is_student(member, user=None):
    if user and user.bot:
        return False
    if member and is_staff(member):
        return False
    return True


def get_mention_and_assignee(channel_name, mapping):
    emoji = get_first_emoji(channel_name)
    staff_info = mapping.get(emoji) if emoji else None

    if staff_info and staff_info.get("discord_user_id"):
        mention = f"<@{staff_info['discord_user_id']}>"
        assignee = staff_info.get("display_name", "担当者")
    else:
        mention = f"<@&{MENTOR_ROLE_ID}>"
        assignee = "未登録のためメンター全体"

    return mention, assignee


async def get_alert_channel():
    alert_channel = client.get_channel(ALERT_CHANNEL_ID)
    if alert_channel is None:
        alert_channel = await client.fetch_channel(ALERT_CHANNEL_ID)
    return alert_channel


async def send_reply_notification(guild_name, channel_name, link, mapping):
    try:
        alert_channel = await get_alert_channel()
    except Exception as e:
        print(f"❌ 通知先チャンネル取得エラー: {e}")
        return

    mention, assignee = get_mention_and_assignee(channel_name, mapping)

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
        print(f"✅ 返信漏れ通知送信: #{channel_name} → {assignee}")
    except Exception as e:
        print(f"❌ Discord通知エラー: {e}")


async def send_followup_notification(guild_name, channel_name, link, mapping):
    try:
        alert_channel = await get_alert_channel()
    except Exception as e:
        print(f"❌ 通知先チャンネル取得エラー: {e}")
        return

    mention, assignee = get_mention_and_assignee(channel_name, mapping)

    text = (
        f"{mention}\n\n"
        f"⚠️ **返信が来ていない可能性があります**\n\n"
        f"**サーバー：** {guild_name}\n"
        f"**チャンネル：** {channel_name}\n"
        f"**状況：** 7日以上受講生からスタンプ・返信なし\n\n"
        f"必要があれば、リマインドをお願いいたします！\n\n"
        f"**該当メッセージ：**\n{link}"
    )

    try:
        await alert_channel.send(text)
        print(f"✅ 7日反応なし通知送信: #{channel_name} → {assignee}")
    except Exception as e:
        print(f"❌ Discord通知エラー: {e}")


async def check_24h_reply_missing(guild, category, mapping, now):
    global reply_notified

    check_before = now - timedelta(hours=24)
    check_after = now - timedelta(hours=48)

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

                if str(msg.id) in reply_notified:
                    continue

                author = guild.get_member(msg.author.id)

                if is_staff(author):
                    reply_notified[str(msg.id)] = now.isoformat()
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

                reply_notified[str(msg.id)] = now.isoformat()

                if not staff_responded and not first_missed_link:
                    first_missed_link = msg.jump_url

            if first_missed_link:
                await send_reply_notification(
                    guild.name,
                    channel.name,
                    first_missed_link,
                    mapping
                )
                await asyncio.sleep(0.5)

        except discord.Forbidden:
            print(f"⚠️ アクセス権限なし: #{channel.name}")
        except Exception as e:
            print(f"❌ 24時間返信漏れチェックエラー (#{channel.name}): {e}")


async def check_7d_no_student_reaction(guild, category, mapping, now):
    global followup_notified

    check_before = now - timedelta(days=7)
    check_after = now - timedelta(days=14)

    for channel in category.text_channels:
        try:
            latest_staff_msg = None

            # 直近14日以内の中で、最新のスタッフ投稿を探す
            async for msg in channel.history(
                after=check_after,
                limit=1000,
                oldest_first=False
            ):
                if msg.author.bot:
                    continue

                author = guild.get_member(msg.author.id)

                if is_staff(author):
                    latest_staff_msg = msg
                    break

            if not latest_staff_msg:
                continue

            # 最新スタッフ投稿がまだ7日経っていなければ対象外
            if latest_staff_msg.created_at > check_before:
                continue

            if str(latest_staff_msg.id) in followup_notified:
                continue

            student_responded = False

            # 最新スタッフ投稿より後に、受講生の通常投稿があるか確認
            async for later in channel.history(
                after=latest_staff_msg.created_at,
                oldest_first=True,
                limit=500
            ):
                if later.author.bot:
                    continue

                later_member = guild.get_member(later.author.id)

                if is_student(later_member, later.author):
                    student_responded = True
                    break

            # 最新スタッフ投稿に、受講生リアクションがあるか確認
            if not student_responded:
                try:
                    full_msg = await channel.fetch_message(latest_staff_msg.id)

                    for reaction in full_msg.reactions:
                        async for user in reaction.users():
                            member = guild.get_member(user.id)

                            if is_student(member, user):
                                student_responded = True
                                break

                        if student_responded:
                            break

                except Exception as e:
                    print(f"⚠️ 7日反応なしリアクション確認エラー: #{channel.name}: {e}")

            followup_notified[str(latest_staff_msg.id)] = now.isoformat()

            if not student_responded:
                await send_followup_notification(
                    guild.name,
                    channel.name,
                    latest_staff_msg.jump_url,
                    mapping
                )
                await asyncio.sleep(0.5)

        except discord.Forbidden:
            print(f"⚠️ アクセス権限なし: #{channel.name}")
        except Exception as e:
            print(f"❌ 7日反応なしチェックエラー (#{channel.name}): {e}")


@tasks.loop(minutes=10)
async def check_all():
    now = datetime.now(timezone.utc)
    mapping = load_mapping()

    for guild in client.guilds:
        for category_name in CATEGORY_NAMES:
            category = discord.utils.get(guild.categories, name=category_name)

            if not category:
                print(f"ℹ️ {guild.name}: カテゴリ '{category_name}' は存在しません")
                continue

            await check_24h_reply_missing(guild, category, mapping, now)
            await check_7d_no_student_reaction(guild, category, mapping, now)

    save_notified(REPLY_NOTIFIED_FILE, reply_notified)
    save_notified(FOLLOWUP_NOTIFIED_FILE, followup_notified)

    print(f"✅ 全チェック完了 ({now.strftime('%Y-%m-%d %H:%M')} UTC)")


@client.event
async def on_ready():
    print(f"✅ ボット起動: {client.user}")
    threading.Thread(target=start_flask, daemon=True).start()

    if not check_all.is_running():
        check_all.start()


client.run(DISCORD_TOKEN)
