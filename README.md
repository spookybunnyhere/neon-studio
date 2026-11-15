# Neon Studio

A dark, glass-style desktop app for controlling **MELK / ELK-BLEDOM style Bluetooth LED lights**
(the kind driven by the *Magic Lantern* / *Lotus Lantern* phone apps). Built with Python, `customtkinter` and `bleak`.

> Tested with one Desidiya neon strip (service `fff0`, write characteristic `fff3`). Other controllers of the same
> family should work; others may not. Reports and pull requests welcome.

## Features
- **First-run setup:** scans for nearby Bluetooth lights, or paste an address by hand. Remembered next time.
- **Auto-reconnect:** if the link drops, the app reconnects and resumes what it was doing.
- **Home:** colour picker, hex input, quick swatches, brightness.
- **Effects:** rainbow, breathing, fire, police, strobe, ocean, random fade + preset palettes.
- **Screen:** ambient light that follows your screen (dominant colour or average, edge weighting, black-scene cut-off).
- **Music:** lights react to system audio or the microphone.
- **Tools:** PC meter (CPU / RAM / GPU / disk / battery), typing-reactive light, double-clap toggle,
  focus timer (colour blend), pattern studio, text and voice commands, a raw/built-in-mode explorer.
- **System tray** icon (closing the window hides it to the tray; use tray -> Quit to exit).
- **Auto-tune** of the update rate so a slow Bluetooth link never builds up lag.

## Free vs Premium
- **Free:** lights on/off, colours (picker, hex, swatches) and brightness.
- **Premium (₹129, lifetime):** effects & palettes, screen ambient sync, music mode, PC meter, typing light,
  double-clap, focus timer, pattern studio, voice/text commands, explorer.
- Click **Buy Premium** in the app: pay ₹129 by UPI, send your Gmail + UTR from the app, and your key is e-mailed to you once the payment is verified. Paste it under **Enter licence key**.

## Install
Requires Python 3.9+ and a Bluetooth LE adapter.

```bash
git clone https://github.com/spookybunnyhere/neon-studio.git
cd neon-studio
pip install -r requirements.txt
python neon_studio.py
```

Optional features only need their own packages; the app still starts without them and tells you what's missing.

## First run
1. Switch the light on and close any phone app connected to it (most controllers accept one connection).
2. Press **Scan for lights**, pick yours (⭐ marks likely LED controllers), or type its address.
3. Settings are saved to `~/.neon_studio.json`.

## Notes and limits
- Bluetooth adds a small, unavoidable delay (typically 50-200 ms). Neon Studio keeps it steady, it cannot remove it.
- Effects and ambient mode are drawn by the app, so it must stay running.
- Voice commands use Google's free speech service (internet required; your 5 s clip is sent there).
- Typing mode only counts key presses; it never records what you type.
- Linux/macOS: the tray icon and global key listening may need extra permissions.

## Troubleshooting
- **Won't connect / keeps dropping:** close the phone app, move the PC or dongle closer, use a USB 2.0 port,
  and disable "allow the computer to turn off this device to save power" for the Bluetooth adapter.
- **Crash on start:** see `neon_studio_error.log` next to the script.

## License
MIT - see `LICENSE`.
