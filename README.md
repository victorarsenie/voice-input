# Voice for OpenCode (push-to-talk)

Hold `F9`, speak, release. The clip is transcribed locally and submitted as a
prompt to the OpenCode session titled `voice`, with the `assistant` agent.

Design: `DESIGN.md` in this repo (moved here from Muther on 2026-10-27;
`E:\AI\muther\docs\VOICE.md` is now a pointer to this project).
This folder implements only the input half: hotkey → record → STT → submit.
No TTS, no wake word.

## Files

| file | purpose |
|---|---|
| `voice.py` | coordinator: hotkey, recorder, whisper, submit |
| `voice.json` | key, model, mic, session, agent, CLI path |
| `start-voice.vbs` | starts the listener hidden |
| `logs/voice.log` | every clip, transcript and submit |
| `.venv/` | faster-whisper, sounddevice, keyboard, numpy |

## Config

- `key` — single key held to talk. Default `right ctrl`; the left ctrl is
  ignored (both report scan code 29, so the hook matches on the key name).
- `model` — faster-whisper size. `base` uses the copy already in the HF cache;
  `small.en` is more accurate (~250 MB download, ~2x slower).
- `mic_device` — a name substring pins that device; `null` follows the Windows
  default input. The default is re-read on every press (PortAudio is
  re-initialised, ~30 ms), so swaps need no restart. A pinned device that is
  missing logs a line and falls back to the default.
- `beep` — audible tick when recording starts/stops.
- `silence_peak` — clips whose peak is below this are dropped as muted, so a
  hardware mute never submits an empty prompt (whisper can hallucinate on
  silence).

## Two keys, two behaviours

| key | behaviour |
|---|---|
| hold **right ctrl** | records while held, transcribes on release, posts the prompt (mode `submit`) |
| tap **right shift** | toggles live dictation: mic stays open, each phrase is typed into the focused composer; tap again to stop (mode `toggle_mode`, default `compose`) |

Live dictation segments on pauses (`vad_threshold`, `vad_hangover`), so you can
speak at length and watch it appear, then say `send prompt` when you want it sent.
While live is on the pill stays visible and right ctrl is ignored.

`hold_key`, `toggle_key`, `mode` and `toggle_mode` in `voice.json` control this.
`voice-ctl.ps1 mode <submit|compose>` changes the hold key's mode.

Compose mode only types when the foreground app is `compose_target`
(`OpenCode.exe`); otherwise it logs `compose skipped` and shows `focus the chat
box`, so nothing can land in the wrong window.

Spoken commands in compose mode (exact phrase, after light punctuation stripping):

| phrase | action |
|---|---|
| `send prompt`, `send it`, `send` | press Enter |
| `delete prompt`, `clear prompt`, `scratch that`, `delete it` | Ctrl+A then Delete |
| `remove the last word`, `remove one word`, `remove one`, `delete two`, `delete the last 5 words` | select that many words back, delete (number optional, defaults to 1) |
| `remove the last sentence`, `delete the last sentence` | delete the last sentence |
| `comma`, `add comma`, `punctuation comma` | insert `,` hard against the previous word |
| `period` / `full stop`, `semicolon`, `colon`, `question mark`, `exclamation mark`, `dash` / `hyphen`, `open bracket`, `close bracket`, `apostrophe`, `quote` | insert that character |
| `new line` | insert a newline |

`delete`/`remove`/`undo`/`cut` all work as the verb. Word and sentence commands
must be the whole utterance; `remove one filter` is treated as prose.
Sentence deletion counts the words of the last sentence in the text this tool
typed itself, so manual edits in the box can make it miss.

Deletion counts **words, not tokens**: a full stop or comma sitting on its own
after a word is removed with that word, never on its own. Inserted text also has
stray spaces before punctuation stripped (`Hello there .` → `Hello there.`), and
punctuation commands attach to the preceding word without a space, so
`hello` → `comma` → `world` gives `hello, world`.

Extra punctuation phrases can be added under `punctuation_commands` in
`voice.json`, keyed by the character: `{"–": ["en dash"]}`.

Anything else is pasted in, so you can accumulate a long prompt by voice.

## Feedback while you talk

A borderless, always-on-top pill appears above the taskbar the moment the key
goes down, and never takes focus (`overrideredirect` + topmost, the foreground
window is unchanged — measured):

| state | pill |
|---|---|
| key held | `● recording` (red) |
| key released | `… transcribing` (amber) |
| delivered | `✓ sent` (green, hides after 1.3 s) |
| nothing usable | `too short` / `mic muted?` / `nothing heard` (orange) |

Set `"beep": false` to silence the two ticks.
- `target` — `active` (default) sends the transcript to the session the app last
  showed, using each session's `time.viewed`. `latest` uses the most recently
  updated session. `voice` sends it to a dedicated session titled
  `session_title`, which keeps speech out of graded bench arms.
- `session_id` — pin an exact session instead of resolving by title.
- `opencode` — path to the CLI binary. The npm shim's real exe is
  `%APPDATA%\npm\node_modules\@opencode\cli\bin\opencode.exe`.

## Behaviour notes

- One stream per utterance: the mic opens on key-down and closes on key-up, so
  the speakers are never recorded (see `VOICE.md`, "landmines").
- Key-repeat is ignored while held (`held` flag); one clip per press.
- Clips under `min_seconds` and empty transcripts are dropped, not submitted.
- Nothing runs between utterances: no daemon polling, no idle stream.

## Run

```powershell
E:\AI\voice\.venv\Scripts\python.exe E:\AI\voice\voice.py   # visible, Ctrl+C to stop
wscript E:\AI\voice\start-voice.vbs                        # hidden
```

Or through the control script:

```powershell
E:\AI\voice\voice-ctl.ps1 start     # hidden, waits for the ready line
E:\AI\voice\voice-ctl.ps1 stop
E:\AI\voice\voice-ctl.ps1 restart
E:\AI\voice\voice-ctl.ps1 status    # pid + last log line
E:\AI\voice\voice-ctl.ps1 log       # last 20 lines
```

It never starts at login by choice: ask for it when you want it.
