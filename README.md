# tautulli-recently_added

# 📚 plexnote.py – Discord Notifications for Plex via Tautulli *(multilingual support in development via centralized output system)*

> Automatically sends Discord embeds for new Plex content via Tautulli, including posters, trailers, ratings, and metadata.

---

## **🌟 Goal**

This Python script sends detailed embed messages to a Discord channel whenever new content (movies, shows, seasons, or episodes) is added to your Plex library. It is triggered by Tautulli and uses TMDB for rich metadata.

---

## **1. 📂 Prepare the Script Folder**

Navigate to your Tautulli config folder:

```
cd /path/to/tautulli/config/
```

Create the `scripts/` folder if it doesn’t exist:

```
mkdir -p scripts
```

---

## **2. 📜 Download the Script**

Download `plexnote.py` and save it into:

```
tautulli/config/scripts/plexnote.py
```

Make it executable:

```
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

Example:

```
https://discord.com/api/webhooks/123456789012345678/AbCdEfGhIjKlMnOpQrStUvWxYz
```

Paste this into the script as `WEBHOOK_URL`.

---

## **5. 🔍 Where to Get Your Keys and IDs**

### **🔐 Tautulli API Key**

1. Open **Tautulli**
2. Go to **Settings** → **Web Interface**
3. Your API key is listed under **API Key**

---

### **🏛️ Plex Server ID**

1. Go to: [https://app.plex.tv/desktop](https://app.plex.tv/desktop)
2. Click any library (e.g., Movies)
3. Look at the URL. It will look like:

```
https://app.plex.tv/desktop/#!/media/1234567890abcdef1234567890abcdef12345678/com.plexapp.plugins.library?source=28
```

The string after `/media/` is your **Plex Server ID**.

```
1234567890abcdef1234567890abcdef12345678
```

Paste this into `PLEX_SERVER_ID`.

---

### **🎮 TMDB API Key**

1. Register at [https://www.themoviedb.org](https://www.themoviedb.org)
2. Go to [https://www.themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)
3. Apply for a **Developer** key
4. Use the **API Key (v3 auth)** in `TMDB_API_KEY`

---

### **⚙️ Configure the Script**

Open `plexnote.py` and update these lines near the top:

```
WEBHOOK_URL      = "https://discord.com/api/webhooks/..."  # Your Discord webhook
TAUTULLI_URL     = "http://localhost:8181"                  # Your Tautulli URL
TAUTULLI_API_KEY = "..."                                    # API key from Tautulli
PLEX_BASE_URL    = "https://app.plex.tv"                    # Plex app URL
PLEX_SERVER_ID   = "..."                                    # Your Plex Server ID (see below)
TMDB_API_KEY     = "..."                                    # Your TMDB API key
```

To customize the fallback placeholder image:

1. Upload an image into any Discord channel (e.g. drag & drop a wallpaper).
2. Right-click the uploaded image and select **"Copy Link"**.
3. Paste this link into the `PLACEHOLDER_IMG` line inside `plexnote.py`:
3.1 Poster Example: https://wallpapercave.com/wp/wp7617642.png"

```
PLACEHOLDER_IMG = "https://cdn.discordapp.com/attachments/..."
```

Make sure the link is public and ends in `.jpg`, `.png` or similar.

---

## **🧠 Automatic Duplicate Protection (**\`\`**)**

The script creates a `posted.json` file in the same folder:

* Prevents duplicates by tracking `rating_key` and signature
* Keeps up to 200 entries, older ones are trimmed
* Uses file locking (`fcntl`) to avoid race conditions
* Sets safe permissions automatically

No manual setup needed – it’s fully automatic.

---

## **🛠️ Add the Script to Tautulli**

To connect the script with Tautulli, follow these steps:

1. Open **Tautulli**
2. Go to **Settings** in the left sidebar
3. Click **Notification Agents**
4. Click **Add a new notification agent**
5. Choose **Scripts** from the list
6. In the configuration panel:

```
Script Folder:   tautulli/config/scripts/
Script File:     plexnote.py
Timeout:         60
Description:     Discord PlexNote (optional)
```

7. Scroll down and under **Notification Triggers**, enable:

```
[x] Recently Added
```

8. Save the configuration

---

## **✨ Features**

* Supports **movies**, **shows**, **seasons**, **episodes**
* Automatically fetches metadata and trailers from TMDB
* Poster/backdrop fallback via TMDB, Plex, or static image
* 3 embed styles: `"boxed"`, `"telegram"`, `"klassisch"`
* Full plot summarization, rating, genre, status, runtime
* Trailer links (YouTube or Plex)
* Duplicate protection via `posted.json`
* Lightweight, no uploads or downloads required
* Python 3.6+ compatible

---

## **✅ Done!**

Your Plex server now notifies your Discord channel automatically whenever new media is added – clean, fast, and rich with metadata.

---

