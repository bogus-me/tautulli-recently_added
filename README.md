# tautulli-recently\_added

# 📚 plexnote.py – Discord Notifications for Plex via Tautulli *(multilingual support in development via centralized output system)*

> Automatically sends Discord embeds for new Plex content via Tautulli, including posters, trailers, ratings, and metadata.

---

## **🌟 Goal**

This Python script sends detailed embed messages to a Discord channel whenever new content (movies, shows, seasons, or episodes) is added to your Plex library. It is triggered by Tautulli and uses TMDB for rich metadata.

### **Special Features (Script Highlights)**

* Full support for **movies**, **seasons**, **episodes**, and **shows**
* Embed-style system: `boxed`, `telegram`, `klassisch` – optimized for mobile & desktop
* **TMDB + TVDB Fallback** logic for metadata (plot, poster, trailer)
* Automatically selected best matching image (Backdrop or Poster)
* Optimized metadata fallback (TMDB → TVDB → placeholder)
* Plot, cast, genre, release, status, runtime
* **Duplicate Protection** with `posted.json`, locked cross-platform
* Handles garbage or missing episode titles and replaces them with TMDB/TVDB titles (if available)
* Works **headless**, no manual input needed (via Tautulli trigger)
* Supports **audio/subtitle detection**, runtime, codec, studio
* Discord posts include clickable links to TMDB, TVDB, Plex, and trailer
* Multilingual metadata fetching (defaults to **de-DE** with fallback to **en-US**)
* Fully automated, Python 3.6+ compatible

---

## **1. 📂 Prepare the Script Folder**

Navigate to your Tautulli config folder:

```bash
cd /path/to/tautulli/config/
```

Create the `scripts/` folder if it doesn’t exist:

```bash
mkdir -p scripts
```

---

## **2. 📜 Download the Script**

Download `plexnote.py` and save it into:

```bash
tautulli/config/scripts/plexnote.py
```

Make it executable:

```bash
chmod 755 tautulli/config/scripts/plexnote.py
```

---

## **3. 🔔 Create a Discord Webhook**

1. Open Discord and go to your server.
2. Create or choose a text channel (e.g., `#plex-activity`).
3. Click the gear icon → **Integrations** → **Webhooks**
4. Click **"New Webhook"**
5. Give it a name (e.g. `PlexBot`) and assign the correct channel.
6. Click **"Copy Webhook URL"**

Paste this into the script as `WEBHOOK_URL`.

---

## **4. ⚙️ Configure the Script**

Open `plexnote.py` and update these lines near the top:

```python
WEBHOOK_URL      = "https://discord.com/api/webhooks/..."  # Your Discord webhook
TAUTULLI_URL     = "http://localhost:8181"                  # Your Tautulli URL
TAUTULLI_API_KEY = "..."                                    # API key from Tautulli
PLEX_BASE_URL    = "https://app.plex.tv"                    # Plex app URL
PLEX_SERVER_ID   = "..."                                    # Your Plex Server ID
TMDB_API_KEY     = "..."                                    # Your TMDB API key
TVDB_API_KEY     = "..."                                    # Your TVDB API key
```

To customize the fallback placeholder image:

1. Upload an image into any Discord channel.
2. Right-click and choose **"Copy Link"**.
3. Paste it in the config:

```python
PLACEHOLDER_IMG = "https://cdn.discordapp.com/attachments/..."
```

Make sure the link is public and ends with `.jpg`, `.png`, or similar.

---

## **5. 🔍 Where to Get Your Keys and IDs**

### **🔐 Tautulli API Key**

1. Open **Tautulli**
2. Go to **Settings** → **Web Interface**
3. Copy your **API Key**

### **🏠 Plex Server ID**

1. Go to: [https://app.plex.tv/desktop](https://app.plex.tv/desktop)
2. Open any library (e.g. Movies)
3. The URL looks like:

```
https://app.plex.tv/desktop/#!/media/1234567890abcdef1234567890abcdef12345678/com.plexapp.....
```

Copy the alphanumeric part after `/media/` → this is your `PLEX_SERVER_ID`

### **🎮 TMDB API Key**

1. Register at [https://www.themoviedb.org](https://www.themoviedb.org)
2. Go to [https://www.themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)
3. Apply for a **Developer** key
4. Use the **API Key (v3 auth)**

### **🛰️ TVDB API Key**

1. Create an account at [https://thetvdb.com](https://thetvdb.com)
2. Go to [https://thetvdb.com/dashboard](https://thetvdb.com/dashboard)
3. Click on **API** in the top bar or visit:
   [https://thetvdb.com/api-information](https://thetvdb.com/api-information)
4. Request access to **API v4**
5. Copy the generated API Key into `TVDB_API_KEY`

> The script uses v4 token-based authentication and caches the token.

---

## **🧠 Automatic Duplicate Protection**

* Automatically creates a `posted.json` file in script directory
* Prevents reposting by tracking `rating_key` + media signature
* Keeps only the last 200 entries (older ones are deleted)
* Uses cross-platform file-locking to ensure safe access

No extra setup needed.

---

## **💡 Add the Script to Tautulli**

1. Open **Tautulli**
2. Go to **Settings** → **Notification Agents**
3. Click **Add new notification agent**
4. Choose **Scripts**
5. Use:

```
Script Folder:   tautulli/config/scripts/
Script File:     plexnote.py
Timeout:         60
Description:     Discord PlexNote
```

6. Enable trigger:

```
[x] Recently Added
```

7. Save

---

## **✅ Done!**

New Plex content is now automatically posted to Discord with rich, styled embeds.

No cronjobs. No polling. Just pure Tautulli trigger magic.
