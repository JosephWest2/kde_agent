"""Bounded title matching for `wait --for title`; never backtracks.

Python's re can backtrack exponentially (`(a|a)*b`, `a?a?...aa`, `.*.*.*x`)
inside one C call that holds the GIL, so neither a thread nor a budget can stop
it on the owner. Regex patterns therefore use a small documented subset of
Python syntax, compiled once into a Glushkov position automaton (one bit per
character position, no epsilon transitions) and run bit-parallel. Matching is
`re.search` semantics: case-sensitive, unanchored unless the pattern starts with
`^` or ends with `$`. Each title character costs at most ceil(positions / 8)
table lookups, and `Search.step` stops after a fixed work budget so a scheduler
step stays short; a 4096-character title needs a bounded number of steps.

Subset: literal characters, `.` (any character except newline), bracket
classes (`[abc]`, `[^a-z]`, ranges, `\\d \\w \\s` and their negations inside),
`\\d \\D \\w \\W \\s \\S`, escaped punctuation, `\\t \\n \\r \\f \\v`, groups
`( )` and `(?: )`, alternation `|`, and the quantifiers `* + ?`. `^` only as the
first and `$` only as the last character. Anything else (counted repetition
`{m,n}`, lazy or possessive quantifiers, backreferences, lookaround, `\\b`, flags,
other escapes) is rejected rather than reinterpreted, as is a pattern that can
match the empty string (it would match every title).
"""
from .contracts import ContractError

MAX_PATTERN = 256
# Lookups of 8-bit follow-table chunks per Search.step (about a millisecond on a laptop).
STEP_BUDGET = 16384
SPECIAL = set('\\.[]()|*+?^${}')
CLASS_ESCAPES = {
    'd': lambda c: c.isdecimal(), 'D': lambda c: not c.isdecimal(),
    'w': lambda c: c.isalnum() or c == '_', 'W': lambda c: not (c.isalnum() or c == '_'),
    's': lambda c: c.isspace(), 'S': lambda c: not c.isspace(),
}
CONTROL_ESCAPES = {'t': '\t', 'n': '\n', 'r': '\r', 'f': '\f', 'v': '\v'}


def rejected(reason, position):
    raise ContractError('invalid_arguments', 'Invalid or unsupported --match pattern; see docs/CLI.md.',
                        context={'field': 'match', 'reason': reason, 'position': position})


class Parser:
    """Recursive descent over at most MAX_PATTERN characters (bounded depth)."""

    def __init__(self, pattern, begin, end):
        # Positions in errors index the full pattern, anchors included.
        self.text, self.at, self.end = pattern, begin, end
        self.atoms = []  # predicate per position: a str literal or a (key, callable) class

    def peek(self, ahead=0):
        return self.text[self.at + ahead] if self.at + ahead < self.end else None

    def alternation(self, depth):
        if depth > 32:
            rejected('nesting_too_deep', self.at)
        branches = [self.sequence(depth)]
        while self.peek() == '|':
            self.at += 1
            branches.append(self.sequence(depth))
        return branches[0] if len(branches) == 1 else ('alt', branches)

    def sequence(self, depth):
        items = []
        while self.peek() not in (None, '|', ')'):
            items.append(self.repeat(depth))
        return ('cat', items)

    def repeat(self, depth):
        node = self.atom(depth)
        if self.peek() in ('*', '+', '?'):
            node = (self.text[self.at], node)
            self.at += 1
            if self.peek() in ('*', '+', '?', '{'):
                rejected('unsupported_quantifier', self.at)
        elif self.peek() == '{':
            rejected('unsupported_quantifier', self.at)
        return node

    def position(self, predicate):
        self.atoms.append(predicate)
        return ('pos', len(self.atoms) - 1)

    def atom(self, depth):
        char, start = self.peek(), self.at
        self.at += 1
        if char == '(':
            if self.peek() == '?':
                if self.peek(1) != ':':
                    rejected('unsupported_group', start)
                self.at += 2
            node = self.alternation(depth + 1)
            if self.peek() != ')':
                rejected('unbalanced_parenthesis', start)
            self.at += 1
            return node
        if char == '.':
            return self.position(('.', lambda c: c != '\n'))
        if char == '[':
            return self.position(self.bracket(start))
        if char == '\\':
            escaped = self.escape(start)
            return self.position(escaped)
        if char in ('*', '+', '?'):
            rejected('nothing_to_repeat', start)
        if char in SPECIAL - {'}'}:
            rejected('unsupported_syntax', start)
        return self.position(char)

    def escape(self, start):
        char = self.peek()
        if char is None:
            rejected('trailing_backslash', start)
        self.at += 1
        if char in CLASS_ESCAPES:
            return '\\' + char, CLASS_ESCAPES[char]
        if char in CONTROL_ESCAPES:
            return CONTROL_ESCAPES[char]
        if char.isascii() and not char.isalnum():
            return char
        rejected('unsupported_escape', start)

    def bracket(self, start):
        negate = self.peek() == '^'
        if negate:
            self.at += 1
        singles, ranges, classes = set(), [], []
        first = True
        while True:
            char = self.peek()
            if char is None:
                rejected('unterminated_class', start)
            if char == ']' and not first:
                self.at += 1
                break
            first = False
            if char == '[':
                rejected('unsupported_syntax', self.at)
            item_start = self.at
            self.at += 1
            if char == '\\':
                char = self.escape(item_start)
                if isinstance(char, tuple):
                    classes.append(char)
                    continue
            if self.peek() == '-' and self.peek(1) not in (None, ']'):
                self.at += 1
                end_start = self.at
                end = self.peek()
                self.at += 1
                if end in ('[', ']'):
                    rejected('unsupported_syntax', end_start)
                if end == '\\':
                    end = self.escape(end_start)
                    if isinstance(end, tuple):
                        rejected('invalid_range', item_start)
                if end < char:
                    rejected('invalid_range', item_start)
                ranges.append((char, end))
            else:
                singles.add(char)
        key = ('[', negate, frozenset(singles), tuple(ranges), tuple(sorted(name for name, _ in classes)))

        def member(c):
            found = c in singles or any(lo <= c <= hi for lo, hi in ranges) or any(test(c) for _, test in classes)
            return found != negate
        return key, member


def glushkov(node, follow):
    """Returns (nullable, first mask, last mask); fills follow[position]."""
    kind = node[0]
    if kind == 'pos':
        bit = 1 << node[1]
        return False, bit, bit
    if kind == 'cat':
        nullable, first, last = True, 0, 0
        for item in node[1]:
            n, f, l = glushkov(item, follow)
            link(last, f, follow)
            first |= f if nullable else 0
            last = l | (last if n else 0)
            nullable = nullable and n
        return nullable, first, last
    if kind == 'alt':
        nullable, first, last = False, 0, 0
        for item in node[1]:
            n, f, l = glushkov(item, follow)
            nullable, first, last = nullable or n, first | f, last | l
        return nullable, first, last
    n, f, l = glushkov(node[1], follow)
    if kind in ('*', '+'):
        link(l, f, follow)
    return n or kind in ('*', '?'), f, l


def link(sources, targets, follow):
    while sources:
        low = sources & -sources
        follow[low.bit_length() - 1] |= targets
        sources ^= low


class Matcher:
    def __init__(self, text, regex):
        self.text, self.regex = text, regex
        if not regex:
            return
        # An odd run of backslashes before a final '$' escapes it.
        start = text.startswith('^')
        end = text.endswith('$') and (len(text) - 1 - len(text[:-1].rstrip('\\'))) % 2 == 0
        parser = Parser(text, 1 if start else 0, len(text) - 1 if end else len(text))
        tree = parser.alternation(0)
        if parser.at != parser.end:
            rejected('unbalanced_parenthesis', parser.at)
        if (start or end) and tree[0] == 'alt':
            # Python binds `^a|b` as `(?:^a)|b`; require explicit grouping instead.
            rejected('anchor_with_alternation', 0)
        follow = [0] * len(parser.atoms)
        nullable, self.first, self.last = glushkov(tree, follow)
        if nullable:
            rejected('matches_empty', 0)
        self.anchored_start, self.anchored_end = start, end
        self.literals, self.classes = {}, {}
        for index, atom in enumerate(parser.atoms):
            if isinstance(atom, str):
                self.literals[atom] = self.literals.get(atom, 0) | 1 << index
            else:
                key, test = atom
                mask, _ = self.classes.get(key, (0, test))
                self.classes[key] = (mask | 1 << index, test)
        self.classes = list(self.classes.values())
        # Follow union for each byte of the active set: chunk k, value v.
        self.tables = []
        for chunk in range(0, len(follow), 8):
            table = [0] * 256
            for value in range(1, 256):
                low = value & -value
                index = chunk + low.bit_length() - 1
                table[value] = table[value ^ low] | (follow[index] if index < len(follow) else 0)
            self.tables.append(table)

    def search(self, title):
        return Search(self, title)

    def mask(self, char, cache):
        value = cache.get(char)
        if value is None:
            value = self.literals.get(char, 0)
            for bits, test in self.classes:
                if test(char):
                    value |= bits
            cache[char] = value
        return value


class Search:
    """One bounded matching run; step() returns None until it knows the answer."""

    def __init__(self, matcher, title):
        self.matcher, self.title = matcher, title
        self.index, self.active, self.cache = 0, 0, {}
        self.result = None
        if not isinstance(title, str) or not title:
            self.result = False  # A null or empty title never matches.
        elif not matcher.regex:
            self.result = matcher.text in title

    def step(self, budget=STEP_BUDGET):
        if self.result is not None:
            return self.result
        m, title, active = self.matcher, self.title, self.active
        tables, last, size = m.tables, m.last, len(self.title)
        while self.index < size:
            if budget <= 0:
                self.active = active
                return None
            char = title[self.index]
            reached, chunk, rest = 0, 0, active
            while rest:
                byte = rest & 255
                if byte:
                    reached |= tables[chunk][byte]
                rest >>= 8
                chunk += 1
            budget -= chunk + 1
            if self.index == 0 or not m.anchored_start:
                reached |= m.first
            active = reached & m.mask(char, self.cache)
            self.index += 1
            # Like Python, `$` also matches before one final newline.
            if active & last and (not m.anchored_end or self.index == size
                                  or (self.index == size - 1 and title[-1] == '\n')):
                self.result = True
                return True
            if not active and m.anchored_start:
                break
        self.result = False
        return False


def compile_match(text, regex):
    """Validate at request time; the worker compiles the same text again."""
    if not isinstance(text, str) or not text or '\0' in text:
        rejected('empty' if text == '' else 'invalid', 0)
    if len(text) > MAX_PATTERN:
        rejected('too_long', MAX_PATTERN)
    return Matcher(text, regex)
