"""Gemini client for the English Section listening tests.

Uses the REST API with urllib, so no extra Python packages are needed.
The API key is read from the GEMINI_API_KEY environment variable, or from
'English Section/.env.local' next to this addon. It is never stored in the database.
"""
import base64
import io
import json
import os
import time
import wave
import urllib.error
import urllib.request
from pathlib import Path

ADDONS_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = ADDONS_DIR / 'English Section' / '.env.local'

TEXT_MODEL = 'gemini-3.8-flash'
TTS_MODEL = 'gemini-3.8-flash-tts'
ENDPOINT = 'https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent'

QUESTION_COUNT = 20
# Speaker name used in the script -> prebuilt Gemini voice.
SPEAKER_VOICES = {'Lecturer': 'Kore', 'Student': 'Puck'}

SCRIPT_PROMPT = """You are writing a listening test in the style of the TOEFL iBT Listening section.
Topic hint: {TOPIC}

Write ONE recording of about 650 to 800 words in natural spoken English:
- either a lecture by a university professor (use only the speaker "Lecturer"), or
- a conversation between a professor and a student (speakers "Lecturer" and "Student").

Then write exactly {N} multiple-choice questions in English about the recording (main idea, detail, inference, purpose, or the function of a statement).
Each question has exactly four options labeled A, B, C, D and exactly one correct answer, which must be supported by the recording.
Make the correct answers look random: use roughly equal numbers of A, B, C and D, in an irregular order with no repeating pattern (never ABCD, ABCD, ...).

Return only JSON in this shape, with no extra text:
{"title": "...", "turns": [{"speaker": "Lecturer", "text": "..."}], "questions": [{"question": "...", "options": {"A": "...", "B": "...", "C": "...", "D": "..."}, "answer": "A"}]}
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


def _validate_script(data):
    if not isinstance(data, dict):
        raise GeminiError('Script JSON is not an object.')
    turns = []
    for turn in data.get('turns') or []:
        speaker = turn.get('speaker')
        text = (turn.get('text') or '').strip()
        if speaker not in SPEAKER_VOICES or not text:
            raise GeminiError('Invalid turn: speaker=%r' % speaker)
        turns.append({'speaker': speaker, 'text': text})
    if not turns:
        raise GeminiError('Script has no turns.')

    questions = data.get('questions') or []
    if len(questions) != QUESTION_COUNT:
        raise GeminiError('Expected %d questions, got %d.' % (QUESTION_COUNT, len(questions)))
    clean = []
    for index, q in enumerate(questions, 1):
        options = q.get('options') or {}
        if not isinstance(options, dict) or set(options) != {'A', 'B', 'C', 'D'}:
            raise GeminiError('Question %d must have options A-D.' % index)
        opts = {k: str(options[k]).strip() for k in 'ABCD'}
        if not all(opts.values()):
            raise GeminiError('Question %d has an empty option.' % index)
        answer = q.get('answer')
        question = (q.get('question') or '').strip()
        if answer not in ('A', 'B', 'C', 'D') or not question:
            raise GeminiError('Question %d has no valid answer or text.' % index)
        clean.append({'question': question, 'options': opts, 'answer': answer})
    return {'title': str(data.get('title') or '').strip(), 'turns': turns, 'questions': clean}


def generate_script(topic=''):
    """Return {'title', 'turns': [{'speaker', 'text'}], 'questions': [...]}, validated."""
    prompt = (SCRIPT_PROMPT
              .replace('{TOPIC}', topic.strip() or 'any academic topic')
              .replace('{N}', str(QUESTION_COUNT)))
    body = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.8},
    }
    last_error = None
    for _attempt in range(3):
        data = _post(TEXT_MODEL, body, timeout=180)
        text = ''.join(p.get('text', '') for p in _candidate_parts(data) if not p.get('thought'))
        try:
            return _validate_script(json.loads(text))
        except (ValueError, GeminiError) as e:
            last_error = str(e)
    raise GeminiError('No valid script after 3 attempts: %s' % last_error)


def _ensure_wav(raw):
    """The API normally returns a WAV file. If it returns raw PCM, wrap it (16-bit mono 24 kHz)."""
    if raw[:4] == b'RIFF':
        return raw
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(raw)
    return buf.getvalue()


def synthesize(turns):
    """Turn the dialogue into one WAV file (bytes) with one voice per speaker."""
    body = {
        'contents': [{'parts': [
            {'text': turn['text'], 'speechMetadata': {'speaker': turn['speaker']}} for turn in turns
        ]}],
        'generationConfig': {
            'responseModalities': ['AUDIO'],
            'speechConfig': {'multiSpeakerVoiceConfig': {'speakerVoiceConfigs': [
                {'speaker': name, 'voiceConfig': {'prebuiltVoiceConfig': {'voiceName': voice}}}
                for name, voice in SPEAKER_VOICES.items()
            ]}},
        },
    }
    data = _post(TTS_MODEL, body, timeout=600)
    for part in _candidate_parts(data):
        inline = part.get('inlineData')
        if inline and inline.get('data'):
            return _ensure_wav(base64.b64decode(inline['data']))
    raise GeminiError('Gemini TTS returned no audio.')
