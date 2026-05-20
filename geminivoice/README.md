# 🎙️ GeminiVoice

A Discord Red-Bot cog that joins a voice channel, listens to voice inputs, and responds in real-time using the **Gemini Multimodal Live API**.


# Currently does not work due to DAVE E2EE implementation and voice-recv support for it.
---

## ✨ Features

- **Real-Time Voice Streaming**: Directly feeds voice channel audio to Gemini and streams audio responses back.
- **Natural Interaction**: Responds to wake words (e.g., "Hey Gemini") and ignores general background chat.
- **Multimodal Live Engine**: Powered by the state-of-the-art `gemini-2.5-flash-native-audio-latest` model.
- **Built-in DAVE E2EE Support**: Employs custom packet processing and fallback bytes-stripping for Discord's End-to-End Encryption (DAVE).

---

## 🛠️ Requirements

Before loading `GeminiVoice`, verify that you have installed the required python dependencies:

1. **`google-genai`**: The official Google GenAI SDK.
2. **`discord-ext-voice-recv`**: Allows the bot to receive voice data from voice channels.

You can install them by running:
```text
[p]pipinstall google-genai discord-ext-voice-recv
```

---

## 🔑 API Key Setup

GeminiVoice requires a Gemini API key from Google AI Studio.

### Step 1: Obtain a Gemini API Key
1. Go to **[Google AI Studio](https://aistudio.google.com/)**.
2. Sign in with your Google account.
3. Click **Create API Key** and generate a new key.

### Step 2: Configure the API Key in Red
Set the API token using Red's built-in `set api` command:
```text
[p]set api gemini api_key YOUR_GEMINI_API_KEY
```
*(Make sure to replace `YOUR_GEMINI_API_KEY` with the actual key you copied).*

---

## 🎮 Commands & Usage

### 1. Set Default Channel
Set the default voice channel for the guild:
```text
[p]gemini_set_channel <voice_channel>
```
*Example:* `[p]gemini_set_channel General`

### 2. Join Voice Channel
Connect Gemini to a voice channel and begin listening:
```text
[p]gemini_join [voice_channel]
```
- If a `<voice_channel>` is specified, the bot will join it.
- If no channel is specified, it will look for the default channel set by `gemini_set_channel`.
- If no default is set, it will attempt to join the voice channel of the command author.

Once joined, Gemini will start listening for you to say **"Hey Gemini"** or **"Gemini"**.

### 3. Leave Voice Channel
Disconnect Gemini from the voice channel:
```text
[p]gemini_leave
```

---

## 💡 How It Works

- **Trigger Phrase**: GeminiVoice listens continuously but will only generate a voice response when it hears a name trigger like "Hey Gemini" or "Gemini".
- **Real-Time Stream**: The cog converts user voice input (48000Hz stereo) into mono 16000Hz PCM and streams it to Google's live server. The server streams back 24000Hz audio, which is upsampled and mixed to stereo 48000Hz before playing it back to the channel.
