#!/usr/bin/env python3
"""
plexnote.py – Discord-Webhook für neu hinzugefügte Plex-Medien (Tautulli-Trigger)
Überarbeitet 31-05-2025
  • exklusiver File-Lock (pending → sent)
  • Rate-Limit-Retry
  • gemeinsame HTTP-Session
  • Placeholder-Bild
  • kompaktes Logging
  • Python ≤ 3.6 kompatibel (kein Walrus-Operator)
"""

import os, re, sys, html, json, time, argparse, urllib.parse, contextlib, fcntl
from datetime import datetime
from typing import Any, Dict, Optional, Tuple, List

import requests

# ═════ Konfiguration ══════════════════════════════════════════
WEBHOOK_URL      = "<DEIN_DISCORD_WEBHOOK_URL>"
TAUTULLI_URL     = "http://<TAUTULLI_SERVER_IP>:<PORT>"
TAUTULLI_API_KEY = "<DEIN_TAUTULLI_API_KEY>"
PLEX_BASE_URL    = "https://app.plex.tv"
PLEX_SERVER_ID   = "<DEINE_PLEX_SERVER_ID>"
TMDB_API_KEY     = "<DEIN_TMDB_API_KEY>"

COLOR_MOVIE, COLOR_SEASON, COLOR_SHOW = 0x1abc9c, 0x3498db, 0xe67e22
MAX_LINE_LEN, MAX_LINES, PLOT_LIMIT   = 45, 4, 150
MAX_WORD_SPLIT_LEN, SINGLE_LINE_LIMIT = 60, 45
HTTP_TIMEOUT, TMDB_TIMEOUT            = 20, 4
RETRY_ATTEMPTS                        = 3

INDENT = " " * 6
NBSP_INDENT = INDENT.replace(" ", "\u00A0")
ZWS = "\u200B"

POSTED_KEYS_FILE = "posted.json"
POSTED_KEYS_MAX  = 200
PLACEHOLDER_IMG  = "https://cdn.discordapp.com/attachments/<CHANNEL_ID>/<BILD_ID>/<DATEINAME>.jpg"

