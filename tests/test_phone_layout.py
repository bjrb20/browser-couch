"""The page stays usable on a phone: these rules stop zoom on focus, keep tap targets big and fit the stream sideways."""
from pathlib import Path
import re
from unittest import TestCase

WEB = Path(__file__).resolve().parent.parent / 'web'


def block(css, query):
    """The text of the multi-line @media block with this exact query."""
    head = '@media' + query + '{\n'
    start = css.index(head) + len(head)
    depth = 1
    for i in range(start, len(css)):
        depth += {'{': 1, '}': -1}.get(css[i], 0)
        if depth == 0:
            return css[start:i]
    raise AssertionError('unclosed @media' + query)


class PhoneLayout(TestCase):
    css = (WEB / 'style.css').read_text()
    html = (WEB / 'index.html').read_text()
    js = (WEB / 'client.mjs').read_text()

    def test_viewport_fits_device_and_safe_areas(self):
        meta = re.search(r'<meta name="viewport" content="([^"]+)"', self.html).group(1)
        for part in ('width=device-width', 'initial-scale=1', 'viewport-fit=cover'):
            self.assertIn(part, meta)
        self.assertNotIn('user-scalable=no', meta)
        self.assertNotIn('maximum-scale', meta)

    def test_text_fields_are_16px_on_phones(self):
        phone = block(self.css, '(max-width:700px),(pointer:coarse)')
        self.assertRegex(phone, r'\ninput,select,textarea[^{]*\{font-size:16px\}')

    def test_touch_targets_are_44px(self):
        touch = block(self.css, '(pointer:coarse)')
        self.assertRegex(touch, r'button,select,input[^{]*\{[^}]*min-height:44px')

    def test_hover_styles_only_with_a_real_pointer(self):
        outside = re.sub(r'@media\(hover:hover\)\{(?:[^{}]*\{[^}]*\})*[^{}]*\}', '', self.css)
        self.assertNotIn(':hover', outside)

    def test_sideways_phone_fills_the_screen_while_playing(self):
        query = '(pointer:coarse) and (orientation:landscape) and (max-height:540px)'
        self.assertIn('@media' + query, self.css)
        self.assertIn("matchMedia('" + query + "')", self.js)
        self.assertIn('fitSideways', self.js)
