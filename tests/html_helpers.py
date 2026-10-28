"""Extract inline scripts and navigation markup from test pages."""

from html.parser import HTMLParser


class NavigationHTML(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.scripts = []
        self.date_picker = None
        self.body_attributes = {}
        self._script_parts = None
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and 'src' not in attrs:
            self._script_parts = []
        elif tag == 'input' and attrs.get('id') == 'date-picker':
            self.date_picker = self.get_starttag_text()
        elif tag == 'body':
            self.body_attributes = attrs

    def handle_data(self, data):
        if self._script_parts is not None:
            self._script_parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self._script_parts is not None:
            self.scripts.append(''.join(self._script_parts))
            self._script_parts = None
