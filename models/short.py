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


class EnglishShort(models.Model):
    _name = 'english.short'
    _description = 'English Short (YouTube Shorts listening)'
    _order = 'id desc'

    name = fields.Char(required=True, default='New short')
    topic = fields.Char(help='Optional hint for Gemini, e.g. "how beavers change rivers" or "my school day".')
    mode = fields.Selection([
        ('school', 'School (SD-SMA): 1 question, simple English'),
        ('toefl', 'TOEFL Listening: 2 questions, academic'),
    ], required=True, default='toefl', copy=True)
    seconds_per_question = fields.Integer(
        default=10, copy=True,
        help='Countdown shown for each question. It is shortened automatically if the Short '
             'would otherwise pass the 90 second limit.')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('queued', 'Queued'),
        ('generating', 'Generating'),
        ('done', 'Done'),
        ('error', 'Error'),
    ], default='draft', readonly=True, copy=False)
    question_ids = fields.One2many('english.short.question', 'short_id', string='Questions')
    progress = fields.Integer(readonly=True, copy=False)
    progress_message = fields.Char(readonly=True, copy=False)
    progress_html = fields.Html(compute='_compute_progress_html', sanitize=False)
    hook = fields.Char(readonly=True, help='The line shown in the first seconds of the Short.')
    clip_title = fields.Char(string='Topic (materi)', readonly=True)
    theme = fields.Char(readonly=True)
    voices = fields.Char(readonly=True)
    script = fields.Text(readonly=True)
    error_message = fields.Text(readonly=True)
    generated_at = fields.Datetime(readonly=True)
    audio_file = fields.Binary(attachment=True, readonly=True)
    video_file = fields.Binary(attachment=True, readonly=True)
    video_html = fields.Html(compute='_compute_video_html', sanitize=False)

    @api.constrains('seconds_per_question')
    def _check_seconds(self):
        for short in self:
            if not render.MIN_SECONDS_PER_QUESTION <= short.seconds_per_question <= render.MAX_SECONDS_PER_QUESTION:
                raise ValidationError(_('The countdown per question must be between %(min)d and %(max)d seconds.',
                                        min=render.MIN_SECONDS_PER_QUESTION, max=render.MAX_SECONDS_PER_QUESTION))

    @api.depends('progress', 'progress_message')
    def _compute_progress_html(self):
        for short in self:
            short.progress_html = (
                '<div class="o_english_progress" data-id="%d" style="max-width:560px;">'
                '<div class="o_english_progress_text mb-1 fw-bold">%s</div>'
                '<div class="progress" style="height:14px;">'
                '<div class="o_english_progress_bar progress-bar progress-bar-striped progress-bar-animated" '
                'role="progressbar" style="width:%d%%;"></div></div></div>'
            ) % (short.id or 0, escape(short.progress_message or 'Waiting in line...'), short.progress or 0)

    @api.depends('video_file')
    def _compute_video_html(self):
        for short in self:
            if short.id and short.video_file:
                short.video_html = (
                    '<video controls preload="metadata" style="width:100%%;max-width:360px;" '
                    'src="/english_section/short/%d"></video>' % short.id
                )
            else:
                short.video_html = '<p class="text-muted">The Short has not been generated yet.</p>'

    def action_generate(self):
        self.write({'state': 'queued', 'error_message': False, 'progress': 0,
                    'progress_message': 'Waiting in line...'})
        self.env.ref('english_section.ir_cron_process_shorts')._trigger()
        return True

    @api.model
    def _cron_process_queue(self):
        shorts = self.search([('state', '=', 'queued')], order='id', limit=3)
        for short in shorts:
            # Commit the state first so the form shows "generating" while the job runs.
            short.write({'state': 'generating'})
            self.env.cr.commit()
            try:
                short._run_generation()
            except Exception as e:
                _logger.exception('English Section: generation failed for short %s', short.id)
                self.env.cr.rollback()
                self.browse(short.id).write({'state': 'error', 'error_message': str(e)[:2000]})
            self.env.cr.commit()

    def _set_progress(self, percent, message):
        # Commit right away so the open form can show it while the job is still running.
        self.write({'progress': int(percent), 'progress_message': message})
        self.env.cr.commit()

    def _run_generation(self):
        self.ensure_one()
        self._set_progress(3, 'Writing the script and questions...')
        script = gemini.generate_short(self.topic or '', self.mode)
        self._set_progress(10, 'Choosing the voices...')
        # Voices are picked once per Generate. The host (narrator) gets an upbeat voice, and the roles of
        # the recording get voices matching their gender that are different from the host's.
        narrator = gemini.assign_narrator_voice()
        voices = gemini.assign_voices(script['speakers'], exclude={narrator})
        self._set_progress(13, 'Recording the voices...')
        wav, timeline = gemini.synthesize(script['turns'], voices)

        # The host's lines: hook, question, answer + short explanation, outro.
        lines = [('hook', script['hook'].replace('*', ''), True)]
        for number, q in enumerate(script['questions'], 1):
            lines.append(('q%d' % number, q['question'], False))
            lines.append(('a%d' % number, 'Jawabannya %s. %s' % (q['answer'], q['explanation_voice']), False))
        lines.append(('outro', gemini.OUTRO_NARRATION, True))

        with tempfile.TemporaryDirectory(prefix='english_section_') as tmp:
            audio_path = Path(tmp) / 'clip.wav'
            audio_path.write_bytes(wav)
            narration = {}
            for index, (key, text, hype) in enumerate(lines):
                self._set_progress(30 + 22 * index / len(lines), 'Recording the host (%d of %d)...' % (index + 1, len(lines)))
                path = Path(tmp) / ('host_%s.wav' % key)
                path.write_bytes(gemini.narrate(text, narrator, hype=hype))
                narration[key] = path
            self._set_progress(55, 'Putting the video together...')

            def on_progress(done, total):
                self._set_progress(55 + 40 * done / total, 'Putting the video together...')

            questions = [dict(q, question_audio=narration['q%d' % n], answer_audio=narration['a%d' % n])
                         for n, q in enumerate(script['questions'], 1)]
            mp4, kept = render.render_video(tmp, {
                'hook': script['hook'],
                'title': script['clip_title'],
                'audio_path': audio_path,
                'hook_audio': narration['hook'],
                'outro_audio': narration['outro'],
                'timeline': timeline,
                'questions': questions,
            }, seconds_per_question=self.seconds_per_question, mode=self.mode, on_progress=on_progress)
        if kept < len(script['questions']):
            _logger.info('English Section: short %s was too long, kept %d of %d questions',
                         self.id, kept, len(script['questions']))
            script['questions'] = script['questions'][:kept]
        self._set_progress(96, 'Saving the results...')

        self.question_ids.unlink()
        for number, q in enumerate(script['questions'], 1):
            self.env['english.short.question'].create({
                'short_id': self.id,
                'sequence': number * 10,
                'question': q['question'],
                'option_a': q['options']['A'],
                'option_b': q['options']['B'],
                'option_c': q['options']['C'],
                'option_d': q['options']['D'],
                'correct_answer': q['answer'],
                'explanation_id': q['explanation_id'],
                'explanation_en': q['explanation_en'],
                'explanation_voice': q['explanation_voice'],
            })
        self.write({
            'hook': script['hook'].replace('*', ''),
            'clip_title': script['clip_title'],
            'theme': script['theme'],
            'voices': ', '.join('%s: %s (%s)' % (role, voices[role], script['speakers'][role])
                                for role in gemini.ROLES) + ', Host: %s' % narrator,
            'script': _script_text(script['turns']),
            'audio_file': base64.b64encode(wav).decode('ascii'),
            'video_file': base64.b64encode(mp4).decode('ascii'),
            'state': 'done',
            'progress': 100,
            'progress_message': 'Done',
            'error_message': False,
            'generated_at': fields.Datetime.now(),
        })
