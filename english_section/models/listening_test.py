import base64
import logging
import tempfile
from pathlib import Path

from odoo import api, fields, models

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
    state = fields.Selection([
        ('draft', 'Draft'),
        ('queued', 'Queued'),
        ('generating', 'Generating'),
        ('done', 'Done'),
        ('error', 'Error'),
    ], default='draft', readonly=True, copy=False)
    questions_ids = fields.One2many('english.listening.question', 'test_id', string='Questions')
    script = fields.Text(readonly=True)
    error_message = fields.Text(readonly=True)
    generated_at = fields.Datetime(readonly=True)
    audio_file = fields.Binary(attachment=True, readonly=True)
    video_file = fields.Binary(attachment=True, readonly=True)
    video_html = fields.Html(compute='_compute_video_html', sanitize=False)

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
        self.write({'state': 'queued', 'error_message': False})
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

    def _run_generation(self):
        self.ensure_one()
        script = gemini.generate_script(self.topic or '')
        wav = gemini.synthesize(script['turns'])
        with tempfile.TemporaryDirectory(prefix='english_section_') as tmp:
            audio_path = Path(tmp) / 'listening.wav'
            audio_path.write_bytes(wav)
            mp4 = render.render_video(tmp, self.name, audio_path, script['questions'])

        self.questions_ids.unlink()
        self.env['english.listening.question'].create([{
            'test_id': self.id,
            'sequence': (index + 1) * 10,
            'question': q['question'],
            'option_a': q['options']['A'],
            'option_b': q['options']['B'],
            'option_c': q['options']['C'],
            'option_d': q['options']['D'],
            'correct_answer': q['answer'],
        } for index, q in enumerate(script['questions'])])
        self.write({
            'script': _script_text(script['turns']),
            'audio_file': base64.b64encode(wav).decode('ascii'),
            'video_file': base64.b64encode(mp4).decode('ascii'),
            'state': 'done',
            'error_message': False,
            'generated_at': fields.Datetime.now(),
        })
