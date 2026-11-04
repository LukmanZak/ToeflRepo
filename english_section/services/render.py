"""Render the listening test as an MP4: audio plus question slides and an answer key at the end.

Timeline:
  1. Intro slide, shown for the whole audio length (+1 s)
  2. One slide per question, shown for `seconds_per_question`
  3. Answer key slide, shown for `answer_seconds`
"""
import shutil
import subprocess
import textwrap
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1280, 720
BG = (16, 42, 67)
FG = (255, 255, 255)
ACCENT = (255, 193, 7)
MUTED = (190, 210, 230)
MARGIN = 90

REGULAR_FONTS = [
    'C:/Windows/Fonts/arial.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
]
BOLD_FONTS = [
    'C:/Windows/Fonts/arialbd.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
]


class RenderError(Exception):
    pass


def _font(candidates, size):
    for path in candidates:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def wav_duration(path):
    with wave.open(str(path), 'rb') as w:
        return w.getnframes() / float(w.getframerate())


def _new_slide():
    img = Image.new('RGB', (WIDTH, HEIGHT), BG)
    return img, ImageDraw.Draw(img)


def _intro_slide(title):
    img, draw = _new_slide()
    title_font = _font(BOLD_FONTS, 64)
    sub_font = _font(REGULAR_FONTS, 36)
    lines = textwrap.wrap(title or 'Listening Section', width=32)
    y = 230
    for line in lines:
        draw.text((MARGIN, y), line, font=title_font, fill=FG)
        y += 80
    draw.text((MARGIN, y + 30), 'Listen to the recording.', font=sub_font, fill=ACCENT)
    draw.text((MARGIN, y + 80), 'Questions appear after the audio ends.', font=sub_font, fill=MUTED)
    return img


def _question_slide(index, total, question, options):
    img, draw = _new_slide()
    label_font = _font(BOLD_FONTS, 34)
    q_font = _font(REGULAR_FONTS, 38)
    opt_font = _font(REGULAR_FONTS, 32)
    draw.text((MARGIN, 50), 'Question %d of %d' % (index, total), font=label_font, fill=ACCENT)
    y = 130
    for line in textwrap.wrap(question, width=58):
        draw.text((MARGIN, y), line, font=q_font, fill=FG)
        y += 52
    y += 40
    for letter in 'ABCD':
        for i, line in enumerate(textwrap.wrap(options[letter], width=70)):
            prefix = '%s.  ' % letter if i == 0 else '     '
            draw.text((MARGIN, y), prefix + line, font=opt_font, fill=FG)
            y += 46
        y += 14
    return img


def _answer_slide(questions):
    img, draw = _new_slide()
    title_font = _font(BOLD_FONTS, 48)
    font = _font(REGULAR_FONTS, 32)
    draw.text((MARGIN, 40), 'Answer Key', font=title_font, fill=ACCENT)
    per_column = (len(questions) + 1) // 2
    for i, q in enumerate(questions):
        column = i // per_column
        row = i % per_column
        x = MARGIN + column * 560
        y = 140 + row * 44
        draw.text((x, y), '%d.' % (i + 1), font=font, fill=FG)
        draw.text((x + 110, y), q['answer'], font=font, fill=ACCENT)
    return img


def _run(cmd, cwd):
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, timeout=900)
    if proc.returncode != 0:
        raise RenderError('ffmpeg failed: %s' % proc.stderr[-800:].decode('utf-8', 'replace'))


def render_video(workdir, title, audio_path, questions, seconds_per_question=15, answer_seconds=20):
    """Build the MP4 in `workdir` and return its bytes."""
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RenderError('ffmpeg was not found on PATH.')
    workdir = Path(workdir)
    audio_path = Path(audio_path)
    audio_len = wav_duration(audio_path)
    total_q = len(questions)

    slides = []  # (image, seconds)
    slides.append((_intro_slide(title), audio_len + 1.0))
    for i, q in enumerate(questions, 1):
        slides.append((_question_slide(i, total_q, q['question'], q['options']), seconds_per_question))
    slides.append((_answer_slide(questions), answer_seconds))

    segments = []
    for i, (img, seconds) in enumerate(slides):
        png = workdir / ('slide%02d.png' % i)
        seg = 'seg%02d.mp4' % i
        img.save(png)
        _run([ffmpeg, '-y', '-loop', '1', '-i', png.name, '-t', '%.2f' % seconds,
              '-r', '2', '-c:v', 'libx264', '-tune', 'stillimage', '-preset', 'veryfast',
              '-pix_fmt', 'yuv420p', seg], workdir)
        segments.append(seg)

    (workdir / 'segments.txt').write_text(''.join("file '%s'\n" % s for s in segments), encoding='utf-8')
    _run([ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', 'segments.txt', '-c', 'copy', 'video.mp4'], workdir)

    total = sum(seconds for _, seconds in slides)
    _run([ffmpeg, '-y', '-i', 'video.mp4', '-i', audio_path.name,
          '-filter_complex', '[1:a]apad[a]', '-map', '0:v', '-map', '[a]',
          '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k', '-t', '%.2f' % total,
          # Move the index (moov atom) to the front so browsers can show duration and seek.
          '-movflags', '+faststart', 'final.mp4'], workdir)
    return (workdir / 'final.mp4').read_bytes()
