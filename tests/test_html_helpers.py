"""HTML extraction must handle tag case, quoted delimiters and script raw text."""

import pytest

from tests.html_helpers import NavigationHTML


@pytest.mark.parametrize('opening', ['script', 'SCRIPT', 'ScRiPt'])
@pytest.mark.parametrize('closing', ['script', 'SCRIPT', 'sCrIpT'])
def test_inline_script_case_and_raw_text(opening, closing):
    script = '\nconst text = "<input>&amp;";\n// <!-- raw script text -->\n'
    html = f'<{opening} type="text/javascript" data-note=">">{script}</{closing}>'
    assert NavigationHTML(html).scripts == [script]


def test_script_extraction_ignores_comments_external_scripts_and_escaped_tags():
    html = ('<!-- <script>commented()</script> -->'
            '&lt;script&gt;escaped()&lt;/script&gt;'
            '<SCRIPT SRC="external.js">external()</SCRIPT>'
            '<script>first()</script><script>second()</script>')
    assert NavigationHTML(html).scripts == ['first()', 'second()']


@pytest.mark.parametrize('tag', ['input', 'INPUT', 'InPuT'])
@pytest.mark.parametrize('ending', ['>', '/>'])
def test_date_picker_preserves_original_markup(tag, ending):
    picker = f'<{tag}\nTYPE="date" data-note="a > b" ID=\'date-picker\' {ending}'
    html = '<input id="other"><!-- <input id="date-picker"> -->' + picker
    assert NavigationHTML(html).date_picker == picker


@pytest.mark.parametrize('tag', ['body', 'BODY', 'BoDy'])
def test_browser_result_attribute_case_and_entities(tag):
    html = f'<{tag} DATA-RESULT="unsafe &quot;href&quot; &amp; value"></{tag}>'
    assert NavigationHTML(html).body_attributes['data-result'] == 'unsafe "href" & value'


def test_missing_navigation_markup_remains_missing():
    parsed = NavigationHTML('<p>No navigation here</p>')
    assert parsed.scripts == []
    assert parsed.date_picker is None
    assert parsed.body_attributes == {}
