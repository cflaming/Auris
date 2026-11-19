# Auris

Offline audiobook reader for EPUB, PDF, and TXT with local OmniVoice TTS, character-aware voices, per-book narrator control, and synced text highlighting.

Everything runs locally after setup. No API keys. No hosted TTS dependency.

## Screenshots

### Library
![Library](assets/library.png)

### Reader
![Reader](assets/reader.png)

### Voice Studio
![Voice Studio](assets/voice_studio.png)

### Settings
![Settings](assets/settings.png)

## Highlights

- Import EPUB, PDF, and TXT books.
- Detect chapters, prologues, epilogues, forewords, appendices, and parts automatically.
- Generate per-character voices with deterministic assignment.
- Customize each detected character in Voice Studio.
- Customize the narrator voice per book.
- Preview voices before saving.
- Upload reference WAV files for voice cloning.
- Invalidate stale cached playback automatically when narrator or character voices change.
- Export audio as WAV or MP3 and subtitles as ASS or SRT.
- Run from a project-local `.venv` created by `uv sync`.

## Requirements

- [uv](https://docs.astral.sh/uv/) (it downloads Python 3.11 itself, so no separate Python install is needed)
- `ffmpeg` on `PATH` for MP3 export and browser streaming
- OmniVoice model files stored locally
- Optional NVIDIA GPU for faster inference

## Installation

```bash
git clone https://github.com/nikhilprasanth/Auris.git
cd Auris/reader
bash setup.sh            # Linux / macOS         (Windows: setup.bat)
bash run.sh              # start it              (Windows: run.bat)
```

`setup` runs `uv sync`, which creates `.venv` from `uv.lock` (the app, PyTorch, OmniVoice, spaCy and its
English model). It picks the PyTorch build for the machine the first time (NVIDIA GPU found: CUDA 12.8; macOS:
PyPI's Apple Silicon build; otherwise CPU) and remembers it in `reader/.auris-extra`, so running `setup` again
after a `git pull` re-syncs with the same build. To choose yourself:

```bash
bash setup.sh --extra cu128     # cpu | cu128 | default (PyPI's own build)
```

Plain `uv sync --extra cu128` works too, but it is not remembered. `run` uses `uv run --no-sync`, so the
build you chose stays (a plain `uv run` would sync back to the default one).

## Model setup

The OmniVoice weights are not bundled with this repository.

You can either:

- Download them from the Settings page using the built-in Hugging Face downloader.
- Point Settings at an existing local OmniVoice model directory.

The model directory must contain the files OmniVoice expects, such as `config.json` and model weights.

## Usage

1. Import a book from the library page.
2. Open the book and start playback from any sentence.
3. Open Voice Studio from the reader sidebar.
4. Adjust character voices or the narrator voice, preview them, then save.
5. Export audio or subtitles if needed.

## Tests

The tests run without a browser, a TTS model or any real data (each test gets its own
temporary database and a fake engine):

```bash
cd reader
uv sync                  # includes pytest and pytest-playwright
uv run --no-sync pytest  # everything
```

The browser tests in `tests/browser` drive the real reader page in Chromium against a throwaway
server whose TTS engine makes tones, and print what each test cost (requests, bytes, TTS encodes,
main-thread time). They need a one-time `uv run --no-sync playwright install chromium`; without Playwright
installed they are skipped. `uv run --no-sync pytest tests/browser` runs just them.

They cover speaker detection on a short demo story (`tests/demo_story.py`), speaker
corrections and adding characters through the API, the audio cache, database
migrations, and Kokoro's helpers.

## Dev server

`reader/dev_server.py` runs the app against a scratch copy of your data, so you can try things
(import a book, correct speakers, change settings) without touching the real thing:

```bash
cd reader
.venv/bin/python dev_server.py                 # http://127.0.0.1:7862, TTS disabled
.venv/bin/python dev_server.py --real-engine   # with the real TTS engine
.venv/bin/python dev_server.py --refresh       # copy your real data again
```

The copy lives in `reader/.dev` (git-ignored). It is made the first time and **reused** on
later starts, so what you change there is still there next time; it does not follow your real
data afterwards, so use `--refresh` to catch up. The database is snapshotted safely even while
the real server is running; the audio cache and uploads are copy-on-write clones where the
filesystem supports it (APFS, btrfs, xfs), so they are instant and take no extra space, and plain
copies elsewhere. Before cloning, a self-test on throwaway files proves a clone really is
copy-on-write (a separate file, no real disk space used, and writing to it leaves the original alone).

Your database, settings, audio cache, uploads and exports can't be changed from the scratch copy,
and it checks that before starting: the scratch folder must not be inside your real data, no file
in it may be a symlink or hardlink, and every data location the app uses must be inside it. If any
check fails it refuses to start.

What it does **not** protect is anything outside the data folder: the TTS model (a model download
started from Settings writes to the model path), the Hugging Face cache used by `--real-engine`,
and the spaCy install button, which installs into your real Python environment.

Under the hood this is the `AURIS_DATA_DIR` environment variable, which moves the database,
settings, audio cache, uploads and exports under another folder with the same layout.

## Voice design caveats

OmniVoice does not produce clean output for every voice-design combination. The upstream docs note that some attribute mixes are unreliable, especially without reference audio.

The most fragile cases are youth voices with extreme pitch settings. For example, combinations like `male, teenager, very high pitch, american accent` can degrade into squeaks, bursts, or static instead of intelligible speech.

Auris now tries to stabilize some known-bad combinations during preview and playback by relaxing them to a nearby voice design, but this is still a model limitation, not something the UI can fully solve.

Best results:

- Prefer `young adult` over `teenager` when you do not have reference audio.
- Avoid `very high pitch` and `very low pitch` on `child` and `teenager` voices.
- Upload a clean WAV reference when you need a specific youthful voice.
- Preview before saving.

Reference: `https://github.com/k2-fsa/OmniVoice/blob/master/docs/voice-design.md`

## Offline installs

uv can install from a folder of wheels instead of the network:

```bash
uv sync --offline                                  # only what is already in uv's cache
uv sync --no-index --find-links /path/to/wheels    # only wheels from a folder
```

The spaCy English model is a locked dependency (a wheel URL), so it needs to be cached or in that folder too.

## Project structure

```text
Auris/
|-- README.md
|-- LICENSE
`-- reader/
    |-- app.py
    |-- pyproject.toml ← dependencies (uv); uv.lock pins them
    |-- setup.bat    ← Windows setup (uv sync with the right PyTorch build)
    |-- setup.sh     ← Linux / macOS setup
    |-- run.bat      ← Windows launcher
    |-- run.sh       ← Linux / macOS launcher
    |-- core/
    |-- static/
    |-- templates/
    `-- data/
```

## Main dependencies

- OmniVoice
- Flask
- ebooklib
- PyMuPDF
- spaCy
- pydub
- soundfile
- PyTorch

## Roadmap

### Small language model for emotion classification

The current enrichment pipeline uses regex patterns to decide which non-verbal tag (`[laughter]`, `[surprise-wa]`, `[question-ei]`, etc.) to inject before each TTS segment. It works well when attribution verbs are present in the text ("she gasped", "he scoffed"), but it cannot understand tone, irony, or context that isn't signalled by a keyword.

The plan is to connect to any OpenAI-compatible language model endpoint as an emotion classifier between parsing and TTS synthesis:

- **Connection:** a configurable base URL and API key in Settings, compatible with any OpenAI-spec server — local (Ollama, LM Studio, llama.cpp server) or remote. No runtime library bundled with Auris; the standard `openai` Python client is the only dependency.
- **Model candidates:** **Qwen3-0.8B** (fastest, lowest RAM), **Qwen3-2B** (better reasoning, still lightweight), **Gemma 4 E2B** (Google's 2B edge model), **LFM2.5-1.2B-Instruct** (Liquid AI — strong reasoning efficiency per parameter). Any model the user serves behind an OpenAI-compatible endpoint will work.
- **Input:** the current segment text plus one sentence of surrounding context.
- **Output:** a single tag from the supported set, or `none`. Structured output / JSON mode keeps latency low and parsing trivial.
- **Fallback:** the existing regex engine remains as a zero-latency fallback when no endpoint is configured or the model returns an invalid response.
- **Integration point:** `core/enrichment.py` — the `_select_expression_tag` function would be replaced by a call to the classifier, with the regex result used as a hint in the prompt.
- **UX:** base URL, API key, and model name are set in Settings. Leaving the base URL blank keeps regex-only mode active.

This would fix the main remaining gap: narration sentences that carry emotional weight without any keyword signal, and multi-emotion moments where the current system can only pick one tag.

### Pluggable TTS backends: Kokoro, Qwen-TTS, Fish Audio Pro

OmniVoice is the only synthesis backend today, wired directly into `core/tts_engine.py`. The plan is to pull the engine behind a small interface (`load`, `generate`, `status`) so Settings can pick a backend per install instead of assuming OmniVoice:

- **Kokoro TTS** — an ~82M-parameter open-weight model. Much lower VRAM/CPU cost than OmniVoice, making it the recommended default for machines without a GPU or with limited RAM. Trade-off: fewer voice-design controls, no reference-audio cloning.
- **Qwen-TTS** — Alibaba's TTS model family. Strong multilingual coverage, useful for books in languages OmniVoice handles poorly.
- **Fish Audio Pro** — Fish Speech's hosted/pro tier with high-fidelity voice cloning. Since this is a network-dependent option, it would be opt-in only and clearly flagged in Settings as breaking the "everything runs locally" guarantee the rest of the app holds to.
- **Integration point:** each backend implements the same interface consumed by `app.py` and Voice Studio, so per-character/per-narrator instruct strings, reference-audio upload, and the caching layer in `core/tts_engine.py` keep working unmodified. The model-download flow already built for OmniVoice (Settings → Hugging Face downloader, including the new auto-download-on-missing option) extends naturally to Kokoro and Qwen-TTS since both ship weights on Hugging Face.

### Docker support

Right now Auris only runs from a project-local `.venv` via `uv sync`. A `Dockerfile` (plus a `docker-compose.yml` for the common case of mounting a model directory and a data volume) would let users skip Python/CUDA setup entirely:

- **Base images:** a CPU image and a CUDA-enabled image (matching the `cpu` and `cu128` extras), so GPU inference still works via `--gpus all` / `nvidia-container-toolkit`.
- **Volumes:** `reader/data` (library + settings) and the OmniVoice model directory mounted from the host, so the container stays disposable while books, progress, and downloaded weights persist.
- **Networking:** the Flask dev server binds to `127.0.0.1` today; the container image would need to bind `0.0.0.0` behind an explicit opt-in flag, since this app has no auth layer and isn't meant to be exposed beyond localhost by default.
- **Model download inside the container:** the auto-download setting (Settings → OmniVoice Model) is the natural fit here — point a fresh container at an empty model volume and let it fetch weights on first boot instead of requiring a pre-populated mount.

## License

MIT. See [LICENSE](LICENSE).
