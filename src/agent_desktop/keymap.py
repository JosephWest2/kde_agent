"""Physical key names, chord parsing, US-layout text mapping and `type` routing. Pure; no native imports.

Every name maps to exactly one Linux evdev key code (linux/input-event-codes.h).
The private desktop uses KWin's default US keyboard layout, so ``type`` maps
characters to the physical keys that produce them there, adding Shift as needed.
Text with any other character goes as one input-method commit instead (``route``).
"""
from __future__ import annotations

from .contracts import ContractError

MAX_CHORD = 8
SHIFT = 42
CAPS_LOCK = 58
# `type --method`: auto (keys when every character has a US key, else the input method), keys, input-method.
TYPE_METHODS = ('auto', 'keys', 'input-method')
MAX_COMMIT_BYTES = 4000  # One input-method commit: text-input-v3's limit, inside libwayland's 4096-byte message.

_LETTERS = dict(zip('qwertyuiop', range(16, 26))) | dict(zip('asdfghjkl', range(30, 39))) \
    | dict(zip('zxcvbnm', range(44, 51)))
_DIGITS = dict(zip('1234567890', range(2, 12)))
# Unshifted punctuation on the US layout, by character and by xdotool keysym name.
_PUNCTUATION = {'-': ('minus', 12), '=': ('equal', 13), '[': ('bracketleft', 26),
                ']': ('bracketright', 27), ';': ('semicolon', 39), "'": ('apostrophe', 40),
                '`': ('grave', 41), '\\': ('backslash', 43), ',': ('comma', 51),
                '.': ('period', 52), '/': ('slash', 53)}
_SHIFTED = dict(zip('!@#$%^&*()', '1234567890')) | {
    '_': '-', '+': '=', '{': '[', '}': ']', ':': ';', '"': "'", '~': '`', '|': '\\',
    '<': ',', '>': '.', '?': '/'}

# Canonical name -> code, then aliases. Lookup is case-insensitive.
KEYS = {
    'escape': 1, 'backspace': 14, 'tab': 15, 'return': 28, 'space': 57, 'delete': 111,
    'insert': 110, 'home': 102, 'end': 107, 'page_up': 104, 'page_down': 109,
    'up': 103, 'down': 108, 'left': 105, 'right': 106,
    'ctrl': 29, 'ctrl_r': 97, 'shift': 42, 'shift_r': 54, 'alt': 56, 'alt_r': 100,
    'super': 125, 'super_r': 126, 'menu': 127, 'caps_lock': 58, 'num_lock': 69,
    'scroll_lock': 70, 'print': 99, 'pause': 119,
    'kp_0': 82, 'kp_1': 79, 'kp_2': 80, 'kp_3': 81, 'kp_4': 75, 'kp_5': 76, 'kp_6': 77,
    'kp_7': 71, 'kp_8': 72, 'kp_9': 73, 'kp_decimal': 83, 'kp_enter': 96, 'kp_add': 78,
    'kp_subtract': 74, 'kp_multiply': 55, 'kp_divide': 98,
    **{f'f{n}': code for n, code in zip(range(1, 11), range(59, 69))}, 'f11': 87, 'f12': 88,
    **_LETTERS, **_DIGITS,
    **{name: code for name, code in _PUNCTUATION.values()},
}
ALIASES = {
    'esc': 'escape', 'enter': 'return', 'del': 'delete', 'ins': 'insert',
    'pageup': 'page_up', 'prior': 'page_up', 'pagedown': 'page_down', 'next': 'page_down',
    'control': 'ctrl', 'control_l': 'ctrl', 'ctrl_l': 'ctrl', 'control_r': 'ctrl_r',
    'shift_l': 'shift', 'alt_l': 'alt', 'altgr': 'alt_r', 'iso_level3_shift': 'alt_r',
    'super_l': 'super', 'meta': 'super', 'meta_l': 'super', 'win': 'super', 'meta_r': 'super_r',
    'capslock': 'caps_lock', 'numlock': 'num_lock', 'scrolllock': 'scroll_lock',
    'sysrq': 'print', 'kp_plus': 'kp_add', 'kp_minus': 'kp_subtract', 'kp_period': 'kp_decimal',
    'kp_return': 'kp_enter', 'dot': 'period', 'quoteleft': 'grave', 'backquote': 'grave',
    **{char: name for char, (name, _) in _PUNCTUATION.items()},
}


def _unsupported(message, **context):
    raise ContractError('unsupported_input', message, context=context)


def key_code(name):
    if not isinstance(name, str) or not name:
        _unsupported('Empty key name in chord.')
    if not name.isascii():
        _unsupported('Key names are ASCII.', key=name[:32])
    lowered = name.lower()
    code = KEYS.get(ALIASES.get(lowered, lowered))
    if code is None:
        hint = None
        if name in _SHIFTED:
            hint = f'"{name}" needs Shift; use shift+{_PUNCTUATION.get(_SHIFTED[name], (_SHIFTED[name],))[0]} or `type`.'
        _unsupported('Unknown key name.', key=name[:32], hint=hint)
    return code


def parse_chord(chord):
    """'ctrl+shift+t' -> [29, 42, 20]; pressed in order, released in reverse."""
    if not isinstance(chord, str) or not chord:
        _unsupported('Empty chord.')
    names = chord.split('+')
    if len(names) > MAX_CHORD:
        _unsupported(f'A chord has at most {MAX_CHORD} keys.')
    codes = [key_code(name) for name in names]
    if len(set(codes)) != len(codes):
        _unsupported('A chord cannot repeat a key.', chord=chord[:64])
    return codes


def text_strokes(text, *, caps_lock=False):
    """US-layout keystrokes for TEXT; each stroke is pressed together then released.

    Rejects the whole text if any character has no mapping, before anything is sent.
    With Caps Lock on, letters invert their Shift; other keys are unaffected.
    """
    strokes = []
    for index, char in enumerate(text):
        if char in _LETTERS:
            strokes.append([SHIFT, _LETTERS[char]] if caps_lock else [_LETTERS[char]])
        elif 'A' <= char <= 'Z':  # ASCII only: 'K' (Kelvin) also lowercases to 'k'.
            strokes.append([_LETTERS[char.lower()]] if caps_lock else [SHIFT, _LETTERS[char.lower()]])
        elif char in _DIGITS:
            strokes.append([_DIGITS[char]])
        elif char in _PUNCTUATION:
            strokes.append([_PUNCTUATION[char][1]])
        elif char in _SHIFTED:
            base = _SHIFTED[char]
            strokes.append([SHIFT, _DIGITS[base] if base in _DIGITS else _PUNCTUATION[base][1]])
        elif char == ' ':
            strokes.append([KEYS['space']])
        elif char == '\n':
            strokes.append([KEYS['return']])
        elif char == '\t':
            strokes.append([KEYS['tab']])
        else:
            _unsupported('Text contains a character the US layout cannot type.',
                         index=index, codepoint=f'U+{ord(char):04X}')
    return strokes


_TYPEABLE = frozenset(_LETTERS) | frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZ') | frozenset(_DIGITS) \
    | frozenset(_PUNCTUATION) | frozenset(_SHIFTED) | frozenset(' \n\t')


def typeable(text):
    """True if the US layout can type every character of TEXT (``text_strokes`` would accept it)."""
    return _TYPEABLE.issuperset(text)


def route(text, method='auto'):
    """'keys' or 'input_method': how ``type`` sends TEXT under METHOD.

    auto keeps today's key path for text the US layout can type, and commits
    any other text whole through the input method. keys always uses keys (and
    rejects what it cannot type); input-method always commits.
    """
    if method == 'keys' or (method == 'auto' and typeable(text)):
        return 'keys'
    return 'input_method'


def commit_text(text):
    """TEXT as the UTF-8 bytes of one commit; rejects characters UTF-8 cannot carry (lone surrogates)."""
    for index, char in enumerate(text):
        if 0xd800 <= ord(char) <= 0xdfff:
            _unsupported('Text contains a lone surrogate, which is not a character.',
                         index=index, codepoint=f'U+{ord(char):04X}')
    return text.encode('utf-8')
