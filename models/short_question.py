from odoo import fields, models


class EnglishShortQuestion(models.Model):
    _name = 'english.short.question'
    _description = 'English Short Question'
    _order = 'sequence, id'

    short_id = fields.Many2one('english.short', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    question = fields.Text(required=True)
    option_a = fields.Char(required=True)
    option_b = fields.Char(required=True)
    option_c = fields.Char(required=True)
    option_d = fields.Char(required=True)
    correct_answer = fields.Selection(
        [('A', 'A'), ('B', 'B'), ('C', 'C'), ('D', 'D')], required=True)
    explanation_id = fields.Text(string='Explanation (Indonesian)',
                                 help='Shown in the video after the answer is revealed.')
    explanation_en = fields.Text(string='Explanation (English)')
    explanation_voice = fields.Text(string='Spoken explanation (Indonesian)',
                                    help='What the host says out loud after the answer is shown.')
