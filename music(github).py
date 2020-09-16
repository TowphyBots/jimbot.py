import asyncio

import discord
import youtube_dl

import aiohttp

import os

from discord.ext import commands
from discord import Spotify
from discord import Streaming
from discord.ext.commands import guild_only
# Suppress noise about console usage from errors
import asyncio
import functools
import itertools
import math
import random
import requests
import json
import sqlite3
import datetime


import youtube_dl
from async_timeout import timeout
import re
import configparser

import time as timeModule
import random

youtube_dl.utils.bug_reports_message = lambda: ''


class VoiceError(Exception):
    pass


class YTDLError(Exception):
    pass


class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: commands.Context, source: discord.FFmpegPCMAudio, *, data: dict, volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')

    def __str__(self):
        return '**{0.title}** by **{0.uploader}**'.format(self)

    @classmethod
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(cls.ytdl.extract_info, search, download=False, process=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        if 'entries' not in data:
            process_info = data
        else:
            process_info = None
            for entry in data['entries']:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        webpage_url = process_info['webpage_url']
        partial = functools.partial(cls.ytdl.extract_info, webpage_url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError('Couldn\'t fetch `{}`'.format(webpage_url))

        if 'entries' not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info['entries'].pop(0)
                except IndexError:
                    raise YTDLError('Couldn\'t retrieve any matches for `{}`'.format(webpage_url))

        return cls(ctx, discord.FFmpegPCMAudio(info['url'], **cls.FFMPEG_OPTIONS), data=info)

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append('{} days'.format(days))
        if hours > 0:
            duration.append('{} hours'.format(hours))
        if minutes > 0:
            duration.append('{} minutes'.format(minutes))
        if seconds > 0:
            duration.append('{} seconds'.format(seconds))

        return ', '.join(duration)


class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(title='Now playing',
                               description='```css\n{0.source.title}\n```'.format(self),
                               color=discord.Color.blurple())
                 .add_field(name='Song Duration', value=self.source.duration)
                 .add_field(name='Requested by', value=self.requester.mention)
                 .add_field(name='Uploader', value='[{0.source.uploader}]({0.source.uploader_url})'.format(self))
                 .add_field(name='URL', value='[Click]({0.source.url})'.format(self))
                 .set_thumbnail(url=self.source.thumbnail))

        return embed


class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.5
        self.skip_votes = set()

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop:
                # Try to get the next song within 3 minutes.
                # If no song will be added to the queue in time,
                # the player will disconnect due to performance
                # reasons.
                try:
                    async with timeout(180):  # 3 minutes
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    return

            self.current.source.volume = self._volume
            self.voice.play(self.current.source, after=self.play_next_song)
            await self.current.source.channel.send(embed=self.current.create_embed())

            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))

        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        if not state:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state

        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage('This command can\'t be used in DM channels.')

        return True

    async def cog_before_invoke(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.send('An error occurred: {}'.format(str(error)))

    @commands.command(name='join', invoke_without_subcommand=True)
    async def _join(self, ctx: commands.Context):
        """Joins a voice channel."""

        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        await ctx.send(f"Joined {ctx.author.voice.channel.mention}")
        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='summon')
    @commands.has_permissions(manage_guild=True)
    async def _summon(self, ctx: commands.Context, *, channel: discord.VoiceChannel = None):
        """Summons the bot to a voice channel.
        If no channel was specified, it joins your channel.
        """

        if not channel and not ctx.author.voice:
            raise VoiceError('You are neither connected to a voice channel nor specified a channel to join.')

        destination = channel or ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='leave', aliases=['disconnect'])
    @commands.has_permissions(manage_guild=True)
    async def _leave(self, ctx: commands.Context):
        """Clears the queue and leaves the voice channel."""

        if not ctx.voice_state.voice:
            return await ctx.send('Not connected to any voice channel.')

        await ctx.send(f"Left {ctx.author.voice.channel.mention}")
        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]

    @commands.command()
    async def volume(self, ctx, volume: int):
        """Changes the player's volume"""

        if ctx.voice_client is None:
            return await ctx.send("Not connected to a voice channel.")

        ctx.voice_client.source.volume = volume / 100
        await ctx.send("Changed volume to {}%".format(volume))

    @commands.command(name='now', aliases=['current', 'playing'])
    async def _now(self, ctx: commands.Context):
        """Displays the currently playing song."""

        await ctx.send(embed=ctx.voice_state.current.create_embed())

    @commands.command(name='pause')
    @commands.has_permissions(manage_guild=True)
    async def _pause(self, ctx: commands.Context):
        """Pauses the currently playing song."""

        if ctx.voice_state.is_playing and ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='resume')
    @commands.has_permissions(manage_guild=True)
    async def _resume(self, ctx: commands.Context):
        """Resumes a currently paused song."""

        if ctx.voice_state.is_playing and ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='stop')
    @commands.has_permissions(manage_guild=True)
    async def _stop(self, ctx: commands.Context):
        """Stops playing song and clears the queue."""

        ctx.voice_state.songs.clear()

        if ctx.voice_state.is_playing:
            ctx.voice_state.voice.stop()
            await ctx.message.add_reaction('⏹')

    @commands.command(name='skip')
    async def _skip(self, ctx: commands.Context):
        """Vote to skip a song. The requester can automatically skip.
        3 skip votes are needed for the song to be skipped.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send('Not playing any music right now...')

        voter = ctx.message.author
        if voter == ctx.voice_state.current.requester:
            await ctx.message.add_reaction('⏭')
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.add(voter.id)
            total_votes = len(ctx.voice_state.skip_votes)

            if total_votes >= 3:
                await ctx.message.add_reaction('⏭')
                ctx.voice_state.skip()
            else:
                await ctx.send('Skip vote added, currently at **{}/3**'.format(total_votes))

        else:
            await ctx.send('You have already voted to skip this song.')

    @commands.command(name='queue')
    async def _queue(self, ctx: commands.Context, *, page: int = 1):
        """Shows the player's queue.
        You can optionally specify the page to show. Each page contains 10 elements.
        """

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(i + 1, song)

        embed = (discord.Embed(description='**{} tracks:**\n\n{}'.format(len(ctx.voice_state.songs), queue))
                 .set_footer(text='Viewing page {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)

    @commands.command(name='shuffle')
    async def _shuffle(self, ctx: commands.Context):
        """Shuffles the queue."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        ctx.voice_state.songs.shuffle()
        await ctx.message.add_reaction('✅')

    @commands.command(name='remove')
    async def _remove(self, ctx: commands.Context, index: int):
        """Removes a song from the queue at a given index."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction('✅')

    @commands.command(name='loop')
    async def _loop(self, ctx: commands.Context):
        """Loops the currently playing song.
        Invoke this command again to unloop the song.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send('Nothing being played at the moment.')

        # Inverse boolean value to loop and unloop.
        ctx.voice_state.loop = not ctx.voice_state.loop
        await ctx.message.add_reaction('✅')

    @commands.command(name='play', aliases=['p', 'pla'])
    async def _play(self, ctx: commands.Context, *, search: str):
        """Plays a song.
        If there are songs in the queue, this will be queued until the
        other songs finished playing.
        This command automatically searches from various sites if no URL is provided.
        A list of these sites can be found here: https://rg3.github.io/youtube-dl/supportedsites.html
        """

        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
            except YTDLError as e:
                await ctx.send('An error occurred while processing this request: {}'.format(str(e)))
            else:
                song = Song(source)

                await ctx.voice_state.songs.put(song)
                await ctx.send('Enqueued {}'.format(str(source)))

    @_join.before_invoke
    @_play.before_invoke
    async def ensure_voice_state(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError('You are not connected to any voice channel.')

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                raise commands.CommandError('Bot is already in a voice channel.')

def get_prefix(client, message):
    with open('prefixes.json', 'r') as f:
        prefixes = json.load(f)
    
    return prefixes[str(message.guild.id)]

bot = commands.Bot(command_prefix=get_prefix)
bot.remove_command("help")

@bot.event
async def on_guild_join(guild):
    with open('prefixes.json', 'r') as f:
        prefixes = json.load(f)

    prefixes[str(guild.id)] = '!'

    with open('prefixes.json', 'w') as f:
        json.dump(prefixes, f, indent =4)

@bot.event
async def on_guild_leave(guild):
    with open('prefixes.json', 'r') as f:
        prefixes = json.load(f)

    prefixes.pop(str(guild.id))

    with open('prefixes.json', 'w') as f:
        json.dump(prefixes, f, indent =4)

@bot.command()
async def prefix(ctx, prefix = None):
    if prefix is not None:
        with open('prefixes.json', 'r') as f:
            prefixes = json.load(f)

        prefixes[str(ctx.guild.id)] = prefix

        with open('prefixes.json', 'w') as f:
            json.dump(prefixes, f, indent =4)
        embed = discord.Embed(
            title = f"Prefix Has Been Set To {prefix}",
            description = f"Examples:\n```{prefix}help\n{prefix}prefix !```",
            colour = discord.Color.blurple()
        )
        await ctx.send(embed = embed)
    else:
        embed = discord.Embed(
            title = "Make Sure You Spelled Everything Correct and Didn't Forget An Argument",
            description = "Example:\n```!prefix j!```",
            colour = discord.Color.blurple()
        )
        await ctx.send(embed = embed)
    

@bot.command()
async def help(ctx, *, text = None):
    db = sqlite3.connect('enable.sqlite')
    cursor = db.cursor()
    cursor.execute(f"SELECT enable from main WHERE user_id = '{ctx.author.id}'")
    result = cursor.fetchone()
        #disable -----------------------
    if str(result[0]) == 'disable':
        if text is None:
            embed = discord.Embed(
            title = "Jims Help Page",
            description = "Prefix `!`"
            )
            embed.add_field(
            name = "Help Modules:",
            value = "`help music`\n`help user connections`\n`help moderation`\n`help settings`\n`help currency`\n\n[**Jims Server**](https://discord.gg/XSpVzTz) | [**Invite**](https://discord.com/api/oauth2/authorize?client_id=753395039840894986&permissions=8&scope=bot)"
            )
            await ctx.send(embed=embed)
        if text == 'user connections':
            embed = discord.Embed(
                title = "User Connections",
                description = "Commands:\n```!spotfiy @user - Displays The Users Spotify Status\n!streaming @user - Displays The Users Streaming Status\n!clone {msg} - Makes a clone of your self\n!wiki (query) - Searches Wikipedia\n!quote {message} - Create A Quote\n!emojify {text} - Emojifys The Text```\nFor More Help Type `!help user connections +`",
                colour = 0xF2F2F2
            )
            embed.set_footer(
                text = "To See Commands Coming Soon, Type !ccs"
            )
            await ctx.send(embed = embed)
        if text == 'music':
            embed = discord.Embed(
                title = "Music",
                description = "Commands:\n```!play {song name}\n!join - Joins Voice Channel\n!leave - Leaves Voice Channel\n!pause - Pauses Current Song\n!resume - Resumes Song [Only Works If Song Is Paused]\n!queue - Displays The Current Queue\n!loop - Loops Current Song\n!shuffle - Shuffles Queue\n!stop - Stops The Music and Gets Rid Of Queue\n!skip - Skips Current Song [Must Be Another Song In Queue]\n!volume {int} - Changes Volume\n!now - Displays The Current Song Being Played```\nFor More Help Type `!help music +`",
                colour = 0xF2F2F2
            )
            embed.set_footer(
                text = "To See Commands Coming Soon, Type !ccs"
            )
            await ctx.send(
            embed = embed
            )
        if text == 'moderation':
            embed = discord.Embed(
                title = "Moderation",
                description = "Commands:\n```!kick @user {reason} - Kicks The Specified User\n!ban @user {reason} - Bans The Specified User\n!nuke - Gets Rid Of Every Message In The Channel\n!history @user - Sends Every Message Sent From The Last 5 Hours By That User\n!clear {amount} - Clears the Specified Number Of Messages From The Channel```\nFor More Help Type `!help moderation +`"
            )
            embed.set_footer(
                text = "To See Commands Coming Soon, Type !ccs"
            )
            await ctx.send(embed=embed)
        if text == 'settings':
            embed = discord.Embed(
                title = "Settings",
                description = "Commands:\n```!dm enable | disable - disable/enable the bot DMing you the help page\n!preifx [prefix] - Sets Different Prefix```"
            )
            embed.set_footer(
                text = "To See Commands Coming Soon, Type !ccs"
            )
            await ctx.send(embed = embed)
        if text == 'moderation +':
            await ctx.send("More Help Coming Soon. For support, DM TowphY#6969")
        if text == 'currency':
            embed = discord.Embed(
                title = "Currency",
                description = "Commands:\n```!balance - Your Balance\n!work - Work for $\n!shop - Displays Market\n!!items - Displays Item Help\n!lottery - Test You Luck```"
            )
            await ctx.send(embed = embed)
        if text == 'music +':
            embed = discord.Embed(
                title = "More Music Help",
                description = "**Image Examples:**\n> !music image play\n> !music image join\n> !music image leave\n> !music image pause\n> !music image resume\n> !music image queue\n> !music image loop\n> !music image shuffle\n> !music image stop\n> !music image skip\n> !music image now"
            )
            await ctx.send(embed =embed)
            #enable ----------------------------
    if str(result[0]) == 'enable':
        if text is None:
            embed = discord.Embed(
                title = "Jims Help Page",
                description = "Prefix `!`"
            )
            embed.add_field(
            name = "Help Modules:",
            value = "`help music`\n`help user connections`\n`help moderation`\n`help settings`\n`help currency`\n\n[**Jims Server**](https://discord.gg/XSpVzTz) | [**Invite**](https://discord.com/api/oauth2/authorize?client_id=753395039840894986&permissions=8&scope=bot)"
            )
            await ctx.author.send(embed=embed)
            await ctx.message.add_reaction('☑️')
        if text == 'user connections':
            embed = discord.Embed(
                title = "User Connections",
                description = "Commands:\n```!spotfiy @user - Displays The Users Spotify Status\n!streaming @user - Displays The Users Streaming Status\n!clone {msg} - Makes a clone of your self\n!wiki (query) - Searches Wikipedia\n!quote {message} - Create A Quote\n!emojify {text} - Emojifys The Text```\nFor More Help Type `!help user connections +`",
                colour = 0xF2F2F2
            )
            embed.set_footer(
                text = "To See Commands Coming Soon, Type !ccs"
            )
            await ctx.author.send(embed = embed)
            await ctx.message.add_reaction('☑️')
        if text == 'music':
            embed = discord.Embed(
            title = "Music",
            description = "Commands:\n```!play {song name}\n!join - Joins Voice Channel\n!leave - Leaves Voice Channel\n!pause - Pauses Current Song\n!resume - Resumes Song [Only Works If Song Is Paused]\n!queue - Displays The Current Queue\n!loop - Loops Current Song\n!shuffle - Shuffles Queue\n!stop - Stops The Music and Gets Rid Of Queue\n!skip - Skips Current Song [Must Be Another Song In Queue]\n!volume {int} - Changes Volume\n!now - Displays The Current Song Being Played```\nFor More Help Type `!help music +`",
            colour = 0xF2F2F2
            )
            embed.set_footer(
            text = "To See Commands Coming Soon, Type !ccs"
            )
            await ctx.author.send(
                embed = embed
            )
            await ctx.message.add_reaction('☑️')
        if text == 'moderation':
            embed = discord.Embed(
                title = "Moderation",
                description = "Commands:\n```!kick @user {reason} - Kicks The Specified User\n!ban @user {reason} - Bans The Specified User\n!nuke - Gets Rid Of Every Message In The Channel\n!history @user - Sends Every Message Sent From The Last 5 Hours By That User\n!clear {amount} - Clears the Specified Number Of Messages From The Channel```\nFor More Help Type `!help moderation +`"
            )
            embed.set_footer(
                text = "To See Commands Coming Soon, Type !ccs"
            )
            await ctx.author.send(embed=embed)
            await ctx.message.add_reaction('☑️')
        if text == 'settings':
            embed = discord.Embed(
                title = "Settings",
                description = "Commands:\n```!dm enable | disable - disable/enable the bot DMing you the help page\n!preifx [prefix] - Sets Different Prefix```"
            )
            embed.set_footer(
                text = "To See Commands Coming Soon, Type !ccs"
            )
            await ctx.author.send(embed = embed)
            await ctx.message.add_reaction('☑️')
        if text == 'moderation +':
            await ctx.author.send("More Help Coming Soon. For support, DM TowphY#6969")
            await ctx.message.add_reaction('☑️')
        if text == 'currency':
            embed = discord.Embed(
                title = "Currency",
                description = "Commands:\n```!balance - Your Balance\n!work - Work for $\n!shop - Displays Market\n!!items - Displays Item Help\n!lottery - Test You Luck```"
            )
            await ctx.author.send(embed = embed)
            await ctx.message.add_reaction('☑️')
        if text == 'music +':
            embed = discord.Embed(
                title = "More Music Help",
                description = "**Image Examples:**\n> !music image play\n> !music image join\n> !music image leave\n> !music image pause\n> !music image resume\n> !music image queue\n> !music image loop\n> !music image shuffle\n> !music image stop\n> !music image skip\n> !music image now"
            )
            await ctx.author.send(embed =embed)
            await ctx.message.add_reaction('☑️')
    if str(result[0]) is None:
        if text is None:
            embed = discord.Embed(
            title = "Jims Help Page",
            description = "Prefix `!`"
            )
            embed.add_field(
            name = "Help Modules:",
            value = "`help music`\n`help user connections`\n`help moderation`\n`help settings`\n`help currency`\n\n[**Jims Server**](https://discord.gg/XSpVzTz) | [**Invite**](https://discord.com/api/oauth2/authorize?client_id=753395039840894986&permissions=8&scope=bot)"
            )
            await ctx.send(embed=embed)
        if text == 'user connections':
            embed = discord.Embed(
                title = "User Connections",
                description = "Commands:\n```!spotfiy @user - Displays The Users Spotify Status\n!streaming @user - Displays The Users Streaming Status\n!clone {msg} - Makes a clone of your self\n!wiki (query) - Searches Wikipedia\n!quote {message} - Create A Quote\n!emojify {text} - Emojifys The Text```\nFor More Help Type `!help user connections +`",
                colour = 0xF2F2F2
            )
            embed.set_footer(
                text = "To See Commands Coming Soon, Type !ccs"
            )
            await ctx.send(embed = embed)
        if text == 'music':
            embed = discord.Embed(
                title = "Music",
                description = "Commands:\n```!play {song name}\n!join - Joins Voice Channel\n!leave - Leaves Voice Channel\n!pause - Pauses Current Song\n!resume - Resumes Song [Only Works If Song Is Paused]\n!queue - Displays The Current Queue\n!loop - Loops Current Song\n!shuffle - Shuffles Queue\n!stop - Stops The Music and Gets Rid Of Queue\n!skip - Skips Current Song [Must Be Another Song In Queue]\n!volume {int} - Changes Volume\n!now - Displays The Current Song Being Played```\nFor More Help Type `!help music +`",
                colour = 0xF2F2F2
            )
            embed.set_footer(
                text = "To See Commands Coming Soon, Type !ccs"
            )
            await ctx.send(
            embed = embed
            )
        if text == 'moderation':
            embed = discord.Embed(
                title = "Moderation",
                description = "Commands:\n```!kick @user {reason} - Kicks The Specified User\n!ban @user {reason} - Bans The Specified User\n!nuke - Gets Rid Of Every Message In The Channel\n!history @user - Sends Every Message Sent From The Last 5 Hours By That User\n!clear {amount} - Clears the Specified Number Of Messages From The Channel```\nFor More Help Type `!help moderation +`"
            )
            embed.set_footer(
                text = "To See Commands Coming Soon, Type !ccs"
            )
            await ctx.send(embed=embed)
        if text == 'settings':
            embed = discord.Embed(
                title = "Settings",
                description = "Commands:\n```!dm enable | disable - disable/enable the bot DMing you the help page\n!preifx [prefix] - Sets Different Prefix```"
            )
            embed.set_footer(
                text = "To See Commands Coming Soon, Type !ccs"
            )
            await ctx.send(embed = embed)
        if text == 'moderation +':
            await ctx.send("More Help Coming Soon. For support, DM TowphY#6969")
        if text == 'currency':
            embed = discord.Embed(
                title = "Currency",
                description = "Commands:\n```!balance - Your Balance\n!work - Work for $\n!shop - Displays Market\n!!items - Displays Item Help\n!lottery - Test You Luck```"
            )
            await ctx.send(embed = embed)
        if text == 'music +':
            embed = discord.Embed(
                title = "More Music Help",
                description = "**Image Examples:**\n> !music image play\n> !music image join\n> !music image leave\n> !music image pause\n> !music image resume\n> !music image queue\n> !music image loop\n> !music image shuffle\n> !music image stop\n> !music image skip\n> !music image now"
            )
            await ctx.send(embed =embed)

@bot.command()
async def quote(ctx, *, msg):
    await ctx.channel.purge(limit=1)
    await ctx.send(f"`{msg}`\n\n~ {ctx.author.name}")

@bot.command()
async def ccs(ctx):
    await ctx.send('Biggest Jim Update Coming 10/1/2020')

@bot.command()
async def music(ctx, *, text):
    if text == 'image play':
        embed = discord.Embed(title = "!play Image")
        embed.set_image(url = "https://cdn.discordapp.com/attachments/754619738096795668/754765345109311519/unknown.png")
        await ctx.send(embed = embed)
    if text == 'image join':
        embed = discord.Embed(title = "!join Image")
        embed.set_image(url = "https://cdn.discordapp.com/attachments/754619738096795668/754767664827727882/unknown.png")
        await ctx.send(embed = embed)
    if text == 'image leave':
        embed = discord.Embed(title = "!leave Image")
        embed.set_image(url = "https://cdn.discordapp.com/attachments/754619738096795668/754769023836880926/unknown.png")
        await ctx.send(embed = embed)
    if text == 'image pause':
        embed = discord.Embed(title = "!pause Image")
        embed.set_image(url = "https://cdn.discordapp.com/attachments/754619738096795668/754769349658542151/unknown.png")
        await ctx.send(embed = embed)
    if text == 'image resume':
        embed = discord.Embed(title = "!resume Image")
        embed.set_image(url = 'https://cdn.discordapp.com/attachments/754619738096795668/754769843755942098/unknown.png')
        await ctx.send(embed = embed)
    if text == 'image resume':
        embed = discord.Embed(title = "!queue Image")
        embed.set_image(url = 'https://cdn.discordapp.com/attachments/754619738096795668/754771178899505212/unknown.png')
        await ctx.send(embed = embed)
    if text == 'image loop':
        embed = discord.Embed(title = "!loop Image")
        embed.set_image(url = 'https://cdn.discordapp.com/attachments/754619738096795668/754771476711866532/unknown.png')
        await ctx.send(embed = embed)
    if text == 'image shuffle':
        embed = discord.Embed(title = "!shuffle Image")
        embed.set_image(url = 'https://cdn.discordapp.com/attachments/754619738096795668/754791040896073728/unknown.png')
        await ctx.send(embed = embed)
    if text == 'image skip':
        embed = discord.Embed(title = "!skip Image")
        embed.set_image(url = 'https://cdn.discordapp.com/attachments/754619738096795668/754792109948797048/unknown.png')
        await ctx.send(embed = embed)
    if text == 'image now':
        embed = discord.Embed(title = "!skip Image")
        embed.set_image(url = 'https://cdn.discordapp.com/attachments/754619738096795668/754792482574827589/unknown.png')
        await ctx.send(embed = embed)

@bot.command(aliases=['s'])
async def spotify(ctx, user: discord.Member=None):
    user = user or ctx.author
    for activity in user.activities:
        if isinstance(activity, Spotify):
            embed = discord.Embed(title = f"{user.name} Is Listening To Spotify", description = f"<:spotify_music:753599315959611412> Song: **{activity.title}**\n<:spotify_artist:753598825645604955> Artist: **{activity.artist}**\n<:spotify_album:753598825817440397> Album: **{activity.album}**\n<:spotify_trackid:753598825670770758> Track ID: **{activity.track_id}**", colour = activity.color)
            embed.set_thumbnail(url=activity.album_cover_url)
            embed.set_footer(text = f"Party Id - {activity.party_id} | requested by {ctx.author.name}")
            await ctx.send(embed = embed)

@bot.command()
async def streaming(ctx, user: discord.Member=None):
    user = user or ctx.author
    for activity in user.activities:
        if isinstance(activity, Streaming):
            embed = discord.Embed(title = f"{user.name} Is Streaming", description = f"Platform: **{activity.platform}**\nName: **{activity.details}**\nUrl: [**Click Here**]({activity.url})\nGame: **{activity.game}**", colour = 0xC45FF9)
            await ctx.send(embed = embed)

@bot.event
async def statuschange():
    await bot.wait_until_ready()
    while True:
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.playing,name="!help"),status=discord.Status.dnd)
        await asyncio.sleep(0.5)
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.playing,name="!help"),status=discord.Status.idle)
        await asyncio.sleep(0.5)
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.playing,name="!help"),status=discord.Status.online)
        await asyncio.sleep(0.5)

@bot.event
async def on_ready():
    db = sqlite3.connect('cart.sqlite')
    cursor = db.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS main(
      user_id TEXT,
      enable TEXT
    )
    ''')
    print("Ahhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhh")
    await statuschange()
    bot.loop.create_task(statuschange())
 
@bot.event
async def on_message(message):
    if not message.author.bot: #if user
        file_object = open(f'{message.author.name}{message.author.guild.id}.txt', 'a')
 
    # Append 'hello' at the end of file
        file_object.write("{} {}: {}\n".format(message.created_at, message.author.name, message.content))
 
    # Close the file
        file_object.close()
        await bot.process_commands(message)
        await asyncio.sleep(3600)
        os.remove(f'{message.author.name}{message.author.guild.id}.txt')
    else:
        print('Tried But Failed Creating A File')

@bot.command()
async def history(ctx, member:discord.User=None):
    guild = ctx.author.guild
    if member is not None:
        with open(f"{member.name}{member.guild.id}.txt", "rb") as file:
            await ctx.send(f"Want another way of seeing the history? `type !more history @user`\n\n{member.mention} History File:", file=discord.File(file, "result.txt"))
            

    if member is None:
        embed = discord.Embed(
            title = "An Error Occurred:",
            description = "Please Specify A User.\nExample:\n```!history @username```",
            colour = discord.Color.blurple()
        )
        await ctx.send(embed = embed)


@bot.command()
async def more(ctx, text = None, member:discord.User=None):
    if member is not None:
        if text == 'history':
            if os.path.exists(f'{ctx.author.name}.txt'):
                lines = open(f'{ctx.author.name}.txt', encoding='utf-8').read().splitlines()
                text = lines
                await ctx.send(text)

@bot.command()
async def dm(ctx, text):
    db = sqlite3.connect('enable.sqlite')
    cursor = db.cursor()
    cursor.execute(f"SELECT enable FROM main WHERE user_id = {ctx.author.id}")
    result = cursor.fetchone()
    if text == 'enable':
        if result is None:
            sql = ("INSERT INTO main(user_id, enable) VALUES(?,?)")
            val = (ctx.author.id, text)
            embed=discord.Embed(description=f'DM Help Status Has Been Set To {text}', colour = discord.Color.blurple())
            await ctx.send(embed=embed)
        elif result is not None:
            sql = ("UPDATE main SET enable = ? WHERE user_id = ?")
            val = (text, ctx.author.id)
            embed=discord.Embed(description=f'DM Help Status Has Been Updated To {text}', colour = discord.Color.blurple())
            await ctx.send(embed=embed)
        cursor.execute(sql, val)
        db.commit()
        cursor.close()
        db.close()
    if text == 'disable':
        if result is None:
            sql = ("INSERT INTO main(user_id, enable) VALUES(?,?)")
            val = (ctx.author.id, text)
            embed=discord.Embed(description=f'DM Help Status Has Been Set To {text}', colour = discord.Color.blurple())
            await ctx.send(embed=embed)
        elif result is not None:
            sql = ("UPDATE main SET enable = ? WHERE user_id = ?")
            val = (text, ctx.author.id)
            embed=discord.Embed(description=f'DM Help Status Has Been Updated To {text}', colour = discord.Color.blurple())
            await ctx.send(embed=embed)
        cursor.execute(sql, val)
        db.commit()
        cursor.close()
        db.close()
    else:
        return

@bot.command()
async def ban (ctx, member:discord.User=None, reason =None):
        if ctx.author.guild_permissions.ban_members:
            if member == None or member == ctx.message.author:
                await ctx.channel.send("You cannot ban yourself")
                return
            if reason == None:
                reason = "For being a jerk!"
            message = f"You have been banned from {ctx.guild.name} for {reason}"
            await member.send(message)
            # await ctx.guild.ban(member, reason=reason)
            embed = discord.Embed(
                title = "The Ban Hammer Has Spoken!",
                description = f"Victum: **{member.name}**\nReason: **{reason}**\nBanned By: **{ctx.author.mention}**"
            )
            await ctx.channel.send(embed = embed)
            await ctx.guild.ban(member)

@bot.command()
async def kick (ctx, member:discord.User=None, *, reason =None):
    if ctx.author.guild_permissions.kick_members:
        if member == None or member == ctx.message.author:
            await ctx.channel.send("You cannot ban yourself")
            return
        if reason == None:
            reason = "For being a jerk!"
        message = f"You have been banned from {ctx.guild.name} for {reason}"
        await member.send(message)
        # await ctx.guild.ban(member, reason=reason)
        embed = discord.Embed(
            title = "The Boot Has Spoken",
            description = f"Victum: **{member.name}**\nReason: **{reason}**\nKicked By: **{ctx.author.mention}**"
        )
        await ctx.channel.send(embed = embed)
        await ctx.guild.kick(member)

@bot.command(name='unban')
@guild_only()  # Might not need ()
async def _unban(ctx, id: int):
    if ctx.author.guild_permissions.ban_members:
        user = await bot.fetch_user(id)
        await ctx.guild.unban(user)
        embed = discord.Embed(
            title = "Unbaned User",
            description = f"User ID: {id}\nUnbanned By {ctx.author.mention}"
        )
        await ctx.send(embed = embed)

@bot.command()
async def nuke(ctx, member:discord.User = None):
    if member is None:
        if ctx.author.guild_permissions.manage_messages:
            await ctx.channel.purge(limit=10000000000000000)
            embed = discord.Embed(
                title =  "Channel Nuked"
            )
            embed.set_image(url = 'https://1.bp.blogspot.com/-YHlG8bdgXiY/VqgUws2TrFI/AAAAAAAAKW4/6mlICLU03ZU/s1600/nuke%2Bmushroom%2Bcloud.gif')
            msg = await ctx.send(embed = embed)
            await asyncio.sleep(7)
            await msg.delete()
        else:
            await ctx.send("You Do Not have the required perms")
    if member is not None:
            db = sqlite3.connect('currency.sqlite')
            cursor = db.cursor()
            cursor.execute(f"SELECT money, nukes from main WHERE user_id = '{ctx.author.id}'")
            result = cursor.fetchone()
            money = int(result[0])
            nukes = int(result[1])
            if nukes < 1:
                    embed = discord.Embed(
                    title = "You Don't Have Any Nukes!",
                    colour = discord.Color.blurple()
                     )
                    await ctx.send(embed = embed)
            else:
                sql = ("UPDATE main SET nukes = ? WHERE user_id = ?")
                val = (nukes - 1, str(ctx.author.id))
                cursor.execute(sql, val)
                db.commit()
                sql2 = ("UPDATE main SET money = ? WHERE user_id = ?")
                val2 = (money - 50, str(member.id))
                cursor.execute(sql2, val2)
                db.commit()
                embed = discord.Embed(
                title = f'{ctx.author.name} Just Nuked 50 Coins From {member.name} Balance',
                description = 'rip',
                colour = discord.Color.blurple()
                )
                await ctx.send(embed = embed)


@bot.command()
async def clear(ctx, amount = 5):
    if ctx.author.guild_permissions.manage_messages:
        await ctx.channel.purge(limit=amount)

@bot.command(helpinfo='For when plain text just is not enough')
async def emojify(ctx, *, text: str):
    '''
    Converts the alphabet and spaces into emoji
    '''
    author = ctx.message.author
    emojified = '   '
    formatted = re.sub(r'[^A-Za-z ]+', "", text).lower()
    if text == '':
        await ctx.send('Remember to say what you want to convert!')
    else:
        for i in formatted:
            if i == ' ':
                emojified += '     '
            else:
                emojified += ':regional_indicator_{}: '.format(i)
        if len(emojified) + 2 >= 2000:
            await ctx.send('Your message in emojis exceeds 2000 characters!')
        if len(emojified) <= 25:
            await ctx.send('Your message could not be converted!')
        else:
            await ctx.send(''+emojified+'')

@bot.command(helpinfo='Info about servers ShrekBot is in', aliases=['server', 'num', 'count'])
@commands.cooldown(1, 10, commands.BucketType.user)
async def servers(ctx):
    '''
    Info about servers ShrekBot is in
    '''
    servers = bot.guilds
    servers.sort(key=lambda x: x.member_count, reverse=True)
    await ctx.send("Top 10 Servers With Jim")
    for x in servers[:10]:
        await ctx.send("**{}** | **{}** Members | {} region | Owned by <@{}> | Created at {}".format(x.name, x.member_count, x.region, x.owner_id, x.created_at, x.icon_url_as(format='png',size=32)))
    y = 0
    for x in bot.guilds:
        y += x.member_count
    await ctx.send("**Count:**\n**Jim Users:** **{}** | **Server Count:** | **{}**".format(y, len(bot.guilds)))

@bot.command(helpinfo='Clone your words - like echo')
async def clone(ctx, *, message = None):
    if message is not None:
        pfp = requests.get(ctx.author.avatar_url_as(format='png', size=256)).content
        hook = await ctx.channel.create_webhook(name=ctx.author.display_name,
                                            avatar=pfp)

        await ctx.channel.purge(limit=1)
        await hook.send(message)
        await hook.delete()
    if message is None:
        embed = discord.Embed(
            title = "Please Provide A Message.",
            description = "Example:\n```!clone Hello World```",
            colour = discord.Color.blurple()
        )
        await ctx.send(embed = embed)

@bot.command(helpinfo='Wikipedia summary', aliases=['w', 'wikipedia'])
async def wiki(ctx, *, query: str):
    '''
    Uses Wikipedia APIs to summarise search
    '''
    sea = requests.get(
        ('https://en.wikipedia.org//w/api.php?action=query'
         '&format=json&list=search&utf8=1&srsearch={}&srlimit=5&srprop='
        ).format(query)).json()['query']

    if sea['searchinfo']['totalhits'] == 0:
        await ctx.send('Sorry, your search could not be found.')
    else:
        for x in range(len(sea['search'])):
            article = sea['search'][x]['title']
            req = requests.get('https://en.wikipedia.org//w/api.php?action=query'
                               '&utf8=1&redirects&format=json&prop=info|images'
                               '&inprop=url&titles={}'.format(article)).json()['query']['pages']
            if str(list(req)[0]) != "-1":
                break
        else:
            await ctx.send('Sorry, your search could not be found.')
            return
        article = req[list(req)[0]]['title']
        arturl = req[list(req)[0]]['fullurl']
        artdesc = requests.get('https://en.wikipedia.org/api/rest_v1/page/summary/'+article).json()['extract']
        lastedited = datetime.datetime.strptime(req[list(req)[0]]['touched'], "%Y-%m-%dT%H:%M:%SZ")
        embed = discord.Embed(title='**'+article+'**', url=arturl, description=artdesc, color=0x3FCAFF)
        embed.set_footer(text='Wiki entry last modified',
                         icon_url='https://upload.wikimedia.org/wikipedia/commons/6/63/Wikipedia-logo.png')
        embed.set_author(name='Wikipedia', url='https://en.wikipedia.org/',
                         icon_url='https://upload.wikimedia.org/wikipedia/commons/6/63/Wikipedia-logo.png')
        embed.timestamp = lastedited
        await ctx.send('**Search result for:** ***"{}"***:'.format(query), embed=embed)

@bot.command()
async def poll(ctx, channel:discord.TextChannel, *, poll : str):
    embed = discord.Embed(
        title = "A Poll Has Started",
        description = f"{poll}",
        colour  = 0xFC5454
    )
    embed.set_footer(
        text = f"Sent By {ctx.author.name}"
    )
    react = await channel.send(embed = embed)
    await react.add_reaction('👍')
    await react.add_reaction('👎')
    embed3 = discord.Embed(
        title = f"Poll Sent To {channel} 👍",
        description = f" [__**Message Link**__](https://discordapp.com/channels/{ctx.author.guild.id}/{channel.id}/{react.id})",
        colour  = 0xFC5454
    )
    await ctx.send(embed = embed3)

@bot.command()
@commands.cooldown(1, 40, commands.BucketType.user)
async def work(ctx):
        mc = ['3', '4', '5', '6', '7', '8', '8', '9', '9', '9', '10', '10', '11', '12']
        m = random.choice(mc)
        mcs = ['12', '13', '14', '15', '16', '16', '17', '18', '19', '19', '18', '20', '20', '20']
        ms = random.choice(mcs)
        jobs = ['Invented A Life Changing Tooth Brush', 'Surgery On A Grape', 'Milked A Thick Cow',
                'Invented Something New', 'Robbed Jims Neighbor', 'Played Poker With The Mafia And Won',
                'Worked Out', 'Cleaned Your Neighbors Gutters', 'Published A New Book',
                'Mowed Some Grass For A Friend', 'Released A Banger Song',
                'Made Some Stonks', 'Filmed A Movie',]
        job = random.choice(jobs)
        db = sqlite3.connect('currency.sqlite')
        cursor = db.cursor()
        cursor.execute(f"SELECT money from main WHERE user_id = '{ctx.author.id}'")
        result = cursor.fetchone()
        cursor.execute(f"SELECT user_id, money, enable from main WHERE user_id = '{ctx.author.id}'")
        result2 = cursor.fetchone()
        money = int(result2[1])
        nukes = str(result2[2])
        if nukes == str('enable'):
            if result is None:
                sql = ("INSERT INTO main(user_id, money) VALUES(?,?)")
                val = (ctx.author.id, ms)
                cursor.execute(sql, val)
                db.commit()
                embed = discord.Embed(
                    description = f'Job: **{job}**\nWhat You Earned: **${ms}**\n\n`Item In Use: Shopping Cart`'
                )
                embed.set_footer(
                    text = 'You Can Not Use This Command For Another 40s'
                )
                await ctx.send(embed = embed)
            else:
                cursor.execute(f"SELECT user_id, money from main WHERE user_id = '{ctx.author.id}'")
                result1 = cursor.fetchone()
                money = int(result1[1])
                sql = ("UPDATE main SET money = ? WHERE user_id = ?")
                val = (money + int(ms), str(ctx.author.id))
                cursor.execute(sql, val)
                db.commit()
                embed = discord.Embed(
              description = f'Job: **{job}**\n\nWhat You Earned: **${ms}**\n\n`Item In Use: Shopping Cart`'
                )
                embed.set_footer(
              text = 'You Can Not Use This Command For Another 40s'
                )
                await ctx.send(embed = embed)
        else:
            if result is None:
                sql = ("INSERT INTO main(user_id, money) VALUES(?,?)")
                val = (ctx.author.id, m)
                cursor.execute(sql, val)
                db.commit()
                embed = discord.Embed(
                description = f'Job: **{job}**\nWhat You Earned: **${m}**\n\n'
                )
                embed.set_footer(
                text = 'You Can Not Use This Command For Another 40s'
                )
                await ctx.send(embed = embed)
            else:
                cursor.execute(f"SELECT user_id, money from main WHERE user_id = '{ctx.author.id}'")
                result1 = cursor.fetchone()
                money = int(result1[1])
                sql = ("UPDATE main SET money = ? WHERE user_id = ?")
                val = (money + int(m), str(ctx.author.id))
                cursor.execute(sql, val)
                db.commit()
                embed = discord.Embed(
                    description = f'Job: **{job}**\n\nWhat You Earned: **${m}**'
                )
                embed.set_footer(
                text = 'You Can Not Use This Command For Another 40s'
                )
                await ctx.send(embed = embed)

@bot.command()
async def daddysMilk(ctx):
            db = sqlite3.connect('currency.sqlite')
            cursor = db.cursor()
            cursor.execute(f"SELECT money, daddy from main WHERE user_id = '{ctx.author.id}'")
            result = cursor.fetchone()
            money = int(result[0])
            nukes = int(result[1])
            if nukes < 1:
                    embed = discord.Embed(
                    title = "You Don't Have And Daddys Milk!",
                    colour = discord.Color.blurple()
                     )
                    await ctx.send(embed = embed)
            else:
                sql = ("UPDATE main SET money = ?, daddy = ? WHERE user_id = ?")
                val = (money - 2000, nukes - 1, str(ctx.author.id))
                cursor.execute(sql, val)
                db.commit()
                lottery = ['500', '500', '1000', '1000', '1010', '1020', '1030', '200', '1040', '1040', '1050', '1020', '2010', '2400', '1900', '2800', '2050', '2050', '2000', '2000', '2900', '2090', '2202', '2222', '2100', '2010', '2500', '3000', '3500', ]
                pick = random.choice(lottery)
                db = sqlite3.connect('currency.sqlite')
                cursor = db.cursor()
                cursor.execute(f"SELECT money from main WHERE user_id = '{ctx.author.id}'")
                result = cursor.fetchone()
                if result is None:
                    sql = ("INSERT INTO main(user_id, money) VALUES(?,?)")
                    val = (ctx.author.id, lottery)
                    cursor.execute(sql, val)
                    db.commit()
                    embed = discord.Embed(
                    description = f'🎰 You Tested Your Chances With The Lottery:\n\nYou Won: **${pick}**',
                    colour = 0xCBFFBD
                    )
                    embed.set_footer(
                    text = 'You Can Not Use This Command For Another 3 Hours'
                    )
                    await ctx.send(embed = embed)
                else:
                    cursor.execute(f"SELECT user_id, money from main WHERE user_id = '{ctx.author.id}'")
                    result1 = cursor.fetchone()
                    money = int(result1[1])
                    sql = ("UPDATE main SET money = ? WHERE user_id = ?")
                    val = (money + int(pick), str(ctx.author.id))
                    cursor.execute(sql, val)
                    db.commit()
                    embed = discord.Embed(
                    description = f'🎰 You Tested Your Chances With The Lottery:\n\nYou Won: **${pick}**',
                    colour = 0xCBFFBD
                    )
                    embed.set_footer(
                    text = 'You Can Not Use This Command For Another 1 Hours'
                    )
                    await ctx.send(embed = embed)
                

@bot.command()
async def balance(ctx):
        db = sqlite3.connect('currency.sqlite')
        cursor = db.cursor()
        cursor.execute(f"SELECT user_id, money, nukes, cart, daddy, jimscoin from main WHERE user_id = '{ctx.author.id}'")
        result = cursor.fetchone()
        if result is not None:
            embed = discord.Embed(
            title = f"{ctx.author.name}'s Balance",
            description =f'Cash: **{(result[1])}**\n\nNukes: **{(result[2])}**\n\nShopping Carts: **{(result[3])}**\n\nDaddys Milk: **{(result[4])}**\n\nJim Coins: **{(result[5])}**',
            colour  = 0xBDD0FF
            ) 
            await ctx.send(embed=embed)
        if result is None:
            ctx.send('You Do Not Have Any Coins! Type `-work` To Earn Some!')

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        embed = discord.Embed(
          title = 'Command Is On A Cooldown',
          description = 'Try Again In %.2f Seconds' % error.retry_after
        )
        await ctx.send(embed = embed)
    raise error  

@bot.command()
async def bal(ctx):
        db = sqlite3.connect('currency.sqlite')
        cursor = db.cursor()
        cursor.execute(f"SELECT user_id, money, nukes, cart, daddy, jimscoin from main WHERE user_id = '{ctx.author.id}'")
        result = cursor.fetchone()
        if result is not None:
            embed = discord.Embed(
            title = f"{ctx.author.name}'s Balance",
            description = f'Cash: **{(result[1])}**\n\nNukes: **{(result[2])}**\n\nShopping Carts: **{(result[3])}**\n\nDaddys Milk: **{(result[4])}**\n\nJim Coins: **{(result[5])}**',
            colour  = 0xBDD0FF
            ) 
            await ctx.send(embed=embed)
        if result is None:
            ctx.send('You Do Not Have Any Coins! Type `-work` To Earn Some!')

@bot.command()
@commands.cooldown(1, 10800, commands.BucketType.user)
async def lottery(ctx):
        lottery = ['2', '1', '2',  '3', '3', '4', '4', '4', '5','6', '6', '6',  '6', '6',
                   '6', '7', '7', '7','7', '7', '8',  '8', '8', '9', '9', '9', '10','10', '10',
                   '11',  '12', '13', '14', '15', '16', '17','18', '19', '20',  '21', '22', '23',
                   '24', '25', '26','27', '28', '29',  '30']
        pick = random.choice(lottery)
        db = sqlite3.connect('currency.sqlite')
        cursor = db.cursor()
        cursor.execute(f"SELECT money from main WHERE user_id = '{ctx.author.id}'")
        result = cursor.fetchone()
        if result is None:
            sql = ("INSERT INTO main(user_id, money) VALUES(?,?)")
            val = (ctx.author.id, lottery)
            cursor.execute(sql, val)
            db.commit()
            embed = discord.Embed(
              description = f'🎰 You Tested Your Chances With The Lottery:\n\nYou Won: **${pick}**',
              colour = 0xCBFFBD
            )
            embed.set_footer(
              text = 'You Can Not Use This Command For Another 3 Hours'
            )
            await ctx.send(embed = embed)
        else:
            cursor.execute(f"SELECT user_id, money from main WHERE user_id = '{ctx.author.id}'")
            result1 = cursor.fetchone()
            money = int(result1[1])
            sql = ("UPDATE main SET money = ? WHERE user_id = ?")
            val = (money + int(pick), str(ctx.author.id))
            cursor.execute(sql, val)
            db.commit()
            embed = discord.Embed(
              description = f'🎰 You Tested Your Chances With The Lottery:\n\nYou Won: **${pick}**',
              colour = 0xCBFFBD
            )
            embed.set_footer(
              text = 'You Can Not Use This Command For Another 3 Hours'
            )
            await ctx.send(embed = embed)

@bot.command()
async def shop(ctx):
  embed = discord.Embed(
    title = 'Jims basement market',
    description = '**Nuke: $100**\n*Item: `!items nuke`*\n\n\n**Shopping Cart:** **$350**\n*x2 Money for an 30 Minutes \n`!items shoppingCart`*\n\n\n**Daddys Milk:** **$2000**\n*OP Lottery `!items daddysMilk`*\n\n\n**Jim Coin:** **$20,000**\n*Collectable `!items jimCoin`*'
  )
  embed.set_footer(
    text = 'To Purchase Type !purchase {item}'
  )
  await ctx.send(embed=embed)

@bot.command()
async def purchase(ctx, *, text = None):
    db = sqlite3.connect('currency.sqlite')
    cursor = db.cursor()
    cursor.execute(f"SELECT money from main WHERE user_id = '{ctx.author.id}'")
    result = cursor.fetchone()
    if text == 'nuke':
            cursor.execute(f"SELECT user_id, money, nukes from main WHERE user_id = '{ctx.author.id}'")
            result1 = cursor.fetchone()
            money = int(result1[1])
            nukes = int(result1[2])
            if money < 350:
                    embed = discord.Embed(
                    title = 'You Do Not Have Enough $',
                    colour = discord.Color.blurple()
                     )
                    await ctx.send(embed = embed)
            else:
                sql = ("UPDATE main SET money = ?, nukes = ? WHERE user_id = ?")
                val = (money - 100,  nukes + 1, str(ctx.author.id))
                cursor.execute(sql, val)
                db.commit()
                embed = discord.Embed(
                title = 'Thanks For Purchasing A Nuke!',
                description = 'Check Your New Balance With `!bal`\n```type !nuke @user To Delete 20 Coins From A User```',
                colour = discord.Color.blurple()
                )
                await ctx.send(embed = embed)
    if text == 'shopping cart':
        if result is None:
            embed = discord.Embed(
              title = 'You Do Not Have Any $',
              colour = discord.Color.blurple()
            )
            await ctx.send(embed = embed)
        else:
            
                cursor.execute(f"SELECT user_id, money, cart from main WHERE user_id = '{ctx.author.id}'")
                result1 = cursor.fetchone()
                money = int(result1[1])
                nukes = int(result1[2])
                if money < 350:
                    embed = discord.Embed(
                    title = 'You Do Not Have Enough $',
                    colour = discord.Color.blurple()
                     )
                    await ctx.send(embed = embed)
                else:
                    sql = ("UPDATE main SET money = ?, cart = ? WHERE user_id = ?")
                    val = (money - 350,  nukes + 1, str(ctx.author.id))
                    cursor.execute(sql, val)
                    db.commit()
                    embed = discord.Embed(
                    title = 'Thanks For Purchasing A Shopping Cart!',
                    description = 'Check Your New Balance With `!bal`\n```type !shoppingCart To Get x2 Coins For 30 Minutes```',
                    colour = discord.Color.blurple()
                    )
                    await ctx.send(embed = embed)
    if text == 'daddys milk':
        if result is None:
            embed = discord.Embed(
              title = 'You Do Not Have Any $',
              colour = discord.Color.blurple()
            )
            await ctx.send(embed = embed)
        else:
            cursor.execute(f"SELECT user_id, money, daddy from main WHERE user_id = '{ctx.author.id}'")
            result1 = cursor.fetchone()
            money = int(result1[1])
            nukes = int(result1[2])
            if money < 2000:
                embed = discord.Embed(
              title = 'You Do Not Have Enough $',
              colour = discord.Color.blurple()
                )
                await ctx.send(embed = embed)
            else:
                sql = ("UPDATE main SET money = ?, daddy = ? WHERE user_id = ?")
                val = (money - 2000,  nukes + 1, str(ctx.author.id))
                cursor.execute(sql, val)
                db.commit()
                embed = discord.Embed(
                title = 'Thanks For Purchasing Daddys Milk!',
                description = 'Check Your New Balance With `!bal`\n```type !daddysMilk To Get A OP Lottery```',
                colour = discord.Color.blurple()
                )
                await ctx.send(embed = embed)
    if text == 'jim coin':
        if result is None:
            embed = discord.Embed(
              title = 'You Do Not Have Any $',
              colour = discord.Color.blurple()
            )
            await ctx.send(embed = embed)
        else:
            cursor.execute(f"SELECT user_id, money, jimscoin from main WHERE user_id = '{ctx.author.id}'")
            result1 = cursor.fetchone()
            money = int(result1[1])
            nukes = int(result1[2])
            if money < 20000:
                embed = discord.Embed(
              title = 'You Do Not Have Enough $',
              colour = discord.Color.blurple()
                )
                await ctx.send(embed = embed)
            else:
                sql = ("UPDATE main SET money = ?, jimscoin = ? WHERE user_id = ?")
                val = (money - 20000,  nukes + 1, str(ctx.author.id))
                cursor.execute(sql, val)
                db.commit()
                embed = discord.Embed(
                title = 'Thanks For Purchasing A JimCoin!',
                description = 'Check Your New Balance With `!bal`\n```type !jimCoin to get secret perks Join Here => https://discord.gg/HPaRjEJ```',
                colour = discord.Color.blurple()
                )
                await ctx.send(embed = embed)
    else: 
        embed = discord.Embed(
              title = "Make Sure You Typed Everything Correctly or Didn't Forget And Argument",
              description = "Example:\n```!purchase nuke\n!purchase shopping cart\n!purchase daddys milk\n!purchase jim coin```",
              colour = discord.Color.blurple()
                )
        await ctx.send(embed = embed)

@bot.command()
async def shoppingCart(ctx):
    db = sqlite3.connect('currency.sqlite')
    cursor = db.cursor()
    cursor.execute(f"SELECT enable FROM main WHERE user_id = {ctx.author.id}")
    result = cursor.fetchone()

    if result is None:
            embed = discord.Embed(
              title = 'You Do Not Have Any Shopping Carts',
              colour = discord.Color.blurple()
            )
            await ctx.send(embed = embed)
    else:
            
                cursor.execute(f"SELECT user_id, cart, enable from main WHERE user_id = '{ctx.author.id}'")
                result1 = cursor.fetchone()
                money = int(result1[1])
                if money < 1:
                    embed = discord.Embed(
                    title = 'You Do Not Have Any Shopping Carts',
                    colour = discord.Color.blurple()
                     )
                    await ctx.send(embed = embed)
                else:
                    sql = ("UPDATE main SET cart = ?, enable = ? WHERE user_id = ?")
                    val = (money - 1,  str('enable'), str(ctx.author.id))
                    cursor.execute(sql, val)
                    db.commit()
                    embed = discord.Embed(
                    title = 'Shopping Cart Enabled',
                    description = 'You Know How 2x Money\nExample\n```!work```',
                    colour = discord.Color.blurple()
                    )
                    await ctx.send(embed = embed)
                    await asyncio.sleep(1800)
                    sql = ("UPDATE main SET enable = ? WHERE user_id = ?")
                    val = (str('    '), str(ctx.author.id))
                    cursor.execute(sql, val)
                    db.commit()
                    embed = discord.Embed(
                    title = 'Shopping Cart Disabled',
                    description = 'You Now Longer Have 2x Money',
                    colour = discord.Color.blurple()
                    )
                    await ctx.send(embed = embed)
        
@bot.command()
async def items(ctx, *, text = None):
    if text is None:
        embed = discord.Embed(
            title = "Items Info Page:",
            description = "**Nuke:**\n\nUse: `!nuke @user`\nInfo: `Takes Away $50 From Specified User`\n\n**Shopping Cart:**\n\nUse: `!shoppingCart`\nInfo: `2x Money From !work`\n\n**Daddys Milk:**\n\nUse: `!daddysMilk`\nInfo: `100x Money In Lottery`\n\n**Jim Coin:**\n\nUse: `!jimCoin`\nInfo: `Special Role In Support Server`" 
        )
        await ctx.send(embed = embed)
    if text == "nuke":
        embed = discord.Embed(
            title = "Item: Nuke Info Page:",
            description = "**Nuke:**\n\nUse: `!nuke @user`\nInfo: `Takes Away $50 From Specified User`" 
        )
        await ctx.send(embed = embed)
    if text == "shoppingCart":
        embed = discord.Embed(
            title = "Item: shoppingCart Info Page:",
            description = "**Shopping Cart:**\n\nUse: `!shoppingCart`\nInfo: `2x Money From !work`" 
        )
        await ctx.send(embed = embed)
    if text == "daddysMilk":
        embed = discord.Embed(
            title = "Item: shoppingCart Info Page:",
            description = "**Daddys Milk:**\n\nUse: `!daddysMilk`\nInfo: `100x Money In Lottery`" 
        )
        await ctx.send(embed = embed)
    if text == "jimCoin":
        embed = discord.Embed(
            title = "Item: shoppingCart Info Page:",
            description = "**Jim Coin:**\n\nUse: `!jimCoin`\nInfo: `Special Role In Support Server`" 
        )
        await ctx.send(embed = embed)

@bot.command()
async def jimCoin(ctx):
    db = sqlite3.connect('currency.sqlite')
    cursor = db.cursor()
    cursor.execute(f"SELECT enable FROM main WHERE user_id = {ctx.author.id}")
    result = cursor.fetchone()

    if result is None:
            embed = discord.Embed(
              title = 'You Do Not Have A One Of a Kind Jim Coin',
              colour = discord.Color.blurple()
            )
            await ctx.send(embed = embed)
    else:
            
                cursor.execute(f"SELECT jimscoin, yay from main WHERE user_id = '{ctx.author.id}'")
                result1 = cursor.fetchone()
                money = int(result1[0])
                if money < 1:
                    embed = discord.Embed(
                    title = 'You Do Not Have Any Shopping Carts',
                    colour = discord.Color.blurple()
                     )
                    await ctx.send(embed = embed)
                else:
                    sql = ("UPDATE main SET cart = ?, yay = ? WHERE user_id = ?")
                    val = (money - 1,  str('enable'), str(ctx.author.id))
                    cursor.execute(sql, val)
                    db.commit()
                    embed = discord.Embed(
                    title = 'You Used Your Jim Coin!',
                    description = 'You Are Now A Pretty Special Person\nExample\n```!items jimCoin```',
                    colour = discord.Color.blurple()
                    )
                    await ctx.send(embed = embed)

bot.add_cog(Music(bot))

bot.run('token')
