import urllib.request
import json
import asyncio
import os
import re
import sys
import re
import random
import time
import sqlite3
import math

import configparser

import encodings.aliases
from datetime import datetime
from datetime import timezone, tzinfo, timedelta
import time
import time as timeModule

import asyncio
from discord.voice_client import VoiceClient
import youtube_dl

import discord
from discord import File
from discord import Game
from discord.ext import commands
from discord.ext.commands import Bot
from discord.utils import find
from discord.utils import get
from discord import Spotify

bot = commands.Bot(command_prefix='!')
bot.remove_command("help")


initial_extensions = ['cogs.music']

if __name__ == '__main__':
    for extension in initial_extensions:
        bot.load_extension(extension)

bot.run('super secret token')
