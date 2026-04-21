#!/usr/bin/env python3
"""
Audio Cleanup Tool — Remove breaths, pauses, filler words, and repeated content from audio.

Similar to 剪映's "删口癖" (filler removal) feature.

Usage:
    python3 audio_cleanup.py input.mp3 [--output output.mp3] [--language zh] [--report]
    python3 audio_cleanup.py input.mp3 --mode silence   # Only remove silences/pauses
    python3 audio_cleanup.py input.mp3 --mode filler     # Remove filler words + silences
    python3 audio_cleanup.py input.mp3 --mode full       # Full cleanup including repeats

Features:
    - Silence/pause removal (configurable threshold)
    - Breath sound detection and removal
    - Filler word detection (嗯、啊、那个、就是说、you know、um、uh, etc.)
    - Repeated phrase detection and deduplication
    - Smooth crossfade transitions to maintain natural flow
    - Supports Chinese (zh) and English (en)
"""

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pydub import AudioSegment
from pydub.silence import detect_nonsilent


@dataclass
class Segment:
    """A segment of transcribed audio."""
    start: float  # seconds
    end: float    # seconds
    text: str
    keep: bool = True
    removal_reason: str = ""


@dataclass
class CleanupReport:
    """Report of what was removed from the audio."""
    input_file: str = ""
    output_file: str = ""
    input_duration_sec: float = 0
    output_duration_sec: float = 0
    silences_removed: int = 0
    silence_duration_removed_sec: float = 0
    fillers_removed: int = 0
    filler_texts: list = field(default_factory=list)
    repeats_removed: int = 0
    repeat_texts: list = field(default_factory=list)
    breaths_removed: int = 0
    breath_duration_removed_sec: float = 0

    def summary(self) -> str:
        saved = self.input_duration_sec - self.output_duration_sec
        pct = (saved / self.input_duration_sec * 100) if self.input_duration_sec > 0 else 0
        lines = [
            f"=== Audio Cleanup Report ===",
            f"Input:  {self.input_file} ({self.input_duration_sec:.1f}s)",
            f"Output: {self.output_file} ({self.output_duration_sec:.1f}s)",
            f"Saved:  {saved:.1f}s ({pct:.1f}%)",
            f"",
            f"Silences removed: {self.silences_removed} ({self.silence_duration_removed_sec:.1f}s)",
            f"Breaths removed:  {self.breaths_removed} ({self.breath_duration_removed_sec:.1f}s)",
            f"Fillers removed:  {self.fillers_removed}",
        ]
        if self.filler_texts:
            lines.append(f"  Filler words: {', '.join(self.filler_texts[:20])}")
        lines.append(f"Repeats removed: {self.repeats_removed}")
        if self.repeat_texts:
            lines.append(f"  Repeated: {', '.join(self.repeat_texts[:10])}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "input_file": self.input_file,
            "output_file": self.output_file,
            "input_duration_sec": round(self.input_duration_sec, 1),
            "output_duration_sec": round(self.output_duration_sec, 1),
            "saved_sec": round(self.input_duration_sec - self.output_duration_sec, 1),
            "saved_pct": round((self.input_duration_sec - self.output_duration_sec) / self.input_duration_sec * 100, 1) if self.input_duration_sec > 0 else 0,
            "silences_removed": self.silences_removed,
            "fillers_removed": self.fillers_removed,
            "repeats_removed": self.repeats_removed,
            "breaths_removed": self.breaths_removed,
            "filler_texts": self.filler_texts[:20],
            "repeat_texts": self.repeat_texts[:10],
        }


# Filler words / phrases by language
FILLER_PATTERNS = {
    "zh": [
        "嗯", "啊", "呃", "额", "哦", "噢", "唔",
        "那个", "这个", "就是", "就是说", "然后呢", "然后",
        "对吧", "对不对", "是吧", "你知道吗", "怎么说呢",
        "反正就是", "基本上就是", "其实就是",
    ],
    "en": [
        "um", "uh", "er", "ah", "like",
        "you know", "i mean", "sort of", "kind of",
        "basically", "actually", "literally",
        "so yeah", "right", "okay so",
    ],
}

# Short breath/grunt patterns (typically < 0.3s with specific acoustic properties)
BREATH_MAX_DURATION_MS = 400
BREATH_MIN_SILENCE_BEFORE_MS = 100


def load_audio(filepath: str) -> AudioSegment:
    """Load audio file in any format supported by ffmpeg."""
    ext = Path(filepath).suffix.lower().lstrip(".")
    if ext in ("mp3", "wav", "ogg", "flac", "m4a", "aac", "wma", "webm"):
        return AudioSegment.from_file(filepath, format=ext)
    return AudioSegment.from_file(filepath)


def detect_breaths(audio: AudioSegment, report: CleanupReport) -> list[tuple[int, int]]:
    """
    Detect breath sounds — short bursts of noise between silent regions.
    Returns list of (start_ms, end_ms) for breath segments.
    """
    breaths = []
    # Find non-silent segments
    nonsilent = detect_nonsilent(audio, min_silence_len=BREATH_MIN_SILENCE_BEFORE_MS,
                                  silence_thresh=audio.dBFS - 16)
    for start_ms, end_ms in nonsilent:
        duration = end_ms - start_ms
        if duration <= BREATH_MAX_DURATION_MS:
            segment = audio[start_ms:end_ms]
            # Breaths tend to have lower RMS than speech
            if segment.dBFS < audio.dBFS - 6:
                breaths.append((start_ms, end_ms))
                report.breaths_removed += 1
                report.breath_duration_removed_sec += duration / 1000

    return breaths


def remove_silences(audio: AudioSegment, min_silence_ms: int = 700,
                     keep_silence_ms: int = 250, silence_thresh_db: int = -40,
                     report: Optional[CleanupReport] = None) -> AudioSegment:
    """
    Remove long silences, keeping a short gap for natural pacing.
    """
    nonsilent_ranges = detect_nonsilent(
        audio,
        min_silence_len=min_silence_ms,
        silence_thresh=silence_thresh_db,
    )

    if not nonsilent_ranges:
        return audio

    result = AudioSegment.empty()
    total_silence_removed = 0

    for i, (start_ms, end_ms) in enumerate(nonsilent_ranges):
        # Add padding around each non-silent chunk
        chunk_start = max(0, start_ms - keep_silence_ms)
        chunk_end = min(len(audio), end_ms + keep_silence_ms)
        chunk = audio[chunk_start:chunk_end]

        if i > 0:
            # Calculate how much silence we skipped
            prev_end = nonsilent_ranges[i - 1][1] + keep_silence_ms
            gap = start_ms - keep_silence_ms - prev_end
            if gap > 0:
                total_silence_removed += gap
                if report:
                    report.silences_removed += 1

        if len(result) > 0:
            result = result.append(chunk, crossfade=min(50, len(chunk) // 2))
        else:
            result = chunk

    if report:
        report.silence_duration_removed_sec = total_silence_removed / 1000

    return result


def transcribe_segments(filepath: str, language: str = "zh",
                         model_size: str = "medium") -> list[Segment]:
    """
    Transcribe audio using faster-whisper, returning word-level segments.
    """
    from faster_whisper import WhisperModel

    print(f"Loading Whisper model ({model_size})...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    print(f"Transcribing ({language})...")
    segments_iter, info = model.transcribe(
        filepath,
        language=language,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=300,
            speech_pad_ms=100,
        ),
    )

    segments = []
    for seg in segments_iter:
        if seg.words:
            for word in seg.words:
                segments.append(Segment(
                    start=word.start,
                    end=word.end,
                    text=word.word.strip(),
                ))
        else:
            segments.append(Segment(
                start=seg.start,
                end=seg.end,
                text=seg.text.strip(),
            ))

    print(f"Transcribed {len(segments)} segments")
    return segments


def detect_fillers(segments: list[Segment], language: str,
                    report: CleanupReport) -> list[Segment]:
    """
    Mark filler words for removal.
    """
    fillers = FILLER_PATTERNS.get(language, FILLER_PATTERNS["zh"])

    for seg in segments:
        text_lower = seg.text.lower().strip()
        for filler in fillers:
            if text_lower == filler or (len(text_lower) <= len(filler) + 2 and filler in text_lower):
                seg.keep = False
                seg.removal_reason = f"filler: {seg.text}"
                report.fillers_removed += 1
                report.filler_texts.append(seg.text)
                break

    return segments


@dataclass
class Sentence:
    """A sentence-level group of word segments."""
    start: float
    end: float
    text: str
    word_segments: list  # list of Segment
    keep: bool = True


def _group_into_sentences(segments: list[Segment], pause_threshold: float = 0.4) -> list[Sentence]:
    """
    Group word-level segments into sentence-level units based on pauses.
    A new sentence starts when the gap between words exceeds pause_threshold seconds.
    Lower threshold (0.4s) creates finer-grained groups for better repeat detection.
    """
    if not segments:
        return []

    kept = [s for s in segments if s.keep]
    if not kept:
        return []

    sentences = []
    current_words = [kept[0]]

    for i in range(1, len(kept)):
        gap = kept[i].start - kept[i - 1].end
        if gap >= pause_threshold:
            # Start new sentence
            text = "".join(w.text for w in current_words)
            sentences.append(Sentence(
                start=current_words[0].start,
                end=current_words[-1].end,
                text=text,
                word_segments=current_words[:],
            ))
            current_words = [kept[i]]
        else:
            current_words.append(kept[i])

    # Last sentence
    if current_words:
        text = "".join(w.text for w in current_words)
        sentences.append(Sentence(
            start=current_words[0].start,
            end=current_words[-1].end,
            text=text,
            word_segments=current_words[:],
        ))

    return sentences


def _text_similarity(a: str, b: str) -> float:
    """Compute similarity ratio between two strings using SequenceMatcher."""
    from difflib import SequenceMatcher
    # Normalize: remove punctuation and whitespace
    import re
    a_clean = re.sub(r'[^\w]', '', a)
    b_clean = re.sub(r'[^\w]', '', b)
    if not a_clean or not b_clean:
        return 0.0
    return SequenceMatcher(None, a_clean, b_clean).ratio()


def _is_prefix_retry(a: str, b: str, threshold: float = 0.6) -> bool:
    """
    Check if sentence 'a' is a failed attempt (prefix) of sentence 'b'.
    Common pattern: speaker says half a sentence, stops, restarts the full sentence.
    """
    import re
    a_clean = re.sub(r'[^\w]', '', a)
    b_clean = re.sub(r'[^\w]', '', b)
    if not a_clean or not b_clean:
        return False
    # Check if a is a prefix of b (allowing some variation)
    min_len = min(len(a_clean), len(b_clean))
    if min_len < 2:
        return False
    # a should be shorter or similar length to b
    if len(a_clean) > len(b_clean) * 1.3:
        return False
    # Check prefix overlap
    from difflib import SequenceMatcher
    match = SequenceMatcher(None, a_clean, b_clean[:len(a_clean)]).ratio()
    return match >= threshold


def _longest_common_substring(a: str, b: str) -> str:
    """Find the longest common substring between two strings."""
    if not a or not b:
        return ""
    m, n = len(a), len(b)
    # Optimize: use rolling approach for memory
    prev = [0] * (n + 1)
    best_len = 0
    best_end = 0
    for i in range(1, m + 1):
        curr = [0] * (n + 1)
        for j in range(1, n + 1):
            if a[i-1] == b[j-1]:
                curr[j] = prev[j-1] + 1
                if curr[j] > best_len:
                    best_len = curr[j]
                    best_end = i
            else:
                curr[j] = 0
        prev = curr
    return a[best_end - best_len:best_end]


def _find_phrase_repeats_in_pair(sent_a: 'Sentence', sent_b: 'Sentence',
                                  min_phrase_len: int = 4) -> Optional[str]:
    """
    Check if sentences A and B share a repeated phrase (≥ min_phrase_len chars).
    Returns the repeated phrase if found, None otherwise.
    This catches cases where a short phrase like "右边这张照片" repeats
    even if the surrounding sentence content differs.
    """
    import re
    a_clean = re.sub(r'[^\w]', '', sent_a.text)
    b_clean = re.sub(r'[^\w]', '', sent_b.text)
    if len(a_clean) < min_phrase_len or len(b_clean) < min_phrase_len:
        return None

    lcs = _longest_common_substring(a_clean, b_clean)
    if len(lcs) < min_phrase_len:
        return None

    # The repeated phrase should be a substantial portion of at least one sentence
    shorter = min(len(a_clean), len(b_clean))
    if len(lcs) >= shorter * 0.5:
        return lcs
    # Or be long enough on its own (≥6 chars for Chinese = ~3 words)
    if len(lcs) >= 6:
        return lcs

    return None


def detect_repeats(segments: list[Segment], report: CleanupReport,
                    window: int = 8) -> list[Segment]:
    """
    Detect repeated sentences/phrases (speaker retakes) and mark earlier attempts for removal.
    Keep the LAST occurrence — the speaker's final, cleanest delivery.

    Three-pass approach:
    1. Sentence-level: exact/near-exact repeats and prefix retries (similarity ≥ 0.55)
    2. Phrase-level: shared substring detection across nearby sentences
    3. Word-level: single-word stammering
    """
    sentences = _group_into_sentences(segments)
    print(f"  Grouped {len([s for s in segments if s.keep])} words into {len(sentences)} sentences")

    # Also try with a coarser grouping to catch repeats that span finer groups
    sentences_coarse = _group_into_sentences(segments, pause_threshold=0.8)
    print(f"  Coarse grouping: {len(sentences_coarse)} sentences (0.8s threshold)")

    # Pass 1: Detect sentence-level repeats and retakes (fine-grained)
    for i in range(len(sentences)):
        if not sentences[i].keep:
            continue

        for j in range(i + 1, min(i + window, len(sentences))):
            if not sentences[j].keep:
                continue

            # Check time proximity (retakes usually happen within 15 seconds)
            time_gap = sentences[j].start - sentences[i].end
            if time_gap > 15.0:
                break

            sim = _text_similarity(sentences[i].text, sentences[j].text)

            # Near-exact repeat: mark the earlier one for removal
            if sim >= 0.55:
                sentences[i].keep = False
                for ws in sentences[i].word_segments:
                    ws.keep = False
                    ws.removal_reason = f"repeat sentence ({sim:.0%}): {sentences[i].text[:30]}"
                report.repeats_removed += 1
                report.repeat_texts.append(f"{sentences[i].text[:40]}→{sentences[j].text[:40]}")
                print(f"  [REPEAT {sim:.0%}] Remove: '{sentences[i].text[:50]}' | Keep: '{sentences[j].text[:50]}'")
                break

            # Prefix retry: speaker said partial sentence, then restarted
            if _is_prefix_retry(sentences[i].text, sentences[j].text, threshold=0.5):
                sentences[i].keep = False
                for ws in sentences[i].word_segments:
                    ws.keep = False
                    ws.removal_reason = f"prefix retry: {sentences[i].text[:30]}"
                report.repeats_removed += 1
                report.repeat_texts.append(f"[prefix] {sentences[i].text[:40]}→{sentences[j].text[:40]}")
                print(f"  [PREFIX] Remove: '{sentences[i].text[:50]}' | Keep: '{sentences[j].text[:50]}'")
                break

            # Phrase-level repeat: shared substring detection
            repeated_phrase = _find_phrase_repeats_in_pair(sentences[i], sentences[j])
            if repeated_phrase:
                sentences[i].keep = False
                for ws in sentences[i].word_segments:
                    ws.keep = False
                    ws.removal_reason = f"phrase repeat: {repeated_phrase[:20]}"
                report.repeats_removed += 1
                report.repeat_texts.append(f"[phrase:{repeated_phrase[:15]}] {sentences[i].text[:30]}→{sentences[j].text[:30]}")
                print(f"  [PHRASE '{repeated_phrase[:20]}'] Remove: '{sentences[i].text[:50]}' | Keep: '{sentences[j].text[:50]}'")
                break

    # Pass 1b: Also run repeat detection on coarse grouping to catch wider-span repeats
    for i in range(len(sentences_coarse)):
        if not sentences_coarse[i].keep:
            continue
        # Check if any of its word segments were already removed
        if not any(ws.keep for ws in sentences_coarse[i].word_segments):
            sentences_coarse[i].keep = False
            continue

        for j in range(i + 1, min(i + 5, len(sentences_coarse))):
            if not sentences_coarse[j].keep:
                continue
            if not any(ws.keep for ws in sentences_coarse[j].word_segments):
                sentences_coarse[j].keep = False
                continue

            time_gap = sentences_coarse[j].start - sentences_coarse[i].end
            if time_gap > 15.0:
                break

            # Rebuild text from remaining kept words
            text_i = "".join(ws.text for ws in sentences_coarse[i].word_segments if ws.keep)
            text_j = "".join(ws.text for ws in sentences_coarse[j].word_segments if ws.keep)
            if not text_i or not text_j:
                continue

            sim = _text_similarity(text_i, text_j)
            if sim >= 0.55:
                for ws in sentences_coarse[i].word_segments:
                    if ws.keep:
                        ws.keep = False
                        ws.removal_reason = f"coarse repeat ({sim:.0%}): {text_i[:30]}"
                sentences_coarse[i].keep = False
                report.repeats_removed += 1
                report.repeat_texts.append(f"[coarse] {text_i[:40]}→{text_j[:40]}")
                print(f"  [COARSE {sim:.0%}] Remove: '{text_i[:50]}' | Keep: '{text_j[:50]}'")
                break

            # Phrase-level on coarse
            import re
            a_c = re.sub(r'[^\w]', '', text_i)
            b_c = re.sub(r'[^\w]', '', text_j)
            if len(a_c) >= 4 and len(b_c) >= 4:
                lcs = _longest_common_substring(a_c, b_c)
                shorter = min(len(a_c), len(b_c))
                if len(lcs) >= max(6, shorter * 0.5):
                    for ws in sentences_coarse[i].word_segments:
                        if ws.keep:
                            ws.keep = False
                            ws.removal_reason = f"coarse phrase: {lcs[:20]}"
                    sentences_coarse[i].keep = False
                    report.repeats_removed += 1
                    report.repeat_texts.append(f"[coarse-phrase:{lcs[:15]}] {text_i[:30]}→{text_j[:30]}")
                    print(f"  [COARSE-PHRASE '{lcs[:20]}'] Remove: '{text_i[:50]}' | Keep: '{text_j[:50]}'")
                    break

    # Pass 2: Catch single-word repeats (stammering)
    kept = [s for s in segments if s.keep]
    for i in range(len(kept) - 1):
        if not kept[i].keep:
            continue
        if kept[i].text == kept[i + 1].text:
            gap = kept[i + 1].start - kept[i].end
            if gap < 2.0:
                kept[i].keep = False
                kept[i].removal_reason = f"stammer: {kept[i].text}"
                report.repeats_removed += 1

    return segments


def rebuild_audio_ffmpeg(input_path: str, segments: list[Segment],
                         output_path: str, crossfade_ms: int = 30) -> float:
    """
    Rebuild audio using FFmpeg for precise cutting from the ORIGINAL file.
    This preserves audio quality by never re-encoding until the final output.

    Returns output duration in seconds.
    """
    import subprocess

    if not segments:
        return 0

    # Collect time ranges to keep
    keep_ranges = []
    for seg in segments:
        if seg.keep:
            start_ms = int(seg.start * 1000)
            end_ms = int(seg.end * 1000)
            start_ms = max(0, start_ms - 30)
            end_ms = end_ms + 30
            keep_ranges.append((start_ms, end_ms))

    if not keep_ranges:
        return 0

    # Merge overlapping/close ranges
    keep_ranges.sort()
    merged = [keep_ranges[0]]
    for start, end in keep_ranges[1:]:
        if start <= merged[-1][1] + 80:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    print(f"  Keeping {len(merged)} audio segments from original file")

    # Use FFmpeg concat demuxer approach for precise cutting
    tmp_dir = tempfile.mkdtemp(prefix="audio_cleanup_")
    segment_files = []

    try:
        # Cut each segment from original
        for i, (start_ms, end_ms) in enumerate(merged):
            seg_file = os.path.join(tmp_dir, f"seg_{i:04d}.wav")
            start_sec = start_ms / 1000
            duration_sec = (end_ms - start_ms) / 1000

            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", input_path,
                "-ss", f"{start_sec:.3f}",
                "-t", f"{duration_sec:.3f}",
                "-acodec", "pcm_s16le",
                seg_file,
            ]
            subprocess.run(cmd, check=True)
            segment_files.append(seg_file)

        # Create concat list
        concat_file = os.path.join(tmp_dir, "concat.txt")
        with open(concat_file, "w") as f:
            for sf in segment_files:
                f.write(f"file '{sf}'\n")

        # Concatenate all segments
        raw_output = os.path.join(tmp_dir, "concatenated.wav")
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-acodec", "pcm_s16le",
            raw_output,
        ]
        subprocess.run(cmd, check=True)

        # Apply crossfades and normalize using pydub for the final pass
        audio = AudioSegment.from_wav(raw_output)

        # Normalize audio levels
        from pydub.effects import normalize
        audio = normalize(audio)

        # Export final
        output_ext = Path(output_path).suffix.lower().lstrip(".")
        export_format = output_ext if output_ext in ("mp3", "wav", "ogg", "flac", "m4a") else "wav"
        audio.export(output_path, format=export_format)

        return len(audio) / 1000

    finally:
        # Cleanup temp files
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def rebuild_audio(audio: AudioSegment, segments: list[Segment],
                   crossfade_ms: int = 30) -> AudioSegment:
    """
    Rebuild audio by keeping only the marked segments, with smooth crossfades.
    Fallback method using pydub (used when FFmpeg method is not applicable).
    """
    if not segments:
        return audio

    # Collect time ranges to keep
    keep_ranges = []
    for seg in segments:
        if seg.keep:
            start_ms = int(seg.start * 1000)
            end_ms = int(seg.end * 1000)
            start_ms = max(0, start_ms - 20)
            end_ms = min(len(audio), end_ms + 20)
            keep_ranges.append((start_ms, end_ms))

    if not keep_ranges:
        return audio

    # Merge overlapping ranges
    keep_ranges.sort()
    merged = [keep_ranges[0]]
    for start, end in keep_ranges[1:]:
        if start <= merged[-1][1] + 50:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    # Build output
    result = AudioSegment.empty()
    for start_ms, end_ms in merged:
        chunk = audio[start_ms:end_ms]
        if len(result) > 0 and len(chunk) > crossfade_ms * 2:
            result = result.append(chunk, crossfade=crossfade_ms)
        else:
            result += chunk

    return result


def save_transcript(segments: list[Segment], filepath: str):
    """Save transcribed segments to JSON for external AI analysis."""
    data = []
    for i, seg in enumerate(segments):
        data.append({
            "idx": i,
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text,
            "keep": seg.keep,
            "removal_reason": seg.removal_reason,
        })
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Transcript saved: {filepath} ({len(data)} segments)")


def save_sentences(segments: list[Segment], filepath: str, language: str = "zh"):
    """
    Save sentence-level grouping for AI analysis.
    This is what gets sent to the LLM for retake detection.
    """
    sentences = _group_into_sentences(segments, pause_threshold=0.4)
    data = []
    for i, sent in enumerate(sentences):
        data.append({
            "id": i,
            "start": round(sent.start, 3),
            "end": round(sent.end, 3),
            "text": sent.text,
            "word_indices": [segments.index(ws) for ws in sent.word_segments if ws in segments],
        })
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Sentences saved: {filepath} ({len(data)} sentences)")
    return data


def apply_ai_retakes(segments: list[Segment], retake_ids: list[int],
                      sentences_data: list[dict], report: CleanupReport) -> list[Segment]:
    """
    Apply AI-identified retake sentence IDs to mark segments for removal.
    retake_ids: list of sentence IDs that should be removed (the earlier/bad takes).
    """
    sentences = _group_into_sentences(segments, pause_threshold=0.4)
    removed = 0
    for sent_id in retake_ids:
        if sent_id < 0 or sent_id >= len(sentences):
            print(f"  Warning: sentence ID {sent_id} out of range, skipping")
            continue
        sent = sentences[sent_id]
        if not sent.keep:
            continue
        sent.keep = False
        for ws in sent.word_segments:
            ws.keep = False
            ws.removal_reason = f"AI retake: sentence {sent_id}"
        removed += 1
        report.repeats_removed += 1
        report.repeat_texts.append(f"[AI] #{sent_id}: {sent.text[:40]}")
        print(f"  [AI RETAKE] Remove #{sent_id}: '{sent.text[:60]}'")

    print(f"  AI retakes applied: {removed} sentences removed")
    return segments


def save_intermediates(input_file: Path, segments: list[Segment],
                       sentences: list, remove_ids: set,
                       stage: str, report: CleanupReport):
    """
    Save detailed intermediate files for debugging and auditing.

    Outputs (saved to {input_stem}_intermediates/ directory):
      - {stage}_decisions.json: Per-sentence keep/remove decisions with reasons
      - {stage}_kept_segments.json: Time ranges of kept audio segments
      - {stage}_removed_segments.json: Time ranges of removed audio segments
      - {stage}_summary.txt: Human-readable summary
    """
    intermediates_dir = input_file.parent / f"{input_file.stem}_intermediates"
    intermediates_dir.mkdir(exist_ok=True)

    # 1. Per-sentence decisions
    decisions = []
    for i, sent in enumerate(sentences):
        kept_words = [s for s in sent.word_segments if s.keep]
        removed_words = [s for s in sent.word_segments if not s.keep]
        decisions.append({
            "sentence_id": i,
            "text": sent.text,
            "start": round(sent.start, 3),
            "end": round(sent.end, 3),
            "duration": round(sent.end - sent.start, 3),
            "decision": "KEEP" if i not in remove_ids else "REMOVE",
            "reason": "" if i not in remove_ids else "AI: fragment/repeat/incomplete",
            "word_count": len(sent.word_segments),
            "kept_words": len(kept_words),
        })
    decisions_path = intermediates_dir / f"{stage}_decisions.json"
    with open(decisions_path, "w", encoding="utf-8") as f:
        json.dump(decisions, f, ensure_ascii=False, indent=2)
    print(f"  Saved: {decisions_path}")

    # 2. Kept segments (time ranges)
    kept_ranges = []
    for seg in segments:
        if seg.keep:
            kept_ranges.append({
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": seg.text,
            })
    kept_path = intermediates_dir / f"{stage}_kept_segments.json"
    with open(kept_path, "w", encoding="utf-8") as f:
        json.dump(kept_ranges, f, ensure_ascii=False, indent=2)
    print(f"  Saved: {kept_path} ({len(kept_ranges)} segments)")

    # 3. Removed segments
    removed_ranges = []
    for seg in segments:
        if not seg.keep:
            removed_ranges.append({
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": seg.text,
                "reason": seg.removal_reason,
            })
    removed_path = intermediates_dir / f"{stage}_removed_segments.json"
    with open(removed_path, "w", encoding="utf-8") as f:
        json.dump(removed_ranges, f, ensure_ascii=False, indent=2)
    print(f"  Saved: {removed_path} ({len(removed_ranges)} segments)")

    # 4. Human-readable summary
    summary_path = intermediates_dir / f"{stage}_summary.txt"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"=== {stage} Stage Summary ===\n\n")
        f.write(f"Total sentences: {len(sentences)}\n")
        kept_count = sum(1 for d in decisions if d['decision'] == 'KEEP')
        removed_count = sum(1 for d in decisions if d['decision'] == 'REMOVE')
        f.write(f"Kept: {kept_count}\n")
        f.write(f"Removed: {removed_count}\n\n")
        f.write(f"--- Kept Sentences ---\n")
        for d in decisions:
            if d['decision'] == 'KEEP':
                f.write(f"  [{d['sentence_id']:3d}] ({d['start']:.1f}-{d['end']:.1f}s) {d['text']}\n")
        f.write(f"\n--- Removed Sentences ---\n")
        for d in decisions:
            if d['decision'] == 'REMOVE':
                f.write(f"  [{d['sentence_id']:3d}] ({d['start']:.1f}-{d['end']:.1f}s) {d['text']}\n")
                f.write(f"         Reason: {d['reason']}\n")
    print(f"  Saved: {summary_path}")

    return str(intermediates_dir)


def cleanup_audio(
    input_path: str,
    output_path: Optional[str] = None,
    language: str = "zh",
    mode: str = "full",
    model_size: str = "medium",
    min_silence_ms: int = 700,
    keep_silence_ms: int = 250,
    silence_thresh_db: int = -40,
    show_report: bool = True,
    transcribe_only: bool = False,
    ai_retakes_file: Optional[str] = None,
    save_intermediates_flag: bool = False,
) -> CleanupReport:
    """
    Main audio cleanup pipeline.

    Args:
        input_path: Path to input audio file
        output_path: Path for output (default: input_cleaned.ext)
        language: Language code (zh, en)
        mode: Cleanup mode — 'silence', 'filler', 'full', 'ai', or 'ai-only'
        model_size: Whisper model size (tiny, base, small, medium, large-v3)
        min_silence_ms: Minimum silence duration to remove (ms)
        keep_silence_ms: Silence to keep between segments (ms)
        silence_thresh_db: Silence threshold in dB
        show_report: Print cleanup report
        transcribe_only: Only transcribe and save transcript, don't rebuild
        ai_retakes_file: JSON file with AI-identified retake sentence IDs
        save_intermediates_flag: Save detailed intermediate files for debugging

    Modes:
        silence:  Only remove long silences/pauses
        filler:   Remove fillers + silences (rule-based)
        full:     Full cleanup with rule-based repeat detection + optional AI
        ai:       Rule-based detection + AI retakes (hybrid)
        ai-only:  AI-only mode — skip all rule-based detection, use only AI retakes

    Returns:
        CleanupReport with details of what was removed
    """
    input_file = Path(input_path)
    if not input_file.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    if output_path is None:
        output_path = str(input_file.parent / f"{input_file.stem}_cleaned{input_file.suffix}")

    report = CleanupReport(
        input_file=str(input_path),
        output_file=output_path,
    )

    # Check for cached transcript
    transcript_path = str(input_file.parent / f"{input_file.stem}_transcript.json")
    sentences_path = str(input_file.parent / f"{input_file.stem}_sentences.json")

    if mode == "silence":
        # Simple silence removal only
        print(f"Loading audio: {input_path}")
        audio = load_audio(input_path)
        report.input_duration_sec = len(audio) / 1000
        print(f"Duration: {report.input_duration_sec:.1f}s")
        print("Mode: silence removal only")
        result = remove_silences(audio, min_silence_ms, keep_silence_ms,
                                  silence_thresh_db, report)
        output_ext = Path(output_path).suffix.lower().lstrip(".")
        export_format = output_ext if output_ext in ("mp3", "wav", "ogg", "flac", "m4a") else "mp3"
        print(f"Exporting: {output_path}")
        result.export(output_path, format=export_format)
        report.output_duration_sec = len(result) / 1000
    else:
        # Try to load cached transcript
        segments = None
        if os.path.exists(transcript_path):
            print(f"Loading cached transcript: {transcript_path}")
            with open(transcript_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            segments = [Segment(start=d["start"], end=d["end"], text=d["text"]) for d in data]
            print(f"Loaded {len(segments)} segments from cache")

            # Get audio duration without loading full audio
            import subprocess
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", input_path],
                capture_output=True, text=True
            )
            report.input_duration_sec = float(result.stdout.strip())
        else:
            # Step 1: Transcribe
            print(f"Loading audio: {input_path}")
            audio = load_audio(input_path)
            report.input_duration_sec = len(audio) / 1000
            print(f"Duration: {report.input_duration_sec:.1f}s")

            print("Step 1: Transcribing audio with word-level timestamps...")
            segments = transcribe_segments(input_path, language, model_size)

            # Save transcript for caching/reuse
            save_transcript(segments, transcript_path)

        # Save sentences for AI analysis
        sentences_data = save_sentences(segments, sentences_path, language)

        if transcribe_only:
            print(f"\nTranscription complete. Sentences saved to: {sentences_path}")
            print("Next: Run AI analysis on the sentences file, then use --ai-retakes to apply.")
            return report

        if mode == "ai-only":
            # AI-only mode: skip all rule-based detection
            print("Mode: AI-only (no rule-based detection)")
            if not ai_retakes_file:
                print("ERROR: ai-only mode requires --ai-retakes file")
                print("First run with --transcribe-only, then analyze sentences with AI,")
                print("then run again with --mode ai-only --ai-retakes retakes.json")
                return report

            print(f"Step 2: Applying AI-identified retakes from {ai_retakes_file}...")
            with open(ai_retakes_file, "r", encoding="utf-8") as f:
                ai_data = json.load(f)
            retake_ids = ai_data if isinstance(ai_data, list) else ai_data.get("remove_ids", [])
            segments = apply_ai_retakes(segments, retake_ids, sentences_data, report)

            # Save intermediates if requested
            if save_intermediates_flag:
                print("Saving intermediate files...")
                sentences = _group_into_sentences(segments, pause_threshold=0.4)
                save_intermediates(input_file, segments, sentences,
                                   set(retake_ids), "ai_only", report)
        else:
            # Rule-based modes: filler, full, ai
            # Step 2: Detect and mark removals
            print("Step 2: Detecting fillers...")
            segments = detect_fillers(segments, language, report)

            if save_intermediates_flag:
                sentences = _group_into_sentences(segments, pause_threshold=0.4)
                filler_ids = {i for i, s in enumerate(sentences) if not s.keep}
                save_intermediates(input_file, segments, sentences,
                                   filler_ids, "after_filler_detection", report)

            if mode in ("full", "ai"):
                # Text-based repeat detection
                print("Step 2b: Text-based repeat detection...")
                segments = detect_repeats(segments, report)

                if save_intermediates_flag:
                    sentences = _group_into_sentences(segments, pause_threshold=0.4)
                    repeat_ids = {i for i, s in enumerate(sentences) if not s.keep}
                    save_intermediates(input_file, segments, sentences,
                                       repeat_ids, "after_repeat_detection", report)

            # Apply AI retakes if provided
            if ai_retakes_file:
                print(f"Step 2c: Applying AI-identified retakes from {ai_retakes_file}...")
                with open(ai_retakes_file, "r", encoding="utf-8") as f:
                    ai_data = json.load(f)
                retake_ids = ai_data if isinstance(ai_data, list) else ai_data.get("remove_ids", [])
                segments = apply_ai_retakes(segments, retake_ids, sentences_data, report)

                if save_intermediates_flag:
                    sentences = _group_into_sentences(segments, pause_threshold=0.4)
                    all_removed = {i for i, s in enumerate(sentences) if not s.keep}
                    save_intermediates(input_file, segments, sentences,
                                       all_removed, "after_ai_retakes", report)

        # Step 3: Rebuild audio
        print("Step 3: Rebuilding audio with FFmpeg (precise cutting)...")
        output_duration = rebuild_audio_ffmpeg(input_path, segments, output_path)
        report.output_duration_sec = output_duration

    if show_report:
        print()
        print(report.summary())

    # Save report JSON if intermediates requested
    if save_intermediates_flag:
        intermediates_dir = input_file.parent / f"{input_file.stem}_intermediates"
        intermediates_dir.mkdir(exist_ok=True)
        report_path = intermediates_dir / "final_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
        print(f"Report saved: {report_path}")

    return report


def main():
    parser = argparse.ArgumentParser(
        description="Audio Cleanup — Remove breaths, pauses, filler words, and repeated content",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s recording.mp3                          # Full cleanup (Chinese)
  %(prog)s recording.mp3 --language en             # Full cleanup (English)
  %(prog)s recording.mp3 --mode silence            # Only remove pauses
  %(prog)s recording.mp3 --mode filler             # Remove fillers + pauses
  %(prog)s recording.mp3 -o clean.mp3 --report     # Save report as JSON
  %(prog)s recording.mp3 --model-size large-v3     # Use larger model for accuracy
  %(prog)s recording.mp3 --transcribe-only         # Only transcribe (saves sentences JSON)
  %(prog)s recording.mp3 --ai-retakes retakes.json # Apply AI-detected retakes (hybrid)
  %(prog)s recording.mp3 --mode ai-only --ai-retakes retakes.json  # AI-only mode
  %(prog)s recording.mp3 --mode ai-only --ai-retakes r.json --save-intermediates  # With debug output
        """,
    )
    parser.add_argument("input", help="Input audio file path")
    parser.add_argument("-o", "--output", help="Output file path (default: input_cleaned.ext)")
    parser.add_argument("-l", "--language", default="zh", choices=["zh", "en"],
                        help="Language (default: zh)")
    parser.add_argument("-m", "--mode", default="full",
                        choices=["silence", "filler", "full", "ai", "ai-only"],
                        help="Cleanup mode (default: full). ai-only skips all rules, uses only AI retakes")
    parser.add_argument("--model-size", default="medium",
                        choices=["tiny", "base", "small", "medium", "large-v3"],
                        help="Whisper model size (default: medium)")
    parser.add_argument("--min-silence", type=int, default=700,
                        help="Min silence duration to remove in ms (default: 700)")
    parser.add_argument("--keep-silence", type=int, default=250,
                        help="Silence to keep between segments in ms (default: 250)")
    parser.add_argument("--silence-thresh", type=int, default=-40,
                        help="Silence threshold in dB (default: -40)")
    parser.add_argument("--report", action="store_true",
                        help="Save cleanup report as JSON")
    parser.add_argument("--transcribe-only", action="store_true",
                        help="Only transcribe and save transcript/sentences (no rebuild)")
    parser.add_argument("--ai-retakes", metavar="FILE",
                        help="JSON file with AI-identified retake sentence IDs to remove")
    parser.add_argument("--save-intermediates", action="store_true",
                        help="Save all intermediate files for debugging/auditing")

    args = parser.parse_args()

    report = cleanup_audio(
        input_path=args.input,
        output_path=args.output,
        language=args.language,
        mode=args.mode,
        model_size=args.model_size,
        min_silence_ms=args.min_silence,
        keep_silence_ms=args.keep_silence,
        silence_thresh_db=args.silence_thresh,
        transcribe_only=args.transcribe_only,
        ai_retakes_file=args.ai_retakes,
        save_intermediates_flag=args.save_intermediates,
    )

    if args.report:
        report_path = Path(args.output or args.input).with_suffix(".report.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
        print(f"\nReport saved: {report_path}")


if __name__ == "__main__":
    main()
