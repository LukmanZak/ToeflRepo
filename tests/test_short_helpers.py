"""Pure-Python tests for the Shorts helpers (no Odoo, no Gemini, no Chromium).

Run from the addon folder:  python -m unittest discover -s tests -v
"""
import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services'))

import gemini  # noqa: E402
import render  # noqa: E402

RATE = gemini.PCM_RATE


def tone(seconds, amplitude):
    n = int(RATE * seconds)
    return struct.pack('<%dh' % n, *[int(amplitude * math.sin(2 * math.pi * 220 * i / RATE)) for i in range(n)])


def silence(seconds):
    return bytes(2 * int(RATE * seconds))


def write_wav(path, pcm):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


def valid_short(words, questions):
    return {
        'clip_title': 'How Beavers Change Rivers',
        'theme': 'ecology',
        'speakers': {'Lecturer': 'female', 'Student': 'male'},
        'turns': [{'speaker': 'Lecturer', 'text': ' '.join(['word'] * words)}],
        'questions': [{'question': 'Why?', 'options': {'A': 'a', 'B': 'b', 'C': 'c', 'D': 'd'}, 'answer': 'B',
                       'explanation_en': 'Because.', 'explanation_id': 'Karena.',
                       'explanation_voice': 'Dengarkan kata kuncinya.'} for _ in range(questions)],
    }


class AudioLevelsTest(unittest.TestCase):
    def test_silence_then_loud_then_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'a.wav'
            write_wav(path, silence(1) + tone(1, 20000) + tone(1, 5000))
            levels = render.audio_levels(path, fps=30)
        self.assertAlmostEqual(len(levels), 90, delta=1)
        self.assertTrue(all(v == 0 for v in levels[:25]), 'silence must stay 0')
        self.assertGreater(max(levels[35:55]), 0.9, 'loud part must reach about 1')
        self.assertTrue(all(0 <= v <= 1 for v in levels))
        quiet = sum(levels[65:85]) / 20
        loud = sum(levels[35:55]) / 20
        self.assertLess(quiet, loud, 'a quieter part must give a lower level')
        self.assertGreater(quiet, 0.1)

    def test_all_silence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'a.wav'
            write_wav(path, silence(1))
            self.assertEqual(set(render.audio_levels(path)), {0.0})


class TimelineTest(unittest.TestCase):
    def test_timeline_matches_audio(self):
        pieces = [('Lecturer', tone(2.0, 8000)), ('Student', tone(1.5, 8000)), ('Student', tone(1.0, 8000)),
                  ('Lecturer', tone(0.5, 8000))]
        wav, timeline = gemini.join_turns(pieces)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'a.wav'
            path.write_bytes(wav)
            total = render.wav_duration(path)
        self.assertEqual([t['speaker'] for t in timeline], ['Lecturer', 'Student', 'Student', 'Lecturer'])
        self.assertAlmostEqual(timeline[0]['start'], 0.0)
        self.assertAlmostEqual(timeline[0]['end'], 2.0, places=2)
        self.assertAlmostEqual(timeline[1]['start'], 2.5, places=2)    # 0.5 s pause between speakers
        self.assertAlmostEqual(timeline[2]['start'], timeline[1]['end'] + 0.25, places=2)  # same speaker: 0.25 s
        self.assertAlmostEqual(timeline[-1]['end'], total, places=2)


class VoiceTest(unittest.TestCase):
    def test_host_voice_is_upbeat_and_never_used_by_the_roles(self):
        for _ in range(50):
            host = gemini.assign_narrator_voice()
            self.assertIn(host, gemini.NARRATOR_VOICES)
            voices = gemini.assign_voices({'Lecturer': 'female', 'Student': 'male'}, exclude={host})
            self.assertNotIn(host, voices.values())
            self.assertNotEqual(voices['Lecturer'], voices['Student'])


class HookWordsTest(unittest.TestCase):
    def test_highlight(self):
        words = render.hook_words('Cuma orang *pintar* yang bisa jawab ini')
        self.assertEqual([w['t'] for w in words], 'Cuma orang pintar yang bisa jawab ini'.split())
        self.assertEqual([w['t'] for w in words if w['hl']], ['pintar'])

    def test_multi_word_highlight_and_stray_stars(self):
        words = render.hook_words('Kamu *yakin bisa* jawab* ini?')
        self.assertEqual([w['t'] for w in words if w['hl']], ['yakin', 'bisa'])
        self.assertTrue(all('*' not in w['t'] for w in words))

    def test_limit_and_empty(self):
        self.assertEqual(render.hook_words(''), [])
        self.assertEqual(len(render.hook_words(' '.join(['x'] * 40))), render.MAX_HOOK_WORDS)


class BudgetTest(unittest.TestCase):
    # (question narration s, answer narration s) per question; hook 3 s, outro 3 s
    Q = (4.0, 6.0)

    def fit(self, audio, n, seconds=10, hook=3.0, outro=3.0):
        return render.fit_budget(hook, audio, [self.Q] * n, seconds, outro)

    def test_school_fits_with_one_question(self):
        seconds, keep = self.fit(22.0, 1)
        self.assertEqual((seconds, keep), (10, 1))
        self.assertLessEqual(render.total_seconds(3.0, 22.0, [self.Q], seconds, 3.0), 88.0)

    def test_toefl_two_questions_fit_with_a_short_recording(self):
        seconds, keep = self.fit(26.0, 2)
        self.assertEqual(keep, 2)
        self.assertLessEqual(render.total_seconds(3.0, 26.0, [self.Q] * 2, seconds, 3.0), 88.0)

    def test_countdown_shortens_before_a_question_is_dropped(self):
        seconds, keep = self.fit(32.0, 2, seconds=12)
        self.assertEqual(keep, 2)
        self.assertLess(seconds, 12)
        self.assertGreaterEqual(seconds, render.SHRINK_FLOOR)

    def test_drops_to_one_question_when_two_do_not_fit(self):
        seconds, keep = self.fit(46.0, 2)
        self.assertEqual(keep, 1)
        self.assertLessEqual(render.total_seconds(3.0, 46.0, [self.Q], seconds, 3.0), 88.0)

    def test_too_long_even_for_one_question_raises(self):
        with self.assertRaises(render.RenderError):
            self.fit(80.0, 2)

    def test_timing_follows_narration_length(self):
        self.assertGreater(render.recording_delay(5.0), render.recording_delay(1.0))
        self.assertEqual(render.recording_delay(0.5), render.MIN_RECORDING_DELAY)
        self.assertGreater(render.question_lead(6.0), render.question_lead(2.0))
        self.assertEqual(render.answer_seconds(0.5), render.MIN_ANSWER_SECONDS)
        self.assertEqual(render.answer_seconds(30.0), render.MAX_ANSWER_SECONDS)


class MixListeningAudioTest(unittest.TestCase):
    def test_hook_then_recording_at_the_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            hook, rec, out = Path(tmp) / 'h.wav', Path(tmp) / 'r.wav', Path(tmp) / 'm.wav'
            write_wav(hook, tone(1.0, 10000))
            write_wav(rec, tone(3.0, 20000))
            delay = render.recording_delay(1.0)            # 2.0 s: the minimum applies
            self.assertEqual(delay, render.MIN_RECORDING_DELAY)
            render.mix_listening_audio(hook, rec, delay, out)
            self.assertAlmostEqual(render.wav_duration(out), delay + 3.0, places=2)
            levels = render.audio_levels(out, fps=30)
        self.assertTrue(all(v == 0 for v in levels[:4]), 'silence before the hook')
        self.assertGreater(max(levels[8:30]), 0.5, 'the hook is audible')
        self.assertEqual(levels[int(1.8 * 30)], 0, 'silence between the hook and the recording')
        self.assertGreater(max(levels[int(delay * 30) + 5:int(delay * 30) + 40]), 0.9)

    def test_long_hook_pushes_the_recording_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            hook, rec, out = Path(tmp) / 'h.wav', Path(tmp) / 'r.wav', Path(tmp) / 'm.wav'
            write_wav(hook, tone(4.0, 10000))
            write_wav(rec, tone(1.0, 20000))
            delay = render.recording_delay(4.0)
            self.assertGreater(delay, 4.0)
            render.mix_listening_audio(hook, rec, delay, out)
            self.assertAlmostEqual(render.wav_duration(out), delay + 1.0, places=2)


class ValidateShortTest(unittest.TestCase):
    def test_valid(self):
        clean = gemini.validate_short(valid_short(90, 2), 'toefl')
        self.assertEqual(len(clean['questions']), 2)
        self.assertEqual(clean['questions'][0]['explanation_id'], 'Karena.')
        self.assertEqual(clean['hook'], gemini.HOOKS['toefl'])
        self.assertEqual(gemini.validate_short(valid_short(50, 1), 'school')['hook'], gemini.HOOKS['school'])
        self.assertIn(clean['speakers']['Lecturer'], ('male', 'female'))

    def test_word_count_enforced_per_mode(self):
        gemini.validate_short(valid_short(50, 1), 'school')
        with self.assertRaises(gemini.GeminiError):
            gemini.validate_short(valid_short(90, 1), 'school')   # too long for school
        with self.assertRaises(gemini.GeminiError):
            gemini.validate_short(valid_short(50, 2), 'toefl')    # too short for toefl

    def test_question_count_enforced(self):
        with self.assertRaises(gemini.GeminiError):
            gemini.validate_short(valid_short(50, 2), 'school')
        with self.assertRaises(gemini.GeminiError):
            gemini.validate_short(valid_short(90, 1), 'toefl')

    def test_missing_fields(self):
        data = valid_short(90, 2)
        data['clip_title'] = ''
        with self.assertRaises(gemini.GeminiError):
            gemini.validate_short(data, 'toefl')
        data = valid_short(90, 2)
        data['questions'][0]['explanation_id'] = ''
        with self.assertRaises(gemini.GeminiError):
            gemini.validate_short(data, 'toefl')
        data = valid_short(90, 2)
        data['questions'][1]['explanation_voice'] = ''
        with self.assertRaises(gemini.GeminiError):
            gemini.validate_short(data, 'toefl')
        data = valid_short(90, 2)
        data['questions'][0]['explanation_id'] = 'Siswa itu bilang sains pelajaran favoritnya karena eksperimen seru.'
        data['questions'][0]['explanation_voice'] = 'Dia bilang sains favoritnya karena eksperimen seru.'
        with self.assertRaises(gemini.GeminiError):
            gemini.validate_short(data, 'toefl')   # the spoken line repeats the written one
        data = valid_short(90, 2)
        data['questions'][0]['explanation_voice'] = ' '.join(['kata'] * 30)
        with self.assertRaises(gemini.GeminiError):
            gemini.validate_short(data, 'toefl')
        data = valid_short(90, 2)
        data['questions'][0]['options'].pop('D')
        with self.assertRaises(gemini.GeminiError):
            gemini.validate_short(data, 'toefl')


if __name__ == '__main__':
    unittest.main()
