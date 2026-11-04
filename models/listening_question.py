from odoo import fields, models


class EnglishListeningQuestion(models.Model):
    _name = 'english.listening.question'
    _description = 'English Listening Question'
    _order = 'sequence, id'

    test_id = fields.Many2one('english.listening.test', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    question = fields.Text(required=True)
    option_a = fields.Char(required=True)
    option_b = fields.Char(required=True)
    option_c = fields.Char(required=True)
    option_d = fields.Char(required=True)
    correct_answer = fields.Selection(
        [('A', 'A'), ('B', 'B'), ('C', 'C'), ('D', 'D')], required=True)
