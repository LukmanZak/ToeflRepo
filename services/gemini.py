"""Gemini client for the English Section Shorts.

Text uses the REST API with urllib. Speech uses the Gemini Live API (WebSocket), which needs
the `websockets` package (installed in the Dockerfile).
The API key is read from the GEMINI_API_KEY environment variable, or from
'.env.local' in the addon folder. It is never stored in the database.
"""
import asyncio
import base64
import io
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import wave
import urllib.error
import urllib.request
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[1] / '.env.local'

TEXT_MODEL = 'gemini-3.5-flash-lite'
# Speech models, tried in order for each piece of text.
LIVE_MODELS = ['gemini-3.8-live-extended-thinking', 'gemini-3.8-live']
ENDPOINT = 'https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent'
LIVE_URL = ('wss://generativelanguage.googleapis.com/ws/'
            'google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent')
PCM_RATE = 24000  # Live API audio: 16-bit mono PCM at 24 kHz
LIVE_INSTRUCTION = (
    'You are a text-to-speech engine for a listening test. Read the user text aloud exactly, '
    'word for word, in a natural, clear lecture or conversation voice. Do not add, skip, '
    'translate or answer anything.')

NARRATOR_INSTRUCTION = (
    'You are the cheerful, upbeat host of a short English quiz video for Indonesian learners. Read the user '
    'text aloud exactly, word for word, in the language it is written in (Indonesian or English). Sound bright, '
    'smiling and positive with clear energy, never flat, tired or sleepy. Do not add, skip, translate or answer '
    'anything.')
# For the hook and the ending: the loudest, most expressive reading, like a top short-video creator.
NARRATOR_HYPE_INSTRUCTION = (
    'You are an extremely energetic, charismatic short-video host speaking to Indonesian viewers. Read the user '
    'text aloud exactly, word for word, in the language it is written in. Be very expressive and enthusiastic: '
    'a big, bright, smiling voice, a high and lively pitch with a lot of pitch variation, punchy emphasis on the '
    'key words, fast confident pacing, an excited exclamation at each "!" and a playful challenge in each '
    '"?". Never sound flat, calm or sleepy. Do not add, skip, translate or answer anything.')
# The opening line of every Short. *Starred* words get the yellow highlight on screen.
HOOKS = {
    'toefl': 'Stop Scroll! Sejago apa *listening* english kamu?',
    'school': 'Cuma *2%* orang yang bisa jawab soal ini!',
}
OUTRO_NARRATION = 'Jangan lupa follow akun ini ya! Next kita latihan lagi!'

ROLES = ('Lecturer', 'Student')
# Prebuilt Gemini voices by gender. One voice per role is picked at random on every Generate.
VOICES = {
    'male': ['Puck', 'Charon', 'Fenrir', 'Orus', 'Algenib', 'Iapetus'],
    'female': ['Kore', 'Zephyr', 'Leda', 'Aoede', 'Callirrhoe', 'Autonoe'],
}

# Voices with an upbeat character, used for the host. The roles of the recording never use the host's voice.
NARRATOR_VOICES = ['Puck', 'Zephyr', 'Fenrir', 'Autonoe', 'Leda']
HYPE_TEMPO = 1.08   # the hook and the ending are played a little faster, pitch unchanged

# One Short = one recording. The mode decides the level, the length and the number of questions.
MODES = {
    'school': {'questions': 1, 'min_words': 35, 'max_words': 65},
    'toefl': {'questions': 2, 'min_words': 70, 'max_words': 110},
}

MODE_RULES = {
    'school': (
        'Level: simple English for school students aged 7 to 16 (CEFR A2 to B1). Use common words and short '
        'sentences. Topics: school, family, friends, hobbies, animals, food, weather, daily routines.\n'
        'The recording is either a short conversation between a teacher (speaker "Lecturer") and a student '
        '(speaker "Student"), or a short monologue by the teacher (only "Lecturer").\n'
        'The recording is 40 to 60 words of natural spoken English.\n'
        'Write exactly 1 multiple-choice question about a clear detail or the main idea. The answer must be '
        'stated plainly in the recording.'),
    'toefl': (
        'Level: the TOEFL iBT Listening section. The recording is either a short monologue by a university '
        'professor (only "Lecturer") or a conversation between a professor and a student ("Lecturer" and '
        '"Student"), on an academic topic.\n'
        'The recording is 80 to 100 words of natural spoken English.\n'
        'Write exactly 2 multiple-choice questions in English (main idea, detail, inference, purpose, or '
        'function of a statement). The two questions must test different things.'),
}

SCRIPT_PROMPT = """You are writing one very short listening test for a YouTube Short.
Topic hint: {TOPIC}

{MODE_RULES}

Every question has exactly four options labeled A, B, C, D and exactly one correct answer, supported by the recording. Make the correct answer position random, not always the same letter.
Each question also has two explanations of why the correct option is right, based on what the speaker said: "explanation_en" in English and "explanation_id" in natural everyday Indonesian. Each is one sentence of at most 25 words.
Each question also has "explanation_voice": what the host SAYS out loud right after revealing the answer. Indonesian, casual and quick, at most 14 words, one short sentence. It must NOT restate the reason given in the written explanations and must not reuse their wording. Give a different, useful angle instead: the key word or phrase to listen for, or the trap that makes the most tempting wrong option wrong. Example of the style (do not copy): "Kuncinya ada di kata 'absolutely', itu tanda favorit!"

Also write:
- "clip_title": the topic of the recording in English, at most 6 words (shown as the subject of the listening).
- "theme": a short English label of the topic.
- "speakers": the gender of each role so the voices fit the characters: maps "Lecturer" and "Student" to "male" or "female". If there is no student, still give both.

Return only JSON in this shape, with no extra text:
{"clip_title": "...", "theme": "...", "speakers": {"Lecturer": "female", "Student": "male"}, "turns": [{"speaker": "Lecturer", "text": "..."}], "questions": [{"question": "...", "options": {"A": "...", "B": "...", "C": "...", "D": "..."}, "answer": "A", "explanation_en": "...", "explanation_id": "...", "explanation_voice": "..."}]}
"""


class GeminiError(Exception):
    pass


def get_api_key():
    key = os.environ.get('GEMINI_API_KEY', '').strip()
    if key:
        return key
    try:
        lines = ENV_FILE.read_text(encoding='utf-8').splitlines()
    except OSError:
        raise GeminiError('GEMINI_API_KEY is not set and %s cannot be read.' % ENV_FILE)
    for line in lines:
        name, sep, value = line.partition('=')
        if sep and name.strip() == 'GEMINI_API_KEY' and value.strip():
            return value.strip().strip('"\'')
    raise GeminiError('GEMINI_API_KEY is missing in %s.' % ENV_FILE)


RETRY_STATUS = (429, 503)


def _post(model, body, timeout=300, attempts=4):
    payload = json.dumps(body).encode('utf-8')
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            ENDPOINT % model,
            data=payload,
            headers={'Content-Type': 'application/json', 'x-goog-api-key': get_api_key()},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:500]
            if e.code in RETRY_STATUS and attempt < attempts:
                time.sleep(10 * attempt)  # model is busy; back off and try again
                continue
            raise GeminiError('Gemini %s returned HTTP %s: %s' % (model, e.code, detail))
        except urllib.error.URLError as e:
            raise GeminiError('Gemini request failed: %s' % e.reason)


def _candidate_parts(data):
    try:
        candidate = data['candidates'][0]
    except (KeyError, IndexError, TypeError):
        raise GeminiError('Gemini returned no candidates: %s' % str(data)[:500])
    return candidate.get('content', {}).get('parts', [])


def text_overlap(spoken, written):
    """Share of the longer words of `spoken` that also appear in `written` (0..1). Used to keep the
    host's explanation from repeating the written one."""
    words = [w for w in re.findall(r'\w+', spoken.lower()) if len(w) > 3]
    if not words:
        return 0.0
    written_words = set(re.findall(r'\w+', written.lower()))
    return sum(1 for w in words if w in written_words) / float(len(words))


def validate_short(data, mode):
    """Check the JSON Gemini returned for one Short and return it cleaned. Raises GeminiError."""
    if mode not in MODES:
        raise GeminiError('Unknown mode: %r' % mode)
    rules = MODES[mode]
    if not isinstance(data, dict):
        raise GeminiError('Script JSON is not an object.')

    turns = []
    for turn in data.get('turns') or []:
        speaker = turn.get('speaker') if isinstance(turn, dict) else None
        text = str((turn or {}).get('text') or '').strip() if isinstance(turn, dict) else ''
        if speaker not in ROLES or not text:
            raise GeminiError('Invalid turn: speaker=%r' % speaker)
        turns.append({'speaker': speaker, 'text': text})
    if not turns:
        raise GeminiError('The recording has no turns.')
    words = sum(len(t['text'].split()) for t in turns)
    if not rules['min_words'] <= words <= rules['max_words']:
        raise GeminiError('The recording has %d words, expected %d to %d.'
                          % (words, rules['min_words'], rules['max_words']))

    questions = data.get('questions') or []
    if len(questions) != rules['questions']:
        raise GeminiError('Expected %d question(s), got %d.' % (rules['questions'], len(questions)))
    clean = []
    for number, q in enumerate(questions, 1):
        options = q.get('options') or {}
        if not isinstance(options, dict) or set(options) != {'A', 'B', 'C', 'D'}:
            raise GeminiError('Question %d must have options A-D.' % number)
        opts = {k: str(options[k]).strip() for k in 'ABCD'}
        if not all(opts.values()):
            raise GeminiError('Question %d has an empty option.' % number)
        answer = q.get('answer')
        question = str(q.get('question') or '').strip()
        if answer not in ('A', 'B', 'C', 'D') or not question:
            raise GeminiError('Question %d has no valid answer or text.' % number)
        explanation_en = str(q.get('explanation_en') or '').strip()
        explanation_id = str(q.get('explanation_id') or '').strip()
        if not explanation_en or not explanation_id:
            raise GeminiError('Question %d needs explanation_en and explanation_id.' % number)
        explanation_voice = str(q.get('explanation_voice') or '').strip()
        if not explanation_voice or len(explanation_voice.split()) > 24:
            raise GeminiError('Question %d needs a short explanation_voice (at most 24 words).' % number)
        if text_overlap(explanation_voice, explanation_id) > 0.5:
            raise GeminiError('Question %d: explanation_voice is too similar to explanation_id.' % number)
        clean.append({'question': question, 'options': opts, 'answer': answer,
                      'explanation_en': explanation_en, 'explanation_id': explanation_id,
                      'explanation_voice': explanation_voice})

    clip_title = str(data.get('clip_title') or '').strip()
    if not clip_title:
        raise GeminiError('The script has no clip_title.')

    genders = data.get('speakers') or {}
    speakers = {}
    for role in ROLES:
        gender = str(genders.get(role, '')).strip().lower()
        speakers[role] = gender if gender in VOICES else random.choice(list(VOICES))
    return {
        'hook': HOOKS[mode],
        'clip_title': clip_title,
        'theme': str(data.get('theme') or clip_title).strip(),
        'speakers': speakers,
        'turns': turns,
        'questions': clean,
    }


def assign_narrator_voice():
    """The host's voice: one of the upbeat voices, chosen at random."""
    return random.choice(NARRATOR_VOICES)


def assign_voices(speakers, exclude=()):
    """Pick a random voice per role that matches its gender. The two roles never share a voice and
    never use a voice in `exclude` (the host's)."""
    voices = {}
    for role in ROLES:
        pool = [v for v in VOICES[speakers[role]] if v not in voices.values() and v not in exclude]
        voices[role] = random.choice(pool)
    return voices


def generate_short(topic='', mode='toefl'):
    """Return {'hook' (fixed per mode, see HOOKS), 'clip_title', 'theme', 'speakers': {role: gender}, 'turns', 'questions'}, validated.

    Each question is {'question', 'options', 'answer', 'explanation_en', 'explanation_id', 'explanation_voice'}.
    """
    if mode not in MODES:
        raise GeminiError('Unknown mode: %r' % mode)
    default_topic = 'any academic topic' if mode == 'toefl' else 'everyday school life'
    prompt = (SCRIPT_PROMPT
              .replace('{TOPIC}', topic.strip() or default_topic)
              .replace('{MODE_RULES}', MODE_RULES[mode]))
    body = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.8},
    }
    last_error = None
    for _attempt in range(3):
        data = _post(TEXT_MODEL, body, timeout=240)
        text = ''.join(p.get('text', '') for p in _candidate_parts(data) if not p.get('thought'))
        try:
            return validate_short(json.loads(text), mode)
        except (ValueError, GeminiError) as e:
            last_error = str(e)
    raise GeminiError('No valid script after 3 attempts: %s' % last_error)


def _pcm_to_wav(pcm):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(PCM_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


async def _speak_async(text, voice, model, instruction=LIVE_INSTRUCTION):
    """Read `text` with one prebuilt voice through the Live API. Returns raw PCM bytes."""
    try:
        import websockets
    except ImportError:
        raise GeminiError('The "websockets" Python package is missing. Rebuild the Odoo image.')
    setup = {'setup': {
        'model': 'models/%s' % model,
        'generationConfig': {
            'responseModalities': ['AUDIO'],
            'speechConfig': {'voiceConfig': {'prebuiltVoiceConfig': {'voiceName': voice}}},
        },
        'systemInstruction': {'parts': [{'text': instruction}]},
    }}
    turn = {'clientContent': {'turns': [{'role': 'user', 'parts': [{'text': text}]}], 'turnComplete': True}}
    pcm = bytearray()
    async with websockets.connect('%s?key=%s' % (LIVE_URL, get_api_key()), max_size=None) as ws:
        await ws.send(json.dumps(setup))
        first = json.loads(await ws.recv())
        if 'setupComplete' not in first:
            raise GeminiError('Gemini Live setup failed: %s' % str(first)[:300])
        await ws.send(json.dumps(turn))
        while True:
            message = json.loads(await ws.recv())
            content = message.get('serverContent') or {}
            for part in (content.get('modelTurn') or {}).get('parts', []):
                inline = part.get('inlineData')
                if inline and inline.get('data'):
                    pcm += base64.b64decode(inline['data'])
            if content.get('turnComplete'):
                break
    return bytes(pcm)


def _speak(text, voice, attempts=2, instruction=LIVE_INSTRUCTION):
    """Read `text` aloud. Each model in LIVE_MODELS gets `attempts` tries before the next one."""
    errors = []
    words = len(text.split())
    for model in LIVE_MODELS:
        for attempt in range(1, attempts + 1):
            try:
                pcm = asyncio.run(asyncio.wait_for(_speak_async(text, voice, model, instruction), timeout=240))
            except Exception as e:  # network, websocket close, timeout
                error = '%s: %s' % (type(e).__name__, e)
            else:
                seconds = len(pcm) / (2.0 * PCM_RATE)
                # Speech is about 2.5 words per second. Much shorter audio means the model skipped text.
                if seconds >= words / 6.0:
                    return pcm
                error = 'audio too short (%.1fs for %d words)' % (seconds, words)
            errors.append('%s: %s' % (model, error))
            time.sleep(3 * attempt)
    raise GeminiError('Gemini Live speech failed: %s' % ' | '.join(errors)[:600])


def join_turns(pieces):
    """Join [(speaker, pcm)] into one WAV. Returns (wav_bytes, timeline).

    The Live API has one voice per session, so each turn is read on its own and the pieces are
    joined with a short pause. `timeline` is [{'speaker', 'start', 'end'}] in seconds of the audio;
    the slide uses it to light up whoever is talking.
    """
    pcm = bytearray()
    timeline = []
    previous = None
    for speaker, piece in pieces:
        if previous is not None:
            pause = 0.25 if previous == speaker else 0.5
            pcm += bytes(2 * int(PCM_RATE * pause))
        start = len(pcm) / (2.0 * PCM_RATE)
        pcm += piece
        timeline.append({'speaker': speaker, 'start': round(start, 3),
                         'end': round(len(pcm) / (2.0 * PCM_RATE), 3)})
        previous = speaker
    return _pcm_to_wav(bytes(pcm)), timeline


def synthesize(turns, voices):
    """Turn one recording into (wav_bytes, timeline). `voices` maps role -> prebuilt voice name."""
    return join_turns([(turn['speaker'], _speak(turn['text'], voices[turn['speaker']])) for turn in turns])


def _speed_up(wav_bytes, tempo):
    """Play the WAV `tempo` times faster with the pitch unchanged (ffmpeg). Falls back to the original
    audio if ffmpeg is not available."""
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        return wav_bytes
    with tempfile.TemporaryDirectory(prefix='english_tempo_') as tmp:
        src, dst = Path(tmp) / 'in.wav', Path(tmp) / 'out.wav'
        src.write_bytes(wav_bytes)
        proc = subprocess.run([ffmpeg, '-y', '-loglevel', 'error', '-i', str(src), '-af', 'atempo=%.3f' % tempo,
                               '-ar', str(PCM_RATE), '-ac', '1', '-c:a', 'pcm_s16le', str(dst)],
                              capture_output=True, timeout=60)
        if proc.returncode != 0 or not dst.exists():
            return wav_bytes
        return dst.read_bytes()


def narrate(text, voice, hype=False):
    """The host reads `text` (Indonesian or English). `hype` = the extra expressive style used for the
    hook and the ending (also played a little faster). Returns WAV bytes."""
    if not hype:
        return _pcm_to_wav(_speak(text, voice, instruction=NARRATOR_INSTRUCTION))
    return _speed_up(_pcm_to_wav(_speak(text, voice, instruction=NARRATOR_HYPE_INSTRUCTION)), HYPE_TEMPO)
