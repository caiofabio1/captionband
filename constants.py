"""Shared constants for CaptionBand.

Centralizes "magic numbers" so they can be reviewed, tuned, and unit-tested
without hunting through implementation files. Everything that affects user-
visible timing or behavior should live here, not embedded in widgets or
providers.
"""
from __future__ import annotations

# ---------------------------------------------------------------------- App


APP_NAME = "CaptionBand"
APP_DISPLAY_NAME = "CaptionBand"
APP_VERSION = "0.12.0"  # keep in step with installer.iss and pyproject.toml
APP_GITHUB_REPO = "caiofabio1/captionband"


# ---------------------------------------------------------------------- Overlay timings (ms)


# Minimum time a caption stays visible before being replaced by a queued one.
# Below ~1.2s most readers can't finish; we use 1500ms as a balance.
MIN_DISPLAY_MS = 1500

# How long after the last event we clear the caption history (idle timeout).
# Long enough to bridge typical pauses, short enough to clear stale text.
IDLE_CLEAR_MS = 15_000

# Time window in which we ignore exact-duplicate originals (some providers
# emit the same final twice, especially during reconnection).
DEDUP_WINDOW_MS = 4000

# Fade + slide animation duration when a new caption appears.
ANIMATION_DURATION_MS = 280

# Animation refresh rate (33ms ≈ 30fps).
ANIMATION_TICK_MS = 33

# Auto-concatenate consecutive utterances if both:
#   - same detected language
#   - gap between them <= CONCAT_GAP_MS
# Set to 0 to disable concatenation entirely.
CONCAT_GAP_MS = 1500

# Maximum pending captions queued behind the currently displayed one. If a
# provider goes haywire and emits dozens of finals, we cap memory growth.
MAX_PENDING_CAPTIONS = 6


# ---------------------------------------------------------------------- Audio buffer


# Live-captioning best-practice defaults for the chunked audio buffer.
# Tweak in `_audio_buffer.py` only if the provider behaves differently.
AUDIO_FRAME_MS = 100              # frame granularity (also VAD smoothing window unit)
AUDIO_SILENCE_HANGOVER_MS = 500   # silence threshold to close a chunk
AUDIO_MIN_SPEECH_MS = 400         # below this we don't emit (saves cost)
AUDIO_MAX_CHUNK_S = 5.0           # hard cap on continuous speech chunk
AUDIO_PREROLL_MS = 200            # audio kept before speech onset
AUDIO_OVERLAP_MS = 300            # audio carried over on forced flush
AUDIO_RMS_SPEECH_THRESHOLD = 0.010
AUDIO_RMS_SMOOTHING_WINDOW = 3    # frames

# Default sample rate handed to providers.
AUDIO_SAMPLE_RATE = 16_000


# ---------------------------------------------------------------------- Provider behavior


# Number of consecutive failures before we trigger the fallback provider.
PROVIDER_FAILURE_THRESHOLD = 3

# Backoff window — failures must happen within this window to count.
PROVIDER_FAILURE_WINDOW_S = 60

# Number of parallel translation workers per cloud provider.
PROVIDER_TRANSLATION_WORKERS = 5

# Azure: silence (ms) that closes a phrase. Service default lets a fast
# presenter run several sentences into one late result; Microsoft's documented
# example for that case is 300 ms (learn.microsoft.com, how-to-recognize-speech,
# "Change how silence is handled", read 2026-09-15). 400 keeps a breath inside
# a sentence from splitting it.
AZURE_SEGMENTATION_SILENCE_MS = 400
# Do NOT raise this above 1000. Microsoft known issue 3002: "Customers
# experience random words being generated as part of Speech recognition results
# (hallucinations) when the SegmentationSilenceTimeout parameter is set to
# > 1,000 ms" (learn.microsoft.com/azure/ai-services/speech-service/known-issues,
# read 2026-09-15). Documented valid range is [100, 5000] ms.

# Azure: how stable a partial must be before the service sends it. Higher =
# less flicker, more latency. Microsoft's captioning guidance calls this the
# knob for "the 'flickering' or changing text"; their own captioning sample
# uses 5 (learn.microsoft.com/azure/ai-services/speech-service/captioning-concepts,
# read 2026-09-15). The projected translation was being rewritten on every
# partial, which is unreadable from the back of a room.
AZURE_STABLE_PARTIAL_THRESHOLD = 5

# Azure: at-start language identification accepts at most 4 candidate
# languages; continuous LID accepts 10 ("You can include up to four languages
# for at-start LID or up to 10 languages for continuous LID" —
# learn.microsoft.com/azure/ai-services/speech-service/language-identification,
# read 2026-09-15). Above this count the provider falls back to continuous.
AZURE_AT_START_LID_MAX_LANGUAGES = 4


# ---------------------------------------------------------------------- Transcript retention


# Maximum number of session files to keep (txt + srt counted as one session).
TRANSCRIPT_KEEP_SESSIONS = 50

# Delete sessions older than this regardless of count.
TRANSCRIPT_MAX_AGE_DAYS = 30

# Rewrite the .srt file(s) every N captions during the session. The .txt is
# line-buffered and survives a crash; the SRT used to exist only after a
# clean stop(), so a power cut at minute 110 lost the subtitle file entirely.
TRANSCRIPT_SRT_CHECKPOINT_EVERY = 20


# ---------------------------------------------------------------------- Update checker


# How often to check GitHub for new releases (per app launch).
# Set to 0 to disable, otherwise we check at most once every N seconds.
UPDATE_CHECK_INTERVAL_S = 24 * 3600  # once a day

# Timeout for the GitHub API call (we don't want to block startup).
UPDATE_CHECK_TIMEOUT_S = 4
