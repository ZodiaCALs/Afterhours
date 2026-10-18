# Afterhours

A small, private podcast player for slow evenings. Afterhours is a single file that runs on your own computer and opens in your browser. It comes with 30 shows already on the shelf, recommends related ones, and captions episodes that don't have captions.

## Quick start

You need Python 3.8 or newer and a current browser (Chrome, Edge, Safari 16 or later, or Firefox).

| System | Command |
| --- | --- |
| macOS or Linux | `python3 afterhours.py` |
| Windows | `py afterhours.py` |

Afterhours opens at `http://127.0.0.1:8765`. Keep the terminal window open while you listen, and press Ctrl+C to stop.

The first launch takes about a minute. Afterhours finds the feed and cover art for each of the 30 pre-added shows through Apple's podcast directory, which only allows a few lookups a minute. A progress bar on Listen Now shows how far along it is. Results are saved, so later launches start straight away.

## What it does

**Library.** 30 shows are pre-added, grouped into seven shelves:
- City pop and anime songs
- Jazz, hip-hop and piano
- Gadgets and tech
- Literature and light lit
- Mystery novels
- Economics, physics and astronomy
- The game industry

You can add any show by searching for it or by pasting a link. The link can be an RSS feed, an Apple Podcasts page or the show's website. You can also import and export your shows as OPML.

**Listen Now.** Up Next shows what you haven't finished, and Latest episodes collects new episodes across your shelf.

**Discover.** This is the page of related recommendations:
- "Because you listen to…" rows, based on what you play.
- Hand-picked related shows for each interest.
- A tab for each topic.
- 日本語 and 中文 tabs that search the Japanese and Taiwanese stores.

You can open any show to preview its episodes before adding it.

**Player:**
- Speed from 0.5× to 3×, with voices kept at their normal pitch.
- Back 15 seconds, forward 30, and previous or next episode.
- Sleep timer: 15, 30, 45 or 60 minutes, or the end of the episode.
- Volume and mute.
- Autoplay of the next episode.
- Resumes where you left off.
- Lock-screen and media-key controls.

**Now Playing.** It opens full screen over a blurred version of the cover. The transcript scrolls alongside like song lyrics, with the current line highlighted, and you can click any line to jump there.

**Captions:**
- Transcripts that come with the feed (WebVTT, SRT, JSON or HTML).
- Caption files you load yourself.
- Auto captions recognised on your computer (see the next section).

While you browse, the current line floats above the player bar like a subtitle.

**Settings.** One place for:
- the auto captions switch and recognition quality
- showing captions, autoplay and appearance
- importing and exporting shows
- looking again for shows that couldn't be found

Open it from the sidebar, or from the gear on Listen Now and Library on a phone.

**Design.** Light and dark appearance. Each show page and the player take their accent colour from the cover art. Layouts are made for desktops, tablets and phones, including phones held sideways.

## Auto captions (optional)

When an episode has no captions, Afterhours can recognise the speech on your computer and caption it. Captions start from where you are in the episode, appear as they're ready, and then fill in the rest.

**Turn it on once.** Quit Afterhours, run this, then start Afterhours again:

```
python3 afterhours.py --setup-captions
```

This installs the free [faster-whisper](https://github.com/SYSTRAN/faster-whisper) add-on. The speech model downloads once, the first time it's needed. The default model is about 145 MB, and Japanese and Chinese shows use a larger one of about 480 MB for better accuracy.

If your Python refuses to install add-ons (common with Homebrew and some Linux systems), the setup command prints three commands that create a small private environment for Afterhours instead.

**Speed.** On a recent computer, the default model usually keeps ahead of playback. Larger models are slower but more accurate. A supported NVIDIA graphics card is used automatically.

**Turning it on and off.** The Auto captions switch is in three places, and they always match:
- the sidebar
- Now Playing, next to the captions
- Settings

Switching it off stops any captioning in progress. Lines already made stay, and Now Playing offers "Caption this episode" whenever you want one episode captioned anyway.

**Quality.** Settings › Recognition quality offers Automatic, Fast, Accurate and Most accurate. Automatic uses Fast for English shows and Accurate for Japanese and Chinese ones. After an episode is captioned with Fast, Now Playing offers "Redo with more accuracy".

**Reading the audio.** Afterhours reads episode audio itself, so it works with any version of the add-on's audio library (PyAV). If ffmpeg is installed, it's used as a backup for files PyAV can't read. Settings › Caption recognition shows whether everything is ready, with version numbers.

**Privacy.** Recognition runs entirely on your computer. The only downloads are the episode audio and the one-time model download. Finished captions are saved in `~/.afterhours/captions`, so they appear instantly next time. To remove them, use Settings › Saved auto captions › Delete.

## How Afterhours finds episodes

1. **The show's RSS feed.** If a site turns Afterhours away, it tries once more the way a web browser would.
2. **The show's website**, if the feed fails or has no episodes. Afterhours looks for:
   - a feed the page links to
   - an Apple Podcasts link it can turn into a feed
   - common feed addresses, used only if the feed's title matches the show
   - failing all that, the episodes and audio on the page itself and on its episode pages

   When this works, the show switches to the working source and its page says so.
3. **By hand.** On a show's page, press **Change source** (the two arrows) and paste the show's RSS feed or website.

Some shows can't be played anywhere outside one app (for example Spotify exclusives), because their audio isn't public.

## Keyboard shortcuts

| Key | Action |
| --- | --- |
| Space or K | Play or pause |
| ← or J / → or L | Back 15 seconds / forward 30 seconds |
| [ / ] | Slower / faster |
| C | Captions on or off |
| T | Transcript |
| M | Mute |
| Shift+N / Shift+P | Next / previous episode |
| / | Search |
| Esc | Close Now Playing or the speed panel |

## Your data

- **Library, progress and settings** are kept in your browser on this computer. Each browser and each port keeps its own copy, so use the same one each time. To move your shows, use Export OPML and Import OPML in Settings.
- **Auto captions** are saved in `~/.afterhours`. Set `AFTERHOURS_DATA` to use another folder.
- **Network:** Afterhours only listens on your own computer (127.0.0.1). It only connects to:
  - podcast sites and their cover art
  - Apple's public podcast directory
  - Hugging Face, once, for the speech model

## Options

| Option | What it does |
| --- | --- |
| `--port 8765` | Use a different port. Each port keeps its own library. |
| `--no-browser` | Don't open the browser automatically. |
| `--setup-captions` | Install the auto captions add-on, then exit. |
| `--verbose` | Log every request in the terminal. |

| Environment variable | What it does |
| --- | --- |
| `AFTERHOURS_DATA` | Where auto captions are saved (default `~/.afterhours`). |
| `AFTERHOURS_APPLE_INTERVAL` | Seconds between Apple directory lookups (default 3.1). Going much lower can get you rate-limited. |

## Troubleshooting

- **Secure connections fail on a Mac.** If you installed Python from python.org, open Applications › Python and run "Install Certificates.command", then restart Afterhours.
- **"Port 8765 is in use."** Another program has the port. Run `python3 afterhours.py --port 8766`. Your library lives per port, so keep using the new one.
- **A show won't load.** Open it and press Change source, then paste its website or feed. Some feeds only work in certain countries; for example, Composer of the Week is UK-only.
- **"Apple's podcast directory asked Afterhours to slow down."** Wait a minute. Setup and searches carry on by themselves.
- **"Caption recognition isn't installed."** Run `python3 afterhours.py --setup-captions`, then restart Afterhours.
- **Auto captions stopped with "open() got an unexpected keyword argument 'metadata_errors'".** This affected Afterhours 3.0 with newer versions of PyAV. Afterhours 3.1 fixes it. If a different caption error appears, update the add-on with `python3 afterhours.py --setup-captions`.
- **An episode won't play.** Its audio link may have changed. Press the refresh button on the show's page.

## Project structure

Everything is in `afterhours.py`. The Python part is a small local helper that:
- serves the app
- fetches feeds, websites and cover art
- paces Apple directory lookups
- runs caption recognition

The app itself (HTML, CSS and JavaScript) is embedded in the same file. There are no other dependencies unless you turn on auto captions.

## Changelog

### v0.1.1

**Fixed**

- **Auto captions failing** with "open() got an unexpected keyword argument 'metadata_errors'". PyAV 14 and later removed an option that some faster-whisper versions still pass when reading audio. Afterhours now reads the audio itself in a way that works with every PyAV version. It skips damaged stretches of audio instead of stopping, and falls back to ffmpeg if it's installed.
- **Clearer caption errors**, including when the speech model couldn't be downloaded.
- **Narrow phones:** the Library header no longer scrolls sideways on the narrowest phones.

**Added**
- **Auto captions switch** in the sidebar, in Now Playing and in Settings, always in step. Switching off stops captioning in progress, and nothing starts by itself while it's off.
- **Settings page.** It has:
  - switches for showing captions, auto captions and autoplay
  - recognition quality and appearance
  - the caption add-on's status, with version numbers
  - deleting saved auto captions
  - looking again for missing shows
  - OPML import and export

  On phones it's a tap away from Listen Now and Library.

**Changed**
- Appearance and OPML import and export moved from the sidebar into Settings.
