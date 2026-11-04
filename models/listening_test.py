import base64
import logging
import tempfile
from pathlib import Path

from markupsafe import escape

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..services import gemini, render

_logger = logging.getLogger(__name__)


def _script_text(turns):
    return '\n'.join('%s: %s' % (t['speaker'], t['text']) for t in turns)


class EnglishListeningTest(models.Model):
    _name = 'english.listening.test'
    _description = 'English Listening Test'
    _order = 'id desc'

    name = fields.Char(required=True, default='New listening test')
    topic = fields.Char(help='Optional hint for Gemini, e.g. "biology lecture on ecosystems".')
    question_count = fields.Integer(
        default=9, copy=True,
        help='Total number of questions in the video (%d to %d). They are spread over the '
             'recordings, at most 3 per recording.' % (gemini.MIN_QUESTIONS, gemini.MAX_QUESTIONS))
    seconds_per_question = fields.Integer(
        default=25, copy=True,
        help='Countdown shown for each question. The real TOEFL has no per-question timer, '
             'so this is only a pacing guide for viewers.')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('queued', 'Queued'),
        ('generating', 'Generating'),
        ('done', 'Done'),
        ('error', 'Error'),
    ], default='draft', readonly=True, copy=False)
    questions_ids = fields.One2many('english.listening.question', 'test_id', string='Questions')
    progress = fields.Integer(readonly=True, copy=False)
    progress_message = fields.Char(readonly=True, copy=False)
    progress_html = fields.Html(compute='_compute_progress_html', sanitize=False)
    theme = fields.Char(readonly=True)
    voices = fields.Char(readonly=True)
    clip_ids = fields.One2many('english.listening.clip', 'test_id', string='Clips')
    script = fields.Text(readonly=True)
    error_message = fields.Text(readonly=True)
    generated_at = fields.Datetime(readonly=True)
    video_file = fields.Binary(attachment=True, readonly=True)
    video_html = fields.Html(compute='_compute_video_html', sanitize=False)
    thumbnail_file = fields.Binary(attachment=True, readonly=True, string='YouTube thumbnail')

    @api.constrains('question_count', 'seconds_per_question')
    def _check_video_settings(self):
        for test in self:
            if not gemini.MIN_QUESTIONS <= test.question_count <= gemini.MAX_QUESTIONS:
                raise ValidationError(_('The number of questions must be between %(min)d and %(max)d.',
                                        min=gemini.MIN_QUESTIONS, max=gemini.MAX_QUESTIONS))
            if not render.MIN_SECONDS_PER_QUESTION <= test.seconds_per_question <= render.MAX_SECONDS_PER_QUESTION:
                raise ValidationError(_('The countdown per question must be between %(min)d and %(max)d seconds.',
                                        min=render.MIN_SECONDS_PER_QUESTION, max=render.MAX_SECONDS_PER_QUESTION))

    @api.depends('progress', 'progress_message')
    def _compute_progress_html(self):
        for test in self:
            test.progress_html = (
                '<div class="o_english_progress" data-id="%d" style="max-width:560px;">'
                '<div class="o_english_progress_text mb-1 fw-bold">%s</div>'
                '<div class="progress" style="height:14px;">'
                '<div class="o_english_progress_bar progress-bar progress-bar-striped progress-bar-animated" '
                'role="progressbar" style="width:%d%%;"></div></div></div>'
            ) % (test.id or 0, escape(test.progress_message or 'Waiting in line...'), test.progress or 0)

    @api.depends('video_file')
    def _compute_video_html(self):
        for test in self:
            if test.id and test.video_file:
                test.video_html = (
                    '<video controls preload="metadata" style="width:100%%;max-width:960px;" '
                    'src="/english_section/video/%d"></video>' % test.id
                )
            else:
                test.video_html = '<p class="text-muted">The video has not been generated yet.</p>'

    def action_generate(self):
        self.write({'state': 'queued', 'error_message': False, 'progress': 0,
                    'progress_message': 'Waiting in line...'})
        self.env.ref('english_section.ir_cron_process_listening')._trigger()
        return True

    @api.model
    def _cron_process_queue(self):
        tests = self.search([('state', '=', 'queued')], order='id', limit=3)
        for test in tests:
            # Commit the state first so the form shows "generating" while the job runs.
            test.write({'state': 'generating'})
            self.env.cr.commit()
            try:
                test._run_generation()
            except Exception as e:
                _logger.exception('English Section: generation failed for test %s', test.id)
                self.env.cr.rollback()
                self.browse(test.id).write({'state': 'error', 'error_message': str(e)[:2000]})
            self.env.cr.commit()

    def _set_progress(self, percent, message):
        # Commit right away so the open form can show it while the job is still running.
        self.write({'progress': int(percent), 'progress_message': message})
        self.env.cr.commit()

    def _run_generation(self):
        self.ensure_one()
        self._set_progress(3, 'Writing the stories and questions...')
        script = gemini.generate_script(self.topic or '', self.question_count)
        total_clips = len(script['clips'])
        self._set_progress(12, 'Choosing the voices...')
        # Voices are picked once per Generate (matching each role's gender) and reused for every clip.
        voices = gemini.assign_voices(script['speakers'])
        wavs = []
        for index, clip in enumerate(script['clips'], 1):
            self._set_progress(12 + 48 * (index - 1) / total_clips,
                               'Recording the voices (%d of %d)...' % (index, total_clips))
            wavs.append(gemini.synthesize(clip['turns'], voices))

        with tempfile.TemporaryDirectory(prefix='english_section_') as tmp:
            render_clips = []
            for index, (clip, wav) in enumerate(zip(script['clips'], wavs), 1):
                audio_path = Path(tmp) / ('clip%02d.wav' % index)
                audio_path.write_bytes(wav)
                render_clips.append({
                    'title': clip['title'],
                    'audio_path': audio_path,
                    'questions': clip['questions'],
                })
            self._set_progress(62, 'Putting the video together...')

            def on_progress(done, total):
                self._set_progress(62 + 33 * done / total, 'Putting the video together...')

            mp4 = render.render_video(tmp, self.name, script['theme'], render_clips,
                                      seconds_per_question=self.seconds_per_question,
                                      on_progress=on_progress)
            thumbnail = render.render_thumbnail(tmp, self.name, script['theme'], self.question_count)
        self._set_progress(96, 'Saving the results...')

        self.clip_ids.unlink()  # cascades to the old questions
        self.questions_ids.unlink()
        question_model = self.env['english.listening.question']
        number = 0
        for index, (clip, wav) in enumerate(zip(script['clips'], wavs), 1):
            clip_rec = self.env['english.listening.clip'].create({
                'test_id': self.id,
                'sequence': index * 10,
                'title': clip['title'],
                'script': _script_text(clip['turns']),
                'audio_file': base64.b64encode(wav).decode('ascii'),
            })
            for q in clip['questions']:
                number += 1
                question_model.create({
                    'test_id': self.id,
                    'clip_id': clip_rec.id,
                    'sequence': number * 10,
                    'question': q['question'],
                    'option_a': q['options']['A'],
                    'option_b': q['options']['B'],
                    'option_c': q['options']['C'],
                    'option_d': q['options']['D'],
                    'correct_answer': q['answer'],
                    'explanation': q['explanation'],
                })
        self.write({
            'theme': script['theme'],
            'voices': ', '.join('%s: %s (%s)' % (role, voices[role], script['speakers'][role])
                                for role in gemini.ROLES),
            'script': '\n\n'.join('== %d. %s ==\n%s' % (i, c['title'], _script_text(c['turns']))
                                  for i, c in enumerate(script['clips'], 1)),
            'video_file': base64.b64encode(mp4).decode('ascii'),
            'thumbnail_file': base64.b64encode(thumbnail).decode('ascii'),
            'state': 'done',
            'progress': 100,
            'progress_message': 'Done',
            'error_message': False,
            'generated_at': fields.Datetime.now(),
        })
