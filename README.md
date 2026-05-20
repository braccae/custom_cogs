# Custom Cogs for Red-DiscordBot

Welcome to the **Custom Cogs** repository for [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot). This repository hosts a collection of specialized cogs designed to extend your bot's capabilities.

Currently, this repository features:
* **GeminiVoice**: A high-performance, real-time voice assistant utilizing the Gemini Multimodal Live API to talk and listen directly in voice channels. 
# Currently does not work due to DAVE E2EE implementation and voice-recv support for it.

---

## 🚀 How to Add This Repo to Red-DiscordBot

To install cogs from this repository, you must add it to your Red-DiscordBot instance using the `Downloader` cog.

### Step 1: Load the Downloader Cog
If you haven't already, load Red's downloader module:
```text
[p]load downloader
```
*(Note: `[p]` is your bot's command prefix, e.g., `!` or `.`)*

### Step 2: Add the Repository
Register this repository with your bot:
```text
[p]repo add braccae-cogs https://github.com/braccae/custom_cogs
```

### Step 3: Install a Cog
Install the desired cog (e.g., `geminivoice`):
```text
[p]cog install braccae-cogs geminivoice
```
Red will automatically download the cog and attempt to install its requirements.

### Step 4: Load the Cog
Once installed, load the cog into your running instance:
```text
[p]load geminivoice
```

---

## 📦 Python Dependencies

If the `Downloader` fails to automatically install the required dependencies (or if you are running in a restricted environment), you can install them manually via Red's internal pip manager:

```text
[p]pip install google-genai discord-ext-voice-recv
```

Alternatively, from your server shell (with the Red virtual environment activated):
```bash
pip install google-genai discord-ext-voice-recv
```

---

## 📂 Available Cogs

| Cog Name | Description | Status | Detailed Guide |
| :--- | :--- | :--- | :--- |
| **GeminiVoice** | Live speech-to-speech voice assistant powered by Gemini 2.5 | 🟢 Active | [GeminiVoice README](./geminivoice/README.md) |

---

## 🛠️ Development & Contributing

If you want to run this repository locally for development or add your own cogs:

1. Clone this repository to your local workspace.
2. Link or add the local path using Red's downloader:
   ```text
   [p]repo add local-cogs /path/to/your/custom_cogs
   ```
3. Submit a pull request for any updates or new cogs!
