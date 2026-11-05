"""Capture one HTML slide as a video segment (or a PNG) with headless Chromium.

Run as a separate process, one per segment:

    python3 -I capture.py spec.json

It is a separate process on purpose: Playwright wants its own event loop, and a Chromium crash or
memory spike must not take the Odoo worker down with it.

The slide is never played in real time. Every frame is a screenshot taken after
`window.seek(t)` (video_templates/base.js) froze the CSS animations at time t.

Spec keys:
    html        absolute path of the rendered slide
    out         output path (.mp4, or .png when mode is "png")
    width, height
    mode        "video" (default) or "png"
    seek        png only: the moment to capture, in seconds
    fps         capture rate for the animated part
    duration    length of the segment in seconds
    play_until  seconds of animation to capture once
    loop_seconds  after play_until, this many seconds are captured and repeated until the segment
                ends (0 = hold the frame at play_until). Use it for endless motion whose period
                divides loop_seconds, such as the equalizer bars.
    audio       wav path, or null for silence
    audio_delay seconds of silence before the audio starts (default 0)
    ffmpeg      path of the ffmpeg binary
"""
import json
import subprocess
import sys
from pathlib import Path

AUDIO_RATE = 24000
OUTPUT_FPS = 30
CHROMIUM_ARGS = [
    '--no-sandbox',
    '--disable-gpu',
    '--disable-dev-shm-usage',
    '--allow-file-access-from-files',
    '--hide-scrollbars',
    '--force-device-scale-factor=1',
]


def _open_page(playwright, spec):
    browser = playwright.chromium.launch(args=CHROMIUM_ARGS)
    context = browser.new_context(viewport={'width': spec['width'], 'height': spec['height']})
    # The slides are generated from model output. They may only read local files.
    context.route('**/*', lambda route: route.continue_()
                  if route.request.url.startswith(('file:', 'data:')) else route.abort())
    page = context.new_page()
    page.goto(Path(spec['html']).as_uri())
    page.evaluate('document.fonts.ready')
    return browser, page


def _shoot(page, t, image_type='jpeg'):
    page.evaluate('t => window.seek(t)', t)
    if image_type == 'png':
        return page.screenshot(type='png')
    return page.screenshot(type='jpeg', quality=92)


def _ffmpeg_command(spec):
    cmd = [spec['ffmpeg'], '-y', '-loglevel', 'error',
           '-f', 'image2pipe', '-framerate', str(spec['fps']), '-c:v', 'mjpeg', '-i', '-']
    if spec.get('audio'):
        # audio_delay: seconds of silence before the audio starts (the hook plays first).
        delay_ms = int(round(float(spec.get('audio_delay') or 0) * 1000))
        audio_filter = ('adelay=%d:all=1,apad' % delay_ms) if delay_ms > 0 else 'apad'
        cmd += ['-i', spec['audio'], '-af', audio_filter]
    else:
        cmd += ['-f', 'lavfi', '-i', 'anullsrc=r=%d:cl=mono' % AUDIO_RATE]
    cmd += ['-map', '0:v', '-map', '1:a', '-t', '%.3f' % spec['duration'],
            '-vf', 'fps=%d,format=yuv420p' % OUTPUT_FPS,
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-tune', 'animation',
            '-c:a', 'aac', '-b:a', '128k', '-ar', str(AUDIO_RATE), '-ac', '1',
            spec['out']]
    return cmd


def capture_video(page, spec):
    fps = spec['fps']
    total = max(1, round(spec['duration'] * fps))
    play = min(total, max(1, round(spec['play_until'] * fps)))
    loop_count = 0 if play >= total else max(1, round(spec.get('loop_seconds', 0) * fps))

    log_path = Path(spec['out']).with_suffix('.log')
    with open(log_path, 'wb') as log:
        proc = subprocess.Popen(_ffmpeg_command(spec), stdin=subprocess.PIPE, stderr=log)
        try:
            for i in range(play):
                proc.stdin.write(_shoot(page, i / fps))
            loop_frames = [_shoot(page, spec['play_until'] + j / fps) for j in range(loop_count)]
            if play < total and not loop_frames:
                loop_frames = [_shoot(page, spec['play_until'])]
            for i in range(total - play):
                proc.stdin.write(loop_frames[i % len(loop_frames)])
            proc.stdin.close()
        except BrokenPipeError:
            pass  # ffmpeg stopped early; its exit code and log say why
        code = proc.wait()
    if code != 0:
        raise RuntimeError('ffmpeg failed (%d): %s' % (code, log_path.read_text(errors='replace')[-600:]))
    log_path.unlink(missing_ok=True)


def main(spec_path):
    spec = json.loads(Path(spec_path).read_text(encoding='utf-8'))
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser, page = _open_page(playwright, spec)
        try:
            if spec.get('mode') == 'png':
                Path(spec['out']).write_bytes(_shoot(page, spec.get('seek', 0), 'png'))
            else:
                capture_video(page, spec)
        finally:
            browser.close()


if __name__ == '__main__':
    try:
        main(sys.argv[1])
    except Exception as e:  # report to the parent process via stderr and the exit code
        print('capture failed: %s: %s' % (type(e).__name__, e), file=sys.stderr)
        sys.exit(1)
