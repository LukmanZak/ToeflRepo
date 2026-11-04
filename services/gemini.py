"""Gemini client for the English Section listening tests.

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

MIN_QUESTIONS = 3
MAX_QUESTIONS = 15
MAX_QUESTIONS_PER_CLIP = 3
ROLES = ('Lecturer', 'Student')
# Prebuilt Gemini voices by gender. One voice per role is picked at random on every Generate
# and reused for all clips of that test.
VOICES = {
    'male': ['Puck', 'Charon', 'Fenrir', 'Orus', 'Algenib', 'Iapetus'],
    'female': ['Kore', 'Zephyr', 'Leda', 'Aoede', 'Callirrhoe', 'Autonoe'],
}

SCRIPT_PROMPT = """You are writing a listening test in the style of the TOEFL iBT Listening section.
Theme hint: {TOPIC}

Choose ONE overall theme (follow the hint if given). Then write {CLIPS} separate short recordings that all fit this theme.
The recordings do not need to continue each other; each one is its own small story or lecture segment, seen from a different angle of the theme.
Each recording:
- has a short title,
- is about 150 to 250 words of natural spoken English,
- is either a monologue by a university professor (only the speaker "Lecturer") or a conversation between a professor and a student (speakers "Lecturer" and "Student"). {KINDS}
- has multiple-choice questions in English (main idea, detail, inference, purpose, or function of a statement). The number of questions per recording is exactly: {PLAN}.

Every question has exactly four options labeled A, B, C, D and exactly one correct answer, supported by its recording.
Across all {N} questions, make the correct answers look random: roughly equal numbers of A, B, C and D, in an irregular order with no repeating pattern.
Every question also has an "explanation": one or two short sentences (at most 40 words) saying why the correct option is right, based on what the speaker said. It is shown on screen after the answer is revealed.

Also decide the gender of each role so the voices fit the characters: "speakers" maps "Lecturer" and "Student" to "male" or "female". Use the same gender for a role in all recordings and refer to them consistently. Do not use names that clash with the chosen gender.

Return only JSON in this shape, with no extra text:
{"theme": "...", "speakers": {"Lecturer": "female", "Student": "male"}, "clips": [{"title": "...", "turns": [{"speaker": "Lecturer", "text": "..."}], "questions": [{"question": "...", "options": {"A": "...", "B": "...", "C": "...", "D": "..."}, "answer": "A", "explanation": "..."}]}]}
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


def plan_clips(question_count):
    """How many questions each recording gets: as even as possible, at most 3 per recording.

    3 -> [3], 4 -> [2, 2], 7 -> [3, 2, 2], 15 -> [3, 3, 3, 3, 3].
    """
    if not MIN_QUESTIONS <= question_count <= MAX_QUESTIONS:
        raise GeminiError('The number of questions must be between %d and %d.'
                          % (MIN_QUESTIONS, MAX_QUESTIONS))
    clips = -(-question_count // MAX_QUESTIONS_PER_CLIP)  # ceil
    base, extra = divmod(question_count, clips)
    return [base + 1] * extra + [base] * (clips - extra)


def _validate_clip(index, clip, expected_questions):
    if not isinstance(clip, dict):
        raise GeminiError('Clip %d is not an object.' % index)
    turns = []
    for turn in clip.get('turns') or []:
        speaker = turn.get('speaker')
        text = (turn.get('text') or '').strip()
        if speaker not in ROLES or not text:
            raise GeminiError('Clip %d has an invalid turn: speaker=%r' % (index, speaker))
        turns.append({'speaker': speaker, 'text': text})
    if not turns:
        raise GeminiError('Clip %d has no turns.' % index)

    questions = clip.get('questions') or []
    if len(questions) != expected_questions:
        raise GeminiError('Clip %d: expected %d questions, got %d.' % (index, expected_questions, len(questions)))
    clean = []
    for number, q in enumerate(questions, 1):
        options = q.get('options') or {}
        if not isinstance(options, dict) or set(options) != {'A', 'B', 'C', 'D'}:
            raise GeminiError('Clip %d question %d must have options A-D.' % (index, number))
        opts = {k: str(options[k]).strip() for k in 'ABCD'}
        if not all(opts.values()):
            raise GeminiError('Clip %d question %d has an empty option.' % (index, number))
        answer = q.get('answer')
        question = (q.get('question') or '').strip()
        if answer not in ('A', 'B', 'C', 'D') or not question:
            raise GeminiError('Clip %d question %d has no valid answer or text.' % (index, number))
        explanation = str(q.get('explanation') or '').strip()
        if not explanation:
            raise GeminiError('Clip %d question %d has no explanation.' % (index, number))
        clean.append({'question': question, 'options': opts, 'answer': answer, 'explanation': explanation})
    return {'title': str(clip.get('title') or 'Part %d' % index).strip(), 'turns': turns, 'questions': clean}


def _validate_script(data, plan):
    if not isinstance(data, dict):
        raise GeminiError('Script JSON is not an object.')
    clips = data.get('clips') or []
    if len(clips) != len(plan):
        raise GeminiError('Expected %d clips, got %d.' % (len(plan), len(clips)))
    genders = data.get('speakers') or {}
    speakers = {}
    for role in ROLES:
        gender = str(genders.get(role, '')).strip().lower()
        speakers[role] = gender if gender in VOICES else random.choice(list(VOICES))
    return {
        'theme': str(data.get('theme') or '').strip(),
        'speakers': speakers,
        'clips': [_validate_clip(i, clip, plan[i - 1]) for i, clip in enumerate(clips, 1)],
    }


def assign_voices(speakers):
    """Pick a random voice per role that matches its gender. The two roles never share a voice."""
    voices = {}
    for role in ROLES:
        pool = [v for v in VOICES[speakers[role]] if v not in voices.values()]
        voices[role] = random.choice(pool)
    return voices


def _kinds_rule(clip_count):
    if clip_count >= 3:
        return 'Mix both kinds across the %d recordings, with at least two conversations.' % clip_count
    if clip_count == 2:
        return 'Make one a monologue and the other a conversation.'
    return 'Make it a conversation.'


def generate_script(topic='', question_count=9):
    """Return {'theme', 'speakers': {role: gender}, 'clips': [{'title', 'turns', 'questions'}]}, validated.

    Each question is {'question', 'options', 'answer', 'explanation'}.
    """
    plan = plan_clips(question_count)
    prompt = (SCRIPT_PROMPT
              .replace('{TOPIC}', topic.strip() or 'any academic topic')
              .replace('{CLIPS}', str(len(plan)))
              .replace('{KINDS}', _kinds_rule(len(plan)))
              .replace('{PLAN}', '; '.join('recording %d: %d' % (i, n) for i, n in enumerate(plan, 1)))
              .replace('{N}', str(question_count)))
    body = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.8},
    }
    last_error = None
    for _attempt in range(3):
        data = _post(TEXT_MODEL, body, timeout=240)
        text = ''.join(p.get('text', '') for p in _candidate_parts(data) if not p.get('thought'))
        try:
            return _validate_script(json.loads(text), plan)
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


async def _speak_async(text, voice, model):
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
        'systemInstruction': {'parts': [{'text': LIVE_INSTRUCTION}]},
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


def _speak(text, voice, attempts=2):
    """Read `text` aloud. Each model in LIVE_MODELS gets `attempts` tries before the next one."""
    errors = []
    words = len(text.split())
    for model in LIVE_MODELS:
        for attempt in range(1, attempts + 1):
            try:
                pcm = asyncio.run(asyncio.wait_for(_speak_async(text, voice, model), timeout=240))
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


def synthesize(turns, voices):
    """Turn one clip into a WAV file (bytes). `voices` maps role -> prebuilt voice name.

    The Live API has one voice per session, so each turn is read on its own and the pieces
    are joined with a short pause.
    """
    pcm = bytearray()
    previous = None
    for turn in turns:
        if previous is not None:
            pause = 0.25 if previous == turn['speaker'] else 0.5
            pcm += bytes(2 * int(PCM_RATE * pause))
        pcm += _speak(turn['text'], voices[turn['speaker']])
        previous = turn['speaker']
    return _pcm_to_wav(bytes(pcm))
