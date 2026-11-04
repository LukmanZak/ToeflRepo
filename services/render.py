"""Render the listening test as an MP4 from the HTML templates in video_templates/.

Each slide is rendered with Jinja2 into a work folder, then services/capture.py turns it into a
short MP4 segment with headless Chromium + ffmpeg (one subprocess per segment, a few at a time).
The segments are joined without re-encoding.

Timeline:
  1. Intro card
  2. For every recording: title slide with the audio (audio length + 1 s), then for every question:
     the question with its countdown, and right after it the answer with an explanation
  3. Answer key, then the outro

To change how a slide looks or what it says, edit the HTML in video_templates/.
"""
import json
import shutil
import subprocess
import sys
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / 'video_templates'
CAPTURE_SCRIPT = Path(__file__).with_name('capture.py')

WIDTH, HEIGHT = 1920, 1080
THUMBNAIL_SIZE = (1280, 720)

MIN_SECONDS_PER_QUESTION = 5
MAX_SECONDS_PER_QUESTION = 120
QUESTION_LEAD_SECONDS = 2.2   # the options slide in first; the countdown starts after this
INTRO_SECONDS = 5.0
OUTRO_SECONDS = 6.0
ANSWER_KEY_SECONDS = 20.0
MAX_WORKERS = 3               # Chromium instances running at the same time
SEGMENT_TIMEOUT = 900


class RenderError(Exception):
    pass


def wav_duration(path):
    with wave.open(str(path), 'rb') as w:
        return w.getnframes() / float(w.getframerate())


def explanation_seconds(text):
    """Reading time for the explanation slide, from its length."""
    return min(20.0, max(8.0, len((text or '').split()) / 2.8 + 3))


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


def _build_segments(workdir, title, theme, clips, seconds_per_question):
    """Render every slide to HTML and return [(spec path, segment path)] in video order."""
    total_q = sum(len(c['questions']) for c in clips)
    segments = []

    def add(name, template, context, **spec):
        slide = _write_slide(workdir, name, template, context)
        segments.append(_write_spec(workdir, name, slide, **spec))

    add('s000_intro', 'intro.html',
        {'title': title or 'Listening Section', 'theme': theme, 'question_count': total_q,
         'clip_count': len(clips)},
        fps=24, duration=INTRO_SECONDS, play_until=4.0, loop_seconds=0, audio=None)

    number = 0
    answers = []
    lead = QUESTION_LEAD_SECONDS
    for clip_number, clip in enumerate(clips, 1):
        audio = Path(clip['audio_path'])
        add('s%03d_clip' % (len(segments)), 'clip.html',
            {'number': clip_number, 'total': len(clips), 'clip_title': clip['title'],
             'question_count': len(clip['questions'])},
            fps=12, duration=wav_duration(audio) + 1.0, play_until=1.5, loop_seconds=2.0,
            audio=str(audio))
        for q in clip['questions']:
            number += 1
            answers.append({'number': number, 'letter': q['answer']})
            add('s%03d_question' % len(segments), 'question.html',
                {'number': number, 'total': total_q, 'clip_number': clip_number,
                 'clip_total': len(clips), 'question': q['question'], 'options': q['options'],
                 'seconds': seconds_per_question, 'lead': lead},
                fps=12, duration=lead + seconds_per_question,
                play_until=lead + seconds_per_question, loop_seconds=0, audio=None)
            add('s%03d_explanation' % len(segments), 'explanation.html',
                {'number': number, 'total': total_q, 'question': q['question'],
                 'options': q['options'], 'answer': q['answer'],
                 'explanation': q.get('explanation', '')},
                fps=15, duration=explanation_seconds(q.get('explanation')), play_until=3.0,
                loop_seconds=0, audio=None)

    add('s%03d_answers' % len(segments), 'answer_key.html', {'answers': answers},
        fps=15, duration=ANSWER_KEY_SECONDS, play_until=0.7 + 0.08 * len(answers) + 0.6,
        loop_seconds=0, audio=None)
    add('s%03d_outro' % len(segments), 'outro.html', {},
        fps=15, duration=OUTRO_SECONDS, play_until=2.5, loop_seconds=1.0, audio=None)
    return segments


def render_video(workdir, title, theme, clips, seconds_per_question=25, on_progress=None):
    """Build the MP4 in `workdir` and return its bytes.

    `clips` is a list of {'title', 'audio_path', 'questions': [{'question', 'options', 'answer',
    'explanation'}]}. `on_progress(done, total)` is called from the calling thread.
    """
    workdir = Path(workdir)
    _prepare_workdir(workdir)
    segments = _build_segments(workdir, title, theme, clips, seconds_per_question)

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
    return (workdir / 'final.mp4').read_bytes()


def render_thumbnail(workdir, title, theme, question_count):
    """Return a 1280x720 PNG for the YouTube thumbnail."""
    workdir = Path(workdir)
    _prepare_workdir(workdir)
    slide = _write_slide(workdir, 'thumbnail', 'thumbnail.html',
                         {'title': title or 'Listening Section', 'theme': theme,
                          'question_count': question_count})
    out = workdir / 'thumbnail.png'
    spec_path = workdir / 'thumbnail.json'
    spec_path.write_text(json.dumps({
        'html': str(slide), 'out': str(out), 'mode': 'png', 'seek': 5.0,
        'width': THUMBNAIL_SIZE[0], 'height': THUMBNAIL_SIZE[1], 'ffmpeg': _ffmpeg(),
    }), encoding='utf-8')
    _run_capture(spec_path)
    return out.read_bytes()
