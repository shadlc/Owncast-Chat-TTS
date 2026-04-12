# Owncast Chat TTS

[![Python Version](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)](https://www.microsoft.com/windows)
[![License](https://img.shields.io/badge/license-GPLv3-green)](LICENSE)

A desktop application that reads Owncast chat messages aloud using TTS (Text-to-Speech). Supports system TTS and OpenAI TTS backends with interruptible playback.

## Features

- Real-time chat monitoring via Owncast WebSocket API
- Text-to-Speech with interruption (new message stops current speech)
- Configurable TTS rate and volume (system backend)
- OpenAI TTS support (requires API key)
- Chat history with configurable max lines
- Pause/resume playback
- Single-instance enforcement

## Requirements

- Python 3.12 or higher
- Owncast server with WebSocket integration enabled

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/shadlc/Owncast-Chat-TTS.git
   cd Owncast-Chat-TTS
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run the application:
   ```bash
   python main.py
   ```

## Configuration

The first launch creates a `config.json` file. You can also configure settings via the GUI (Settings button).

### WebSocket URI

To obtain the WebSocket URI:

1. In your Owncast admin interface, go to **Integrations** → **Access Tokens**.
2. Create or copy an existing access token.
3. Construct the WebSocket URI as:
   ```
   ws://your-owncast-server:8080/ws?accessToken=YOUR_TOKEN
   ```
   (Use `wss://` if using HTTPS.)

Paste this URI into the **Owncast WS URL** field in the settings.

### TTS Backend

- **system**: Uses Windows SAPI5 voices. Adjust rate and volume.
- **openai**: Uses OpenAI TTS API. Requires API key, model, voice, and endpoint URL (default `https://api.openai.com/v1/audio/speech`).
  