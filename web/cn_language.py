"""Chinese setup language and public CN error text, with no runtime conversion dependency."""
import json
from pathlib import Path

import db_reader
import i18n

LANGUAGES = ('zh-CN', 'zh-TW')
_MESSAGES = json.loads(Path(__file__).with_name('cn_messages.json').read_text(encoding='utf-8'))
_PHRASES = sorted(_MESSAGES, key=len, reverse=True)


def language(requested=None):
    if requested in LANGUAGES:
        return requested
    stored = db_reader.get_language()
    return stored if stored in LANGUAGES else 'zh-CN'


def localize_error(message, lang=None):
    text = str(message)
    if (lang or db_reader.get_language()) == 'zh-CN':
        for phrase in _PHRASES:
            text = text.replace(phrase, _MESSAGES[phrase])
    return text


def setup_context(requested=None, error=''):
    lang = language(requested)
    keys = [key for key in i18n._T['en'] if key.startswith('cn_') or key == 'setup_language']
    return {'lang': lang, 't': i18n.get_t(lang), 'error': localize_error(error, lang),
            'cn_setup_texts': {code: {key: i18n.get_t(code)(key) for key in keys} for code in LANGUAGES}}
