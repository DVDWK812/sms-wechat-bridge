"""Shared literal SMS filtering; no message text is logged or persisted."""
FILTER_FIELDS = ('SMS_FILTER_MODE', 'SMS_FILTER_KEYWORD')
FILTER_MODES = {'contains', 'not_contains', 'all'}
DEFAULT_MODE = 'contains'
DEFAULT_KEYWORD = '验证码'


def validate_filter(mode, keyword):
    if not isinstance(mode, str) or mode not in FILTER_MODES:
        raise ValueError('invalid_sms_filter_mode')
    if (not isinstance(keyword, str) or len(keyword) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in keyword)):
        raise ValueError('invalid_sms_filter_keyword')
    if mode != 'all' and not keyword.strip():
        raise ValueError('empty_sms_filter_keyword')
    return mode, keyword


def filter_settings(config):
    return validate_filter(config.get(FILTER_FIELDS[0], DEFAULT_MODE),
                           config.get(FILTER_FIELDS[1], DEFAULT_KEYWORD))


def filter_environment(config):
    return dict(zip(FILTER_FIELDS, filter_settings(config)))


def matches_sms(content, mode, keyword):
    mode, keyword = validate_filter(mode, keyword)
    if not isinstance(content, str):
        return False
    if mode == 'all':
        return True
    found = keyword in content
    return found if mode == 'contains' else not found


def describe_filter(config):
    mode, keyword = filter_settings(config)
    if mode == 'all':
        return '无关键字限制：转发全部非空短信全文'
    action = '包含' if mode == 'contains' else '不包含'
    return f'正文{action}关键字“{keyword}”：转发全文'
