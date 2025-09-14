# YouTube Subscription Rescriber (Playwright)

A Python utility to automatically re-subscribe to YouTube channels from a CSV export (e.g. from [Google Takeout](https://takeout.google.com/)). It uses **Playwright** to log in once and then process each channel in sequence.

---

## ✨ Key Features

- Reads channel list from `subscriptions.csv`
- Logs successful subscriptions to a text file
- Records failed attempts in a separate CSV
- Remembers where you left off with an offset file
- Periodically restarts the browser for stability

---

## 🖥️ Compatibility

- ✅ Tested on Linux  
- ⚠️ Should also work on Windows/macOS, though untested

---

## 📋 Requirements

- Python 3.8 or newer  
- [Playwright](https://playwright.dev/python/) (`pip install playwright`)  
- Chromium browser (Playwright installs it automatically)

---

## 🚀 Setup & Usage

### 1. Get your subscriptions file

1. Visit [Google Takeout](https://takeout.google.com/).  
2. Select **YouTube and YouTube Music** only.  
3. Export and extract the archive.  
4. Inside the `subscriptions/` folder you’ll find `subscriptions.csv`.  
   Copy this file to the same directory as the script.

---

### 2. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

---

### 3. Run the script

```bash
python3 youtube_subscribe.py
```

- On the first run, a browser window opens.  
- Log into your YouTube account.  
- Hit **Enter** in the terminal once you’re done.  
- From then on, it will work headless using the saved `auth.json`.

---

## 📂 Generated Files

- `subscription_log.txt` – records channels successfully subscribed  
- `skipped_channels.csv` – channels that failed (with reason)  
- `last_offset.txt` – progress marker so you can resume later  
- `auth.json` – saved login session for Playwright  

---

## ⚠️ Notes & Caveats

- Mimics normal browsing pace but could still trigger YouTube limits.  
- Use responsibly and at your own risk.  
- Don’t spam — respect YouTube’s [Terms of Service](https://www.youtube.com/t/terms).  

---

## 🔧 Troubleshooting

- **Login not detected?** Ensure you complete login fully before pressing Enter.  
- **Browser keeps restarting?** That’s intentional after every 25 subscriptions to prevent memory leaks.  
- **Playwright not finding Chromium?** Run `playwright install chromium` again.

---

## ✅ Tested On

- Linux (openSUSE Tumbleweed)  
- Chromium via Playwright 1.45+  
- Python 3.11  
