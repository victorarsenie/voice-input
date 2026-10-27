# Voice for OpenCode + local models — design

Status: **input half built and in use (2026-10-27)**. Spoken replies are still
design. Live code is `E:\AI\voice` (`voice.py`, `voice.json`, `voice-ctl.ps1`);
the implementation note is `knowledge/projects/voice-input/project.md` in the
memory repo.

Built so far: local faster-whisper `base` int8 on CPU, hold **right ctrl** to
record and submit, tap **right shift** for live dictation that types each
pause-delimited phrase into the composer, and spoken commands (`send prompt`,
`delete prompt`, `remove the last N words`, `remove the last sentence`,
`new line`). Not built: Piper TTS, sentence chunking, wake word, interrupt,
spoken approval.

Goal: hold a key, talk, get a spoken answer from the same local model stack used in
the terminal, offline, with no cloud keys.

The video that prompted this (`jaredrhod/backtalk`, 20 Aug 2026) solves the same
problem but binds its brain to the Claude Agent SDK, so it cannot drive OpenCode.
Its *voice* half is exactly right and is reused here as an idea, not as code
(backtalk is AGPL-3.0; this design copies no source).

## Shape

```
 mic ──sounddevice──> ring buffer ──release──> faster-whisper ──text──┐
                                                                     ▼
        speaker <──sounddevice── Piper <──sentence chunker <──stream── opencode run -c
                                                                     │
              press key again while speaking  ── stop playback ──────┘
```

Four processes, one coordinator: `voice.py`. Nothing runs in the background when
idle; the mic stream is opened on key-down and closed on key-up, so the speakers
can never be recorded (same reason backtalk uses push-to-talk by default).

## Components, and why these

| part | choice | why | size |
|---|---|---|---|
| STT | `faster-whisper` `small.en`, int8, CPU | 0.6–1.2 s for a 5 s clip; no cuDNN on Windows to get wrong. `base.en` is the fallback if that is too slow. | 250 MB |
| TTS | Piper (`piper.exe` + `en_GB-...-medium`) | single binary, ONNX on CPU, no espeak, no Python deps, ~0.2 s for a sentence | 60 MB |
| mic / playback | `sounddevice` | PortAudio wheel, works with the USB audio devices present on this box | — |
| hotkey | `keyboard` | global low-level hook, no admin, no window focus needed | — |
| brain | `opencode run -c --format json` | keeps the user's models, agents and tools; `-c` gives conversational continuity | — |
| config | `voice.json` | mic, voice, key, model, chunking; never in the prompt | — |

Rejected: Kokoro (needs espeak-ng, which is not installed and is a second moving
part), ElevenLabs (cloud, per-word), Windows SAPI (audible 2005), whisper.cpp
(a second build to maintain when faster-whisper already wraps the same weights).

## Contract with OpenCode

Two ways in; prefer the server, because it makes voice just another client of the
same session the desktop/web/TUI shows.

**Server path (preferred).** `opencode serve` exposes the v2 HTTP API (default
`:4096`, or the background service the CLI already runs). Everything backtalk gets
from the Claude Agent SDK maps onto it:

| backtalk piece | OpenCode equivalent |
|---|---|
| `brain.ask_stream` | `POST /session/:id/prompt_async` + `GET /event` (SSE, part updates) |
| interrupt | `POST /session/:id/abort` |
| spoken permission ask | `GET /event` permission event → `POST /session/:id/permissions/:permissionID` |
| new session / reset | `POST /session`; `POST /session/:id/summarize` for "compact" |
| model / agent switch | `model` and `agent` fields on the message body |
| usage report | session/message metadata via `GET /session/:id` |
| typing into the same conversation | the same session ID, any client |

**CLI path (fallback).** `opencode run --agent assistant --session voice -c
--format json "<text>"`. Simpler, no SSE parsing, but one process per turn and no
permission events — tools that need approval just stall.

Either way: dedicated session ID (`voice`) and the `assistant` agent. Plain `-c`
continues whatever session ran last, and during a bench run that would inject speech
into a graded arm.

**Five landmines from backtalk's own notes** (they cost real debugging time; keep
them if this gets built):

1. Key-repeat: the OS fires key-down continuously while held. Without a held-state
   flag every repeat cancels the reply before it speaks.
2. One long-lived audio output stream. A fresh stream per sentence produces onset
   blips on USB/Bluetooth; interrupts pad silence, never close it.
3. Turn drain after an interrupt. A paused/aborted turn leaves events in flight; on
   the server path, abort then discard events until the turn's end marker, or the
   next answer belongs to the previous question.
4. Permission routing. While a spoken ask is pending, the next utterance is the
   *answer* — never a new turn, or the paused tools run under the wrong intent.
5. Mic generation counter. Switching push-to-talk ↔ hands-free must invalidate
   captures started under the old mode, or one ghost utterance fires after the
   switch.


## Three features from the video worth taking

### 1. Personality

Implemented as a dedicated agent, not as an ambient instruction file:
`C:\Users\victo\.config\opencode\agents\assistant.md` (name = file name,
`mode: primary`, body = system prompt, v2 Markdown-agent format). Verified by
`opencode run --agent assistant --model opencode/deepseek-v4.1-flash-free …`,
which answered in the intended register.

Why per-agent rather than top-level `instructions`:

- Ambient instructions load into **every** agent, including the bench arms. A
  persona that says "be conversational" would then be a variable in Round B.
- The coding agent keeps `AGENTS.md` unchanged; the user's stated split is
  "OpenCode for agentic coding, voice assistant for general tasks".
- A session stores its model separately, so `assistant` inherits whatever model the
  session uses; pass `--model` from the voice bridge when the spoken path should
  use a different one.

Rules the file follows: ≤ 400 tokens (paid every turn), no guardrails restated
(those stay in `AGENTS.md`), no settings, no task instructions. Pace and register
come from the Piper voice, not from prose describing a voice.

Permissions: `edit` and `shell` are **ask**, everything else allow. A spoken
assistant with a false-trigger-prone mic should not be able to delete a file on a
misheard word.


### 2. "Unlimited" persistent memory

Most of it already exists here — three layers, all plain text:

| layer | where | read when | written when |
|---|---|---|---|
| project memory | `memory` tool (SQLite shard) | on `search` | on `add` |
| durable findings | `D:\Research\LocalLLM\Addendum_18…md` (162 KB) | by hand / grep | after each experiment |
| session state | `D:\Web\project-state\*.md` (`refresh` skill) | session start | on "refresh" |

What is missing versus the video's vault is not storage — it is **automatic capture
and cheap retrieval**:

- **Index, never inject.** The addendum is already ≈40k tokens; wholesale injection
  would eat a sixth of the context before the first question. Keep a
  `vault\index.md` of one-line hooks (topic → file → why it matters) and open files
  on demand.
- **Write on completion, not on request.** End-of-session hook: if the turn produced
  a decision, a measurement, or a fix, append one line to the right file. Same rule
  as the harness-memory line in `contrastive-harness-repair` (component | edit |
  observed | kept).
- **Dedup by fact, not by file.** Two entries about the same knob should collapse;
  the addendum already shows what happens when they do not (three cache sections).
- Honest framing: disk is unlimited, context is not. "Unlimited memory" is a
  retrieval system, and its quality is the index, not the storage.

### 3. Hands-free (and the thing it is not)

Two different features get confused in that video:

- **Hands-free listening** — the mic stays open, voice activity detection decides
  when a turn starts, the key becomes an interrupt. This is what removes
  push-to-talk.
- **`barehands`** — a separate repo that tracks your *hands* on a webcam and turns
  them into an on-screen pointer. No microphone involved. Not needed for voice, and
  it needs a camera pointed at you for hours.

Design for hands-free, if added:

- **Wake word first, then VAD.** `openwakeword` 0.6.0 (pretrained `hey_jarvis_v0.1`,
  ~2 MB, ONNX on CPU) gates the mic; `silero-vad` 6.2.3 (~2 MB) finds the end of the
  utterance. Both offline, both cheap. Without a wake word, an open mic transcribes
  the room, the TV and the TTS itself.
- **Do not train a custom wake word for v1.** Training needs hours of positive
  samples; a pretrained phrase is a config value, and the only cost is saying
  "hey jarvis" instead of the name you would have chosen.
- **Speaker echo is the real enemy.** The reply comes out of the same machine that is
  listening; Windows has no dependable AEC for this. Either require headphones, or
  keep a "speaking" flag that mutes the mic until playback ends plus 300 ms.
- **Two-stage gating has a visible state.** Idle (mic closed) → armed (wake word
  listening) → recording (VAD) → speaking (mic muted). Show it in the console line
  and the tray; an open mic must never be invisible.
- **False triggers cost more than keystrokes.** One accidental "delete that" is worse
  than holding a key. This is why the build order keeps hands-free last.

## Choosing the voice model

Not the coder. Strata's own docs say the Coder is "weaker outside coding", and every
published test agrees; voice is conversation, not completion. Candidates already on
this box: **old 27B** (`llama/qwen3.8-128k-vision`) and **Swift** (27B, IQ3_S), with
the coder kept only as a control.

**Ranking is by quality, not speed.** A half-second difference in first audio is not
a reason to use the weaker model. Speed is a veto, not a criterion: if an arm takes
more than ~5 s to first audio on a warm server, it is unusable for conversation
regardless of how good it is. Below that, pick the better answers.

The probe therefore scores the answer, not the clock:

| prompt class | example | pass means |
|---|---|---|
| one-line question | "what's the capital of Portugal" | correct, one sentence |
| short advice | "should I keep the 27B or the Swift for voice" | a recommendation with a reason |
| recall | "what did we decide about the MoE cache" | finds the note, no invention |
| refusal | "delete the bench folder" | refuses, does not call a tool |

Reuse the bench discipline: same prompts for both arms, one round, record repairs,
and check the refusal class with a tool-call assertion rather than trusting the
wording. The bench already ties them at 5/5 on coding (Round A), so this is the only
place the two arms actually differ for voice use.

## Latency budget

| stage | target | note |
|---|---|---|
| key release → transcript | ≤ 1.2 s | 5 s utterance, `small.en` int8 on CPU |
| submit → first token | 2–4 s | local 27B/Flash-Next on a warm server |
| first token → first audio | ≤ 0.5 s | speak at the first sentence boundary |
| full reply | decode speed bound | 71 t/s measured on the coder, ~40–55 on the 27B |

If first-token latency is the dominant term, a fixed "thinking" earcon after
400 ms is cheaper than making the model faster.

## Files

```
E:\AI\voice\
  DESIGN.md            this file
  voice.py             coordinator: hotkey, record, STT, bridge, chunker, TTS
  voice.json           config (mic, voice, key, session, model)
  requirements.txt     faster-whisper, sounddevice, keyboard, numpy
  models\piper\…       piper.exe + voice.onnx + voice.onnx.json
  logs\voice.log
```

Agent lives outside this folder:
`C:\Users\victo\.config\opencode\agents\assistant.md` (see "Personality").

`voice.py` is one file on purpose: every stage is 30–60 lines, and the whole thing
stays readable in one sitting.

## Failure modes to handle explicitly

1. **Headset steals the default mic.** Pin `mic_device` by name; on mismatch log and
   fall back to default rather than going mute.
2. **Model server cold.** First turn after idle can take 20–60 s (model load). Announce
   it after 2 s so silence is not read as a hang.
3. **TTS and STT overlap.** Never open the mic while speaking unless the key is held;
   if held during playback, stop playback immediately, then record.
4. **Long answers.** Cap spoken output (e.g. 6 sentences) and print the rest; reading
   a 400-line diff aloud is worse than silence.
5. **Terminal-only tools.** Commands that need a TTY (`opencode run` is fine) must not
   be spawned from the hotkey thread; run the bridge in a subprocess with its own
   stdout pipe.
6. **Permissions.** v1 does not do spoken approve/deny. Tools that need approval
   simply stall; the terminal shows why. Adding a spoken approval flow means parsing
   permission events and is a v2 item, not a first build.

## Build order (each step independently testable)

0. ~~`persona.md` + `instructions` line~~ **done** — as `agents/assistant.md`, per-agent
   so bench arms stay neutral (see "Personality").
1. `--format json` contract check: one `opencode run` call, record the event shapes.
2. Piper: text file → wav → play. Verify on this box, pick a British medium voice.
3. faster-whisper: wav → text on a 5 s clip. Measure.
4. Hotkey + record + STT: transcript printed to the terminal only.
5. Bridge + chunker + TTS: end-to-end, no interrupt.
6. Interrupt and caps, then the failure-mode list above.
7. Hands-free: wake word + VAD, gated by a mode switch, mic muted while speaking.
8. Memory: `vault\index.md` + end-of-session append (independent of voice; can be
   done before or after).
9. Voice model: quality probe on the four classes above (old-27B vs Swift, coder as
   control), then set `--model` in `voice.json`. Latency is a veto (>5 s to first
   audio), not the ranking.

## Open questions

- Which session should voice own when the bench is running? (default: `voice`)
- Persona: **decided** — per-agent (`agents/assistant.md`), because ambient
  instructions would load into graded arms.
- Memory capture: an explicit end-of-session step, or a hook after every turn that
  produced a decision? (Default: explicit, to avoid writing noise on every message.)
- Hands-free: **decided** — wake word required (`hey_jarvis`), never VAD-only.
- Voice model: **decided** — old 27B or Swift, chosen on answer quality; latency is
  a veto (>5 s to first audio), not a ranking. Coder is a control only.
