import numpy as np
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import os
import asyncio
from datetime import timedelta
import yt_dlp as youtube_dl
import uyts
import sqlite3

# データベース接続を確立
conn = sqlite3.connect('bot_settings.db')
c = conn.cursor()

# テーブルが存在しない場合は作成
c.execute('''
CREATE TABLE IF NOT EXISTS settings (
guild_id INTEGER PRIMARY KEY,
language TEXT
)
''')

# カレントディレクトリのパスを取得
current_dir = os.path.dirname(os.path.abspath(__file__))
# cookies.txtファイルのパスを設定
cookies_path = os.path.join(current_dir, 'cookies.txt')

load_dotenv()

TOKEN = os.getenv('DISCORD_BOT_TOKEN')

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# 言語設定を保存する辞書
language_settings = {}

# キューを保存する辞書
music_queue = {}

# ループ設定を保存する辞書
loop_settings = {}

# 再生中フラグを管理する辞書
playing_status = {}

# メッセージの翻訳辞書を外部ファイルから読み込む関数
def load_messages(language):
    messages = {}
    try:
        with open(f'{language}.txt', 'r', encoding='utf-8') as file:
            for line in file:
                key, value = line.strip().split(' = ')
                messages[key] = value
    except FileNotFoundError:
        print(f"Error: {language}.txt not found. Using English as default.")
        return load_messages("en")
    return messages

# 設定を保存する関数
def save_settings(guild_id, language):
    with conn:
        c.execute('''
        INSERT OR REPLACE INTO settings (guild_id, language)
        VALUES (?, ?)
        ''', (guild_id, language))

# 設定を読み込む関数
def load_settings(guild_id):
    c.execute('SELECT language FROM settings WHERE guild_id = ?', (guild_id,))
    result = c.fetchone()
    return result[0] if result else "EN"

# デフォルトのメッセージを読み込み
messages_en = load_messages('en')
messages_jp = load_messages('ja')

# ボット起動時に設定を読み込む
@bot.event
async def on_ready():
    for guild in bot.guilds:
        language_settings[guild.id] = load_settings(guild.id)
    await bot.sync_commands()
    check_inactivity.start()
    print(f'Logged in as {bot.user}')

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    voice_client = before.channel.guild.voice_client if before.channel else None
    if voice_client and not voice_client.is_playing() and len(voice_client.channel.members) == 1:
        await voice_client.disconnect()
        guild_id = before.channel.guild.id
        music_queue[guild_id] = []
        playing_status[guild_id] = False

@bot.slash_command(name="setup", description="Set the language for the bot")
async def slash_setup(interaction: discord.Interaction):
    options = [
        discord.SelectOption(label="English", value="EN"),
        discord.SelectOption(label="日本語", value="JP")
    ]
    select = discord.ui.Select(placeholder="Choose a language...", options=options)

    async def callback(interaction):
        language = select.values[0]
        guild_id = interaction.guild.id
        language_settings[guild_id] = language
        save_settings(guild_id, language)
        messages = messages_en if language == "EN" else messages_jp
        embed = discord.Embed(title="Language Setup", description=messages["setup"].format(language=language), color=discord.Color.blue())
        await interaction.response.send_message(embed=embed)

    select.callback = callback

    view = discord.ui.View()
    view.add_item(select)
    await interaction.response.send_message("Choose a language:", view=view)

@bot.slash_command(name="play", description="Play a song from YouTube")
async def slash_play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    guild_id = interaction.guild.id
    language = language_settings.get(guild_id, "EN")
    messages = messages_en if language == "EN" else messages_jp

    if "discord.gg" in query or "discord.com/invite" in query:
        await interaction.followup.send(messages["invalid_url"])
        return

    if query.startswith("http://") or query.startswith("https://"):
        url = query
    else:
        search = uyts.Search(query)
        search_results = search.results
        if not search_results:
            await interaction.followup.send(messages["no_results"])
            return

        options = []
        for result in search_results[:10]:
            video_url = f"https://www.youtube.com/watch?v={result.id}"
            try:
                youtube_dl.YoutubeDL().extract_info(video_url, download=False)
                options.append(discord.SelectOption(label=result.title, value=video_url))
            except youtube_dl.utils.DownloadError:
                continue

        if not options:
            await interaction.followup.send(messages["no_results"])
            return

        async def callback(interaction):
            selected_url = select.values[0]
            await add_to_queue(interaction.guild.id, selected_url, interaction)

        select = discord.ui.Select(placeholder=messages["choose_song"], options=options)
        select.callback = callback

        view = discord.ui.View()
        view.add_item(select)
        await interaction.followup.send(messages["choose_song"], view=view)
        return

    await add_to_queue(interaction.guild.id, url, interaction)

@bot.command(name="play")
async def play(ctx, *, query):
    guild_id = ctx.guild.id
    language = language_settings.get(guild_id, "EN")
    messages = messages_en if language == "EN" else messages_jp

    if "discord.gg" in query or "discord.com/invite" in query:
        await ctx.send(messages["invalid_url"])
        return

    if query.startswith("http://") or query.startswith("https://"):
        url = query
    else:
        search = uyts.Search(query)
        search_results = search.results
        if not search_results:
            await ctx.send(messages["no_results"])
            return

        options = [discord.SelectOption(label=result.title, value=f"https://www.youtube.com/watch?v={result.id}") for result in search_results[:10]]

        if not options:
            await ctx.send(messages["no_results"])
            return

        async def callback(interaction):
            selected_url = select.values[0]
            await add_to_queue(ctx.guild.id, selected_url, ctx)

        select = discord.ui.Select(placeholder=messages["choose_song"], options=options)
        select.callback = callback

        view = discord.ui.View()
        view.add_item(select)
        await ctx.send(messages["choose_song"], view=view)
        return

    voice_channel = ctx.author.voice.channel
    if voice_channel is None:
        await ctx.send(messages["need_voice_channel"])
        return

    vc = ctx.guild.voice_client

    if vc is None:
        vc = await voice_channel.connect()

    await add_to_queue(ctx.guild.id, url, ctx)

@bot.slash_command(name="loop", description="Enable or disable loop for the current song")
async def slash_loop(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    loop_settings[guild_id] = not loop_settings.get(guild_id, False)
    language = language_settings.get(guild_id, "EN")
    messages = messages_en if language == "EN" else messages_jp
    message = messages["loop_enabled"] if loop_settings[guild_id] else messages["loop_disabled"]
    await interaction.response.send_message(message)

@bot.command(name="loop")
async def loop(ctx):
    guild_id = ctx.guild.id
    loop_settings[guild_id] = not loop_settings.get(guild_id, False)
    language = language_settings.get(guild_id, "EN")
    messages = messages_en if language == "EN" else messages_jp
    message = messages["loop_enabled"] if loop_settings[guild_id] else messages["loop_disabled"]
    await ctx.send(message)

@bot.command(name="leave")
async def leave(ctx):
    voice_client = ctx.guild.voice_client
    if voice_client:
        await voice_client.disconnect()
        language = language_settings.get(ctx.guild.id, "EN")
        messages = messages_en if language == "EN" else messages_jp
        await ctx.send(messages["leave"])
        # キューをクリア
        music_queue[ctx.guild.id] = []
        playing_status[ctx.guild.id] = False

@bot.command(name="skip")
async def skip(ctx):
    voice_client = ctx.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.stop()
        await ctx.send("Skipped the current song!")
        await process_queue(ctx)
    else:
        await ctx.send("No song is currently playing.")

@bot.command(name="stop")
async def stop(ctx):
    voice_client = ctx.guild.voice_client
    if voice_client:
        await voice_client.disconnect()
        await ctx.send("Music stopped and disconnected from voice channel.")
        # キューをクリア
        music_queue[ctx.guild.id] = []
        playing_status[ctx.guild.id] = False

def fetch_info_sync(url):
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'default_search': 'ytsearch',
        'concurrent-fragments': 5,
        'limit-rate': '0',
    }
    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

async def fetch_info(url):
    return await asyncio.to_thread(fetch_info_sync, url)

async def add_to_queue(guild_id, query, ctx):
    if guild_id not in music_queue:
        music_queue[guild_id] = []
    info = await fetch_info(query)
    if 'entries' in info:
        info = info['entries'][0]
    music_queue[guild_id].append({
        'id': info['id'],
        'webpage_url': info['webpage_url'],
        'title': info['title'],
        'duration': info.get('duration', 0)
    })
    queue_position = len(music_queue[guild_id])

    title = info['title']
    duration = str(timedelta(seconds=info.get('duration', 0)))
    url = info['webpage_url']

    language = language_settings.get(guild_id, "EN")
    messages = messages_en if language == "EN" else messages_jp
    embed_description = messages["added_to_queue"].format(title=title, duration=duration, track_number=queue_position)
    embed = discord.Embed(description=embed_description, color=discord.Color.blue())

    if isinstance(ctx, discord.Interaction):
        await ctx.response.send_message(embed=embed)
    else:
        await ctx.send(embed=embed)

    if guild_id not in playing_status:
        playing_status[guild_id] = False

    if not playing_status[guild_id]:
        await process_queue(ctx)

async def process_queue(ctx):
    guild_id = ctx.guild.id
    if guild_id in music_queue and music_queue[guild_id]:
        info = music_queue[guild_id].pop(0)
        playing_status[guild_id] = True
        await play_music(ctx, info)
    else:
        playing_status[guild_id] = False

async def play_music(ctx, info, initial_volume=0.5):
    if isinstance(ctx, discord.Interaction):
        voice_state = ctx.user.voice
    else:
        voice_state = ctx.author.voice
    guild_id = ctx.guild.id
    language = language_settings.get(guild_id, "EN")
    messages = messages_en if language == "EN" else messages_jp

    if voice_state is None or voice_state.channel is None:
        await ctx.send(messages["need_voice_channel"])
        return

    voice_channel = voice_state.channel
    vc = ctx.guild.voice_client

    if vc is None:
        vc = await voice_channel.connect()

    # 再生直前に最新のURLを取得
    latest_info = await fetch_info(info['webpage_url'])
    if 'entries' in latest_info:
        latest_info = latest_info['entries'][0]
    url2 = latest_info['url']
    title = latest_info['title']
    duration = str(timedelta(seconds=latest_info.get('duration', 0)))

    # ffmpegで音量正規化（loudnormフィルタ）を追加
    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn -af loudnorm=I=-16:TP=-1.5:LRA=11'
    }

    ffmpeg_audio = discord.FFmpegPCMAudio(url2, **FFMPEG_OPTIONS)
    source = discord.PCMVolumeTransformer(ffmpeg_audio)
    source.volume = initial_volume
    vc.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(handle_song_end(ctx, info), bot.loop).result())

    if not loop_settings.get(guild_id, False):
        embed_description = messages["now_playing"].format(title=title, duration=duration, track_number=1)
        embed = discord.Embed(title=embed_description, description=title, url=url2, color=discord.Color.green())
        await ctx.send(embed=embed)

async def handle_song_end(ctx, info):
    guild_id = ctx.guild.id

    if loop_settings.get(guild_id, False):
        await play_music(ctx, info)
    else:
        playing_status[guild_id] = False
        await process_queue(ctx)

@tasks.loop(minutes=1)
async def check_inactivity():
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            voice_client = vc.guild.voice_client
            if voice_client:
                if voice_client.is_playing():
                    continue
                if not any(member.bot for member in vc.members):
                    await voice_client.disconnect()

bot.run(TOKEN)