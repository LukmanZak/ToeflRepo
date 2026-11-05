import re

from odoo import http
from odoo.http import request
from werkzeug.exceptions import NotFound


class EnglishSectionVideo(http.Controller):
    @http.route('/english_section/short/<int:short_id>', type='http', auth='user', methods=['GET'])
    def video(self, short_id):
        short = request.env['english.short'].browse(short_id).exists()
        if not short or not short.video_file:
            raise NotFound()
        short.check_access('read')
        # Read the attachment directly. Odoo's Stream path returned 200 with Content-Length 0
        # for this file in the HTTP request, so the browser got an empty body.
        attachment = request.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'english.short'),
            ('res_id', '=', short.id),
            ('res_field', '=', 'video_file'),
        ], limit=1)
        if not attachment:
            raise NotFound()
        data = attachment.raw.content
        total = len(data)
        headers = [
            ('Content-Type', 'video/mp4'),
            ('Accept-Ranges', 'bytes'),
            ('Content-Disposition', 'inline; filename="short_%d.mp4"' % short.id),
            ('Cache-Control', 'no-cache, private'),
            # Odoo's default "default-src 'none'" blocks the <video> element from its own page.
            ('Content-Security-Policy', "default-src 'self'; media-src 'self'"),
            ('X-Content-Type-Options', 'nosniff'),
        ]
        # The browser needs Range support to seek. Without it, clicks on the progress bar do nothing.
        start, end = 0, total - 1
        range_header = request.httprequest.headers.get('Range')
        if range_header:
            match = re.match(r'bytes=(\d*)-(\d*)$', range_header.strip())
            if not match or (not match.group(1) and not match.group(2)):
                return request.make_response(b'', headers=[('Content-Range', 'bytes */%d' % total)], status=416)
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else total - 1
            else:  # suffix range: last N bytes
                start = max(total - int(match.group(2)), 0)
            end = min(end, total - 1)
            if start > end or start >= total:
                return request.make_response(b'', headers=[('Content-Range', 'bytes */%d' % total)], status=416)
            chunk = data[start:end + 1]
            headers += [
                ('Content-Length', str(len(chunk))),
                ('Content-Range', 'bytes %d-%d/%d' % (start, end, total)),
            ]
            return request.make_response(chunk, headers=headers, status=206)
        headers.append(('Content-Length', str(total)))
        return request.make_response(data, headers=headers)
