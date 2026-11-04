from odoo import fields, models


class EnglishListeningClip(models.Model):
    _name = 'english.listening.clip'
    _description = 'English Listening Clip'
    _order = 'sequence, id'

    test_id = fields.Many2one('english.listening.test', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    title = fields.Char(required=True)
    script = fields.Text(readonly=True)
    audio_file = fields.Binary(attachment=True, readonly=True)
    question_ids = fields.One2many('english.listening.question', 'clip_id', string='Questions')
