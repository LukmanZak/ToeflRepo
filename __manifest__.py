{
    'name': 'English Section',
    'version': '20.0.1.0.0',
    'summary': 'TOEFL-style listening tests: Gemini script, Gemini TTS audio, rendered MP4 with questions',
    'author': 'English Section',
    'license': 'LGPL-3',
    'depends': ['web'],
    'data': [
        'security/ir.access.csv',
        'views/listening_views.xml',
        'data/cron.xml',
    ],
    'application': True,
    'installable': True,
}
