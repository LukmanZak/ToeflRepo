"""Render a YouTube Short (1080x1920, at most 90 s) from the HTML templates in video_templates/.

Each slide is rendered with Jinja2 into a work folder, then services/capture.py turns it into a
short MP4 segment with headless Chromium + ffmpeg (one subprocess per segment, a few at a time).
The segments are joined without re-encoding.

Timeline of one Short (the host narrates every part; each slide is as long as its narration needs):
  1. listening: the host says the hook, then the topic ("materi") appears and the recording plays
  2. for every question: the host reads the question, then the countdown; then the answer, which the
     host announces with a short spoken explanation
  3. outro: "follow akun ini, next kita latihan lagi"

To change how a slide looks or what it says, edit the HTML in video_templates/.
"""
import json
import re
import shutil
import subprocess
import sys
import wave
from array import array
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / 'video_templates'
CAPTURE_SCRIPT = Path(__file__).with_name('capture.py')

WIDTH, HEIGHT = 1080, 1920

MIN_SECONDS_PER_QUESTION = 5
MAX_SECONDS_PER_QUESTION = 20
SHRINK_FLOOR = 6              # with 2+ questions, shorten the countdown down to this before dropping a question
QUESTION_LEAD_SECONDS = 2.2   # the options slide in first; the countdown starts after this at the earliest
QUESTION_AUDIO_START = 0.3    # the host starts reading the question this long after the slide appears
HOOK_AUDIO_START = 0.2        # the host starts the hook this long after the video starts
MIN_RECORDING_DELAY = 2.0     # the recording never starts before this
AUDIO_TAIL = 1.0              # short pause after the recording ends
ANSWER_AUDIO_START = 0.5      # the answer lights up first, then the host speaks
MIN_ANSWER_SECONDS, MAX_ANSWER_SECONDS = 6.0, 14.0
OUTRO_AUDIO_START = 0.4
MIN_OUTRO_SECONDS = 3.0
MAX_TOTAL_SECONDS = 88.0      # YouTube Shorts limit is 90 s; keep a margin
LEVEL_FPS = 30
MAX_HOOK_WORDS = 14
MAX_WORKERS = 3               # Chromium instances running at the same time
SEGMENT_TIMEOUT = 900
SPEAKER_LABELS = {'toefl': {'Lecturer': 'Lecturer', 'Student': 'Student'},
                  'school': {'Lecturer': 'Teacher', 'Student': 'Student'}}


class RenderError(Exception):
    pass


def wav_duration(path):
    with wave.open(str(path), 'rb') as w:
        return w.getnframes() / float(w.getframerate())


def read_pcm(path):
    """(sample rate, raw 16-bit mono PCM) of a WAV file."""
    with wave.open(str(path), 'rb') as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise RenderError('Only 16-bit mono WAV audio is supported.')
        return w.getframerate(), w.readframes(w.getnframes())


def mix_listening_audio(hook_path, recording_path, recording_delay, out_path):
    """One WAV for the listening slide: the host's hook, then the recording at `recording_delay` seconds."""
    rate, hook = read_pcm(hook_path)
    rate2, recording = read_pcm(recording_path)
    if rate != rate2:
        raise RenderError('The hook and the recording have different sample rates.')
    lead = bytes(2 * int(rate * HOOK_AUDIO_START))
    gap_seconds = recording_delay - HOOK_AUDIO_START - len(hook) / (2.0 * rate)
    gap = bytes(2 * int(rate * max(0.0, gap_seconds)))
    with wave.open(str(out_path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(lead + hook + gap + recording)


def audio_levels(path, fps=LEVEL_FPS):
    """Loudness of a WAV as a list of 0..1 values, one per 1/fps second (what the slide animates).

    RMS per window, scaled so the loud parts reach about 1, silence stays 0, with a fast attack and
    a slower release so the motion looks natural.
    """
    with wave.open(str(path), 'rb') as w:
        if w.getsampwidth() != 2:
            raise RenderError('Only 16-bit WAV audio is supported.')
        channels, rate = w.getnchannels(), w.getframerate()
        samples = array('h')
        samples.frombytes(w.readframes(w.getnframes()))
    if channels > 1:
        samples = samples[::channels]
    window = max(1, int(round(rate / float(fps))))
    rms = []
    for start in range(0, len(samples), window):
        chunk = samples[start:start + window]
        rms.append((sum(s * s for s in chunk) / float(len(chunk))) ** 0.5 if chunk else 0.0)
    loud = sorted(v for v in rms if v > 0)
    if not loud:
        return [0.0] * len(rms)
    ref = loud[int(len(loud) * 0.95) - 1 if len(loud) >= 20 else -1]
    gate = ref * 0.04
    out, prev = [], 0.0
    for v in rms:
        target = 0.0 if v < gate else min(1.0, v / ref) ** 0.85
        prev = prev + (target - prev) * (0.6 if target > prev else 0.35)
        out.append(round(prev if prev > 0.01 else 0.0, 3))
    return out


def hook_words(hook):
    """Split the hook into [{'t': word, 'hl': bool}]. Words inside *stars* get the highlight."""
    words = []
    for match in re.finditer(r'\*([^*]+)\*|(\S+)', hook or ''):
        if match.group(1):
            words += [{'t': w, 'hl': True} for w in match.group(1).split()]
        else:
            word = match.group(2).replace('*', '')
            if word:
                words.append({'t': word, 'hl': False})
    return words[:MAX_HOOK_WORDS]


# ---- timing: every duration follows the length of the narration audio ----

def recording_delay(hook_seconds):
    """Seconds into the video when the recording starts (after the host has said the hook)."""
    return max(MIN_RECORDING_DELAY, HOOK_AUDIO_START + hook_seconds + 0.5)


def question_lead(question_seconds):
    """Seconds before the countdown starts: the host reads the question first."""
    return max(QUESTION_LEAD_SECONDS, QUESTION_AUDIO_START + question_seconds + 0.5)


def answer_seconds(answer_audio_seconds):
    return min(MAX_ANSWER_SECONDS, max(MIN_ANSWER_SECONDS, ANSWER_AUDIO_START + answer_audio_seconds + 1.2))


def outro_seconds(outro_audio_seconds):
    return max(MIN_OUTRO_SECONDS, OUTRO_AUDIO_START + outro_audio_seconds + 0.8)


def total_seconds(hook_s, audio_s, questions, seconds_per_question, outro_s):
    """`questions` = [(question narration seconds, answer narration seconds)]."""
    return (recording_delay(hook_s) + audio_s + AUDIO_TAIL
            + sum(question_lead(q) + seconds_per_question + answer_seconds(a) for q, a in questions)
            + outro_seconds(outro_s))


def fit_budget(hook_s, audio_s, questions, seconds_per_question, outro_s):
    """Return (countdown seconds, number of questions to keep) so the Short stays under the limit.

    First the countdown is shortened (down to SHRINK_FLOOR while there are 2+ questions), then the
    last question is dropped. Raises RenderError only when even one question with the shortest
    countdown does not fit.
    """
    seconds = max(MIN_SECONDS_PER_QUESTION, min(MAX_SECONDS_PER_QUESTION, seconds_per_question))
    for keep in range(len(questions), 0, -1):
        floor = SHRINK_FLOOR if keep > 1 else MIN_SECONDS_PER_QUESTION
        trial = max(floor, seconds) if seconds >= floor else seconds
        while True:
            if total_seconds(hook_s, audio_s, questions[:keep], trial, outro_s) <= MAX_TOTAL_SECONDS:
                return trial, keep
            if trial <= floor:
                break
            trial -= 1
    total = total_seconds(hook_s, audio_s, questions[:1], MIN_SECONDS_PER_QUESTION, outro_s)
    raise RenderError('The Short would be %.0f s, over the %.0f s limit, even with one question and a %d s '
                      'countdown. The recording (%.0f s) is too long.'
                      % (total, MAX_TOTAL_SECONDS, MIN_SECONDS_PER_QUESTION, audio_s))


def _environment():
    return Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)),
                       autoescape=select_autoescape(['html']), trim_blocks=True, lstrip_blocks=True)


def _prepare_workdir(workdir):
    """Copy css/js/fonts next to the rendered slides so their relative links work."""
    workdir = Path(workdir)
    for source in TEMPLATE_DIR.rglob('*'):
        if source.is_file() and source.suffix != '.html' and source.name != 'OFL.txt':
            target = workdir / source.relative_to(TEMPLATE_DIR)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)


def _write_slide(workdir, name, template, context):
    path = Path(workdir) / ('%s.html' % name)
    path.write_text(_environment().get_template(template).render(**context), encoding='utf-8')
    return path


def _ffmpeg():
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RenderError('ffmpeg was not found on PATH.')
    return ffmpeg


def _run_capture(spec_path):
    proc = subprocess.run([sys.executable, '-I', str(CAPTURE_SCRIPT), str(spec_path)],
                          capture_output=True, timeout=SEGMENT_TIMEOUT)
    if proc.returncode != 0:
        raise RenderError('Slide render failed (%s): %s' % (
            Path(spec_path).stem, proc.stderr[-800:].decode('utf-8', 'replace')))


def _write_spec(workdir, name, slide_html, **spec):
    spec.update({
        'html': str(slide_html),
        'out': str(Path(workdir) / ('%s.mp4' % name)),
        'width': WIDTH,
        'height': HEIGHT,
        'ffmpeg': _ffmpeg(),
    })
    spec_path = Path(workdir) / ('%s.json' % name)
    spec_path.write_text(json.dumps(spec), encoding='utf-8')
    return spec_path, spec['out']


def _build_segments(workdir, clip, seconds_per_question, mode):
    """Render every slide to HTML and return [(spec path, segment path)] in video order.

    `clip` = {'hook', 'title', 'audio_path', 'hook_audio', 'outro_audio', 'timeline', 'questions': [...]};
    every question also has 'question_audio' and 'answer_audio' (the host's narration, WAV paths).
    """
    audio = Path(clip['audio_path'])
    audio_seconds = wav_duration(audio)
    hook_seconds = wav_duration(clip['hook_audio'])
    delay = recording_delay(hook_seconds)
    labels = SPEAKER_LABELS.get(mode, SPEAKER_LABELS['toefl'])
    used = {t['speaker'] for t in clip['timeline']}
    speakers = [{'role': r, 'label': labels[r]} for r in ('Lecturer', 'Student') if r in used]
    segments = []

    def add(name, template, context, **spec):
        slide = _write_slide(workdir, name, template, context)
        segments.append(_write_spec(workdir, name, slide, **spec))

    words = hook_words(clip['hook'])
    # The words pop in while the host speaks the hook; the hook leaves just after the recording starts.
    word_step = min(0.3, max(0.12, (hook_seconds * 0.8) / max(1, len(words))))
    mix = Path(workdir) / 'listening_mix.wav'
    mix_listening_audio(clip['hook_audio'], audio, delay, mix)
    listening_seconds = delay + audio_seconds + AUDIO_TAIL
    add('s000_listening', 'listening.html',
        {'hook_words': words, 'word_step': round(word_step, 3), 'hook_end': round(delay + 0.3, 2),
         'clip_title': clip['title'], 'speakers': speakers,
         'levels': audio_levels(audio), 'timeline': clip['timeline'], 'level_fps': LEVEL_FPS,
         'delay': round(delay, 3), 'audio_seconds': round(audio_seconds, 3)},
        fps=30, duration=listening_seconds, play_until=listening_seconds, loop_seconds=0,
        audio=str(mix), audio_delay=0)

    for q in clip['questions']:
        q_seconds = wav_duration(q['question_audio'])
        a_seconds = wav_duration(q['answer_audio'])
        lead = question_lead(q_seconds)
        add('s%03d_question' % len(segments), 'question.html',
            {'question': q['question'], 'options': q['options'],
             'seconds': seconds_per_question, 'lead': round(lead, 2)},
            fps=20, duration=lead + seconds_per_question,
            play_until=lead + seconds_per_question, loop_seconds=0,
            audio=str(q['question_audio']), audio_delay=QUESTION_AUDIO_START)
        add('s%03d_answer' % len(segments), 'answer.html',
            {'question': q['question'], 'options': q['options'], 'answer': q['answer'],
             'explanation_id': q['explanation_id'], 'explanation_en': q['explanation_en']},
            fps=15, duration=answer_seconds(a_seconds), play_until=3.0, loop_seconds=0,
            audio=str(q['answer_audio']), audio_delay=ANSWER_AUDIO_START)

    add('s%03d_outro' % len(segments), 'outro.html', {},
        fps=15, duration=outro_seconds(wav_duration(clip['outro_audio'])), play_until=3.0, loop_seconds=0,
        audio=str(clip['outro_audio']), audio_delay=OUTRO_AUDIO_START)
    return segments


def render_video(workdir, clip, seconds_per_question=10, mode='toefl', on_progress=None):
    """Build the MP4 in `workdir`. Returns (mp4 bytes, number of questions kept).

    `clip` is {'hook', 'title', 'audio_path', 'hook_audio', 'outro_audio', 'timeline': [{speaker, start,
    end}], 'questions': [{'question', 'options', 'answer', 'explanation_id', 'explanation_en',
    'question_audio', 'answer_audio'}]}.
    If the Short would be too long the countdown is shortened, and if that is not enough the last
    question is left out (the caller gets the count so it can store only the questions that are shown).
    `on_progress(done, total)` is called from the calling thread.
    """
    workdir = Path(workdir)
    questions = clip['questions']
    seconds_per_question, keep = fit_budget(
        wav_duration(clip['hook_audio']), wav_duration(clip['audio_path']),
        [(wav_duration(q['question_audio']), wav_duration(q['answer_audio'])) for q in questions],
        seconds_per_question, wav_duration(clip['outro_audio']))
    clip = dict(clip, questions=questions[:keep])
    _prepare_workdir(workdir)
    segments = _build_segments(workdir, clip, seconds_per_question, mode)

    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_run_capture, spec_path) for spec_path, _out in segments]
        try:
            for future in as_completed(futures):
                future.result()
                done += 1
                if on_progress:
                    on_progress(done, len(segments))
        except BaseException:
            for future in futures:
                future.cancel()
            raise

    listing = workdir / 'segments.txt'
    listing.write_text(''.join("file '%s'\n" % Path(out).name for _spec, out in segments), encoding='utf-8')
    # Move the index (moov atom) to the front so browsers can show duration and seek.
    proc = subprocess.run([_ffmpeg(), '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0',
                           '-i', listing.name, '-c', 'copy', '-movflags', '+faststart', 'final.mp4'],
                          cwd=str(workdir), capture_output=True, timeout=SEGMENT_TIMEOUT)
    if proc.returncode != 0:
        raise RenderError('ffmpeg failed: %s' % proc.stderr[-800:].decode('utf-8', 'replace'))
    return (workdir / 'final.mp4').read_bytes(), keep
