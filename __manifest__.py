{
    'name': 'English Section',
    'version': '20.0.1.0.0',
    'summary': 'YouTube Shorts listening practice (TOEFL and school level): Gemini script and voices, rendered 9:16 MP4',
    'author': 'English Section',
    'license': 'LGPL-3',
    'depends': ['web'],
    'data': [
        'security/ir.access.csv',
        'views/short_views.xml',
        'data/cron.xml',
    ],
    'assets': {
        'web.assets_backend': ['english_section/static/src/progress_service.js'],
    },
    'application': True,
    'installable': True,
}
