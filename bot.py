import asyncio
import json
import logging
import os
import io
from PIL import Image, ImageDraw, ImageFont, ImageOps
from datetime import datetime, timezone, timedelta
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('DISCORD_BOT_TOKEN', '').strip()
LOG_CHANNEL_ID = int(os.getenv('LOG_CHANNEL_ID', '0'))
STATE_FILE = Path(__file__).with_name('vc_state.json')
JST = timezone(timedelta(hours=9))
WIDTH = 72  # 8-character label + 1 space + 72 columns = 81 ASCII characters
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('vc-logger')

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True
bot = commands.Bot(command_prefix='!', intents=intents)
lock = asyncio.Lock()
sessions = {}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def dt(value):
    return datetime.fromisoformat(value)


def stamp(value):
    return dt(value).astimezone(JST).strftime('%Y/%m/%d %H:%M:%S')


def hm(value):
    return dt(value).astimezone(JST).strftime('%H:%M')


def display(member):
    return member.display_name


def save():
    tmp = STATE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(STATE_FILE)


def load():
    global sessions
    if STATE_FILE.exists():
        try:
            sessions = json.loads(STATE_FILE.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            logger.exception('状態ファイルの読み込みに失敗しました')
            sessions = {}


def key(channel):
    return str(channel.id)


def create_session(channel, at):
    return {'guild_id': channel.guild.id, 'channel_id': channel.id,
            'channel_name': channel.name, 'start': at, 'members': {},
            'participants': {}, 'first': None, 'last': None,
            'timeline': {}, 'interrupted': False}


def add_event(s, uid, kind, at):
    s.setdefault('timeline', {}).setdefault(uid, []).append({'at': at, 'kind': kind})


def join(s, member, at, initial=False):
    uid = str(member.id)
    s['participants'][uid] = display(member)
    if uid in s['members']:
        return
    s['members'][uid] = {'joined': at, 'stream_start': None}
    if s['first'] is None and not initial:
        s['first'] = uid
        kind = 'F'
    else:
        kind = '?' if initial else 'I'
    add_event(s, uid, kind, at)
    if member.voice and member.voice.self_stream:
        s['members'][uid]['stream_start'] = at
        add_event(s, uid, '?' if initial else 'S', at)


def stop_stream(s, member, at):
    info = s['members'].get(str(member.id))
    if info and info.get('stream_start'):
        info['stream_start'] = None
        add_event(s, str(member.id), 'E', at)


def leave(s, member, at):
    uid = str(member.id)
    if uid not in s['members']:
        return
    stop_stream(s, member, at)
    del s['members'][uid]
    s['last'] = uid
    add_event(s, uid, 'L' if not s['members'] else 'O', at)


def get_font(size, bold=False):
    paths = ([r'C:\Windows\Fonts\meiryob.ttc', r'C:\Windows\Fonts\YuGothB.ttc',
              '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'] if bold else
             [r'C:\Windows\Fonts\meiryo.ttc', r'C:\Windows\Fonts\YuGothR.ttc',
              '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'])
    paths += ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def timeline_image(s, end, avatars):
    """Draw Japanese names, circular avatars, and color-coded timeline bars."""
    start_dt, end_dt = dt(s['start']), dt(end)
    duration = max(1., (end_dt - start_dt).total_seconds())
    people = list(s['participants'].items())
    W, LEFT, RIGHT, ROW = 1500, 360, 65, 76
    H = 230 + len(people) * ROW + 90
    img = Image.new('RGB', (W, H), '#1e1f22')
    draw = ImageDraw.Draw(img)
    strong, normal, small = get_font(27, True), get_font(23), get_font(19)
    green, orange, white, gray = '#63f28a', '#ffae42', '#eef0f4', '#a9afb9'
    x0, x1 = LEFT, W - RIGHT

    def x(at):
        fraction = (dt(at) - start_dt).total_seconds() / duration
        return round(x0 + max(0, min(1, fraction)) * (x1 - x0))

    def label(text, at_x, y, fill):
        bbox = draw.textbbox((0, 0), text, font=small)
        w = bbox[2] - bbox[0]
        draw.rounded_rectangle((at_x-w/2-6, y-34, at_x+w/2+6, y-8), radius=5, fill='#1e1f22')
        draw.text((at_x-w/2, y-34-bbox[1]), text, font=small, fill=fill)

    draw.text((32, 22), f"{s['channel_name']}  |  VC活動記録", font=strong, fill=white)
    draw.text((32, 67), f"{stamp(s['start'])}  ～  {stamp(end)}    参加者 {len(people)}人", font=small, fill=gray)
    draw.text((32, 132), '参加者', font=normal, fill=gray)
    for fraction in (0, .25, .5, .75, 1):
        tx = round(x0 + (x1-x0)*fraction)
        t = start_dt + (end_dt-start_dt)*fraction
        text = t.astimezone(JST).strftime('%H:%M')
        bbox = draw.textbbox((0,0), text, font=small)
        draw.text((tx-(bbox[2]-bbox[0])/2, 131), text, font=small, fill=gray)
        draw.line((tx, 164, tx, 190+len(people)*ROW), fill='#363a40', width=2)

    for i, (uid, name) in enumerate(people):
        y = 205 + i*ROW
        avatar = avatars.get(uid)
        if avatar:
            avatar = ImageOps.fit(avatar.convert('RGB'), (46, 46))
            mask = Image.new('L', (46, 46), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 45, 45), fill=255)
            img.paste(avatar, (32, y-23), mask)
        else:
            draw.ellipse((32,y-23,78,y+23), fill='#5865f2')
            initial = name[:1] if name else '?'
            draw.text((46,y-14), initial, font=normal, fill=white)
        # Clip overly long names without disturbing the timeline column.
        shown = name
        while shown and draw.textbbox((0,0), shown, font=normal)[2] > 245:
            shown = shown[:-1]
        if shown != name:
            shown = shown[:-1] + '…'
        draw.text((90, y-16), shown, font=normal, fill=white)

        events = sorted(s.get('timeline', {}).get(uid, []), key=lambda e:e['at'])
        active, streaming, previous = False, False, None
        for e in events:
            xx = x(e['at'])
            if active and previous is not None and xx > previous:
                draw.line((previous, y, xx, y), fill=orange if streaming else green, width=11)
            kind = e['kind']
            if kind in ('F','I','?'):
                active = True
            elif kind in ('O','L'):
                active, streaming = False, False
            elif kind == 'S':
                streaming = True
            elif kind == 'E':
                streaming = False
            # Event glyphs sit above the bar, with a dot on the exact timestamp.
            draw.ellipse((xx-5,y-5,xx+5,y+5), fill=orange if kind in ('S','E') else green if kind in ('F','I','?') else white)
            label(kind, xx, y, orange if kind in ('S','E') else green if kind in ('F','I','?') else white)
            previous = xx
        if active and previous is not None:
            draw.line((previous,y,x1,y),fill=orange if streaming else green,width=11)

    bottom = 215 + len(people)*ROW
    draw.text((32,bottom), 'F 最初の入室  I 入室  O 退出  L 最後の退出  S 配信開始  E 配信終了  ? 状態不明', font=small, fill=gray)
    draw.line((32,bottom+49,70,bottom+49),fill=green,width=10)
    draw.text((82,bottom+35),'VC入室中',font=small,fill=white)
    draw.line((250,bottom+49,288,bottom+49),fill=orange,width=10)
    draw.text((300,bottom+35),'画面共有中',font=small,fill=white)
    output = io.BytesIO()
    img.save(output,format='PNG')
    output.seek(0)
    return output


async def load_avatars(guild, s):
    avatars = {}
    for uid in s['participants']:
        try:
            member = guild.get_member(int(uid))
            if member is None:
                member = await guild.fetch_member(int(uid))
            data = await member.display_avatar.replace(size=128, format='png').read()
            with Image.open(io.BytesIO(data)) as avatar:
                avatars[uid] = avatar.copy()
        except (discord.HTTPException, OSError, ValueError) as exc:
            logger.warning('アバター取得失敗 %s: %s', uid, exc)
    return avatars


async def send_log(guild, messages):
    channel = guild.get_channel(LOG_CHANNEL_ID) or bot.get_channel(LOG_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
        logger.error('ログ先チャンネルが見つからないか、サーバーが違います: %s', LOG_CHANNEL_ID)
        return False
    for message in messages:
        await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
    return True


async def close_session(channel, s, at):
    name = discord.utils.escape_markdown(s['channel_name'])
    header = (f'**VC終了：{name}**  {stamp(s["start"])} ～ {stamp(at)}\n'
              f'参加人数：{len(s["participants"])}人')
    if s['interrupted']:
        header += '\n⚠️ Bot停止中の出来事は復元できません。? は観測開始時点で在室を示します。'
    dest = channel.guild.get_channel(LOG_CHANNEL_ID) or bot.get_channel(LOG_CHANNEL_ID)
    if not isinstance(dest, discord.TextChannel) or dest.guild.id != channel.guild.id:
        logger.error('ログ先チャンネルが見つからないか、サーバーが違います: %s', LOG_CHANNEL_ID)
        return False
    avatars = await load_avatars(channel.guild, s)
    image_data = timeline_image(s, at, avatars)
    await dest.send(header, file=discord.File(image_data, filename='vc_timeline.png'),
                    allowed_mentions=discord.AllowedMentions.none())
    return True


@bot.event
async def on_ready():
    logger.info('ログイン: %s', bot.user)
    async with lock:
        if getattr(bot, '_vc_initialized', False):
            return
        bot._vc_initialized = True
        at = now_iso()
        channels = {}
        for guild in bot.guilds:
            for channel in guild.voice_channels + guild.stage_channels:
                channels[key(channel)] = channel
                current = {str(m.id): m for m in channel.members if not m.bot}
                s = sessions.get(key(channel))
                if s is None and current:
                    s = create_session(channel, at)
                    s['interrupted'] = True
                    sessions[key(channel)] = s
                    for member in current.values():
                        join(s, member, at, initial=True)
                elif s is not None:
                    s['interrupted'] = True
                    for uid in list(s['members']):
                        if uid not in current:
                            add_event(s, uid, '?', at)
                            del s['members'][uid]
                    for uid, member in current.items():
                        if uid not in s['members']:
                            join(s, member, at, initial=True)
                        else:
                            info = s['members'][uid]
                            if bool(info.get('stream_start')) != bool(member.voice.self_stream):
                                add_event(s, uid, '?', at)
                                info['stream_start'] = at if member.voice.self_stream else None
                    if not current:
                        try:
                            if await close_session(channel, s, at):
                                del sessions[key(channel)]
                        except discord.HTTPException:
                            logger.exception('セッション送信失敗')
        for channel_id in list(sessions):
            if channel_id not in channels:
                del sessions[channel_id]
        save()


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    at = now_iso()
    async with lock:
        if before.channel != after.channel:
            if before.channel:
                s = sessions.get(key(before.channel))
                if s:
                    leave(s, member, at)
                    if not s['members']:
                        try:
                            if await close_session(before.channel, s, at):
                                del sessions[key(before.channel)]
                        except discord.HTTPException:
                            logger.exception('セッション送信失敗')
            if after.channel:
                s = sessions.get(key(after.channel))
                if s is None:
                    s = create_session(after.channel, at)
                    sessions[key(after.channel)] = s
                join(s, member, at)
        elif after.channel:
            s = sessions.get(key(after.channel))
            if s and before.self_stream != after.self_stream:
                uid = str(member.id)
                if after.self_stream and uid in s['members']:
                    s['members'][uid]['stream_start'] = at
                    add_event(s, uid, 'S', at)
                else:
                    stop_stream(s, member, at)
        save()


if __name__ == '__main__':
    if not TOKEN or LOG_CHANNEL_ID <= 0:
        raise SystemExit('DISCORD_BOT_TOKEN と LOG_CHANNEL_ID を .env に設定してください。')
    load()
    bot.run(TOKEN)
