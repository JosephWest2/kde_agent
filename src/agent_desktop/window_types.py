"""Strict typed compositor observations; no process authority is derived here."""
from dataclasses import dataclass, asdict
import json
import math
import re
import uuid
from .contracts import ContractError
from .protocol import decode

MAX_BYTES = 256 * 1024
MAX_WINDOWS = 256


def invalid():
    raise ContractError('window_query_failed', 'Invalid structured window observation.')


def encoded(request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r'[a-z0-9-]{1,128}', request_id):
        invalid()
    return 'const request = ' + json.dumps({'request_id': request_id}, ensure_ascii=True, allow_nan=False) + ';\n'


def window_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[0-9a-fA-F-]{36}|\{[0-9a-fA-F-]{36}\})', value):
        invalid()
    try:
        result = str(uuid.UUID(value.strip('{}')))
    except ValueError:
        invalid()
    if result != value.strip('{}').lower():
        invalid()
    return result


@dataclass(frozen=True)
class Bounds:
    x: float
    y: float
    width: float
    height: float

    @classmethod
    def parse(cls, value):
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {'x', 'y', 'width', 'height'}:
            invalid()
        for key, number in value.items():
            if type(number) not in (int, float) or (abs(number) > 1e100 or not math.isfinite(number)) or (key in ('width', 'height') and number <= 0):
                invalid()
        return cls(**value)


@dataclass(frozen=True)
class Window:
    uuid: str
    pid: int | None
    title: str | None
    resource_class: str | None
    client: Bounds | None
    frame: Bounds | None
    active: bool

    @classmethod
    def parse(cls, row, active):
        if not isinstance(row, dict) or set(row) != {'uuid', 'pid', 'title', 'class', 'client', 'frame', 'active'}:
            invalid()
        ident = window_id(row['uuid'])
        if row['pid'] is not None and (type(row['pid']) is not int or not 0 < row['pid'] < 2**31):
            invalid()
        for key in ('title', 'class'):
            if row[key] is not None and (not isinstance(row[key], str) or len(row[key]) > 4096):
                invalid()
        if type(row['active']) is not bool or row['active'] != (active == ident):
            invalid()
        return cls(ident, row['pid'], row['title'], row['class'], Bounds.parse(row['client']), Bounds.parse(row['frame']), row['active'])

    def wire(self, generation):
        return {'window': {'generation': generation, 'window_id': self.uuid}, 'pid': self.pid,
                'title': self.title, 'class': self.resource_class,
                'client': None if self.client is None else asdict(self.client),
                'frame': None if self.frame is None else asdict(self.frame), 'active': self.active}


@dataclass(frozen=True)
class Output:
    name: str
    width: int
    height: int
    scale: float | None


class Decoder:
    """One bounded JSON parse, then at most 16 rows per owner turn."""
    def __init__(self, raw, request_id):
        if len(raw) > MAX_BYTES:
            invalid()
        try:
            value = decode(raw)
        except ContractError:
            invalid()
        if (set(value) != {'schema_version', 'request_id', 'active_uuid', 'windows', 'outputs'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['request_id'] != request_id or not isinstance(value['windows'], list)
                or len(value['windows']) > MAX_WINDOWS):
            invalid()
        self.active = None if value['active_uuid'] is None else window_id(value['active_uuid'])
        outputs = value['outputs']
        if not isinstance(outputs, list) or len(outputs) != 1:
            invalid()
        o = outputs[0]
        if (not isinstance(o, dict) or set(o) != {'name', 'width', 'height', 'scale'}
                or not isinstance(o['name'], str) or not 0 < len(o['name']) <= 256
                or type(o['width']) is not int or o['width'] != 1280
                or type(o['height']) is not int or o['height'] != 720
                or (o['scale'] is not None and (type(o['scale']) not in (int, float) or o['scale'] != 1))):
            invalid()
        self.output = Output(**o)
        self.pending, self.windows, self.seen = iter(value['windows']), [], set()

    def step(self, deadline, now):
        for _ in range(16):
            if now() >= deadline:
                return False
            try:
                row = next(self.pending)
            except StopIteration:
                if self.active is not None and self.active not in self.seen:
                    invalid()
                self.windows.sort(key=lambda w: w.uuid)
                return True
            window = Window.parse(row, self.active)
            if window.uuid in self.seen:
                invalid()
            self.seen.add(window.uuid)
            self.windows.append(window)
        return False
