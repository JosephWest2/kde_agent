"""Caller paths are normalized locally; wire paths are validated, never repaired."""
from dataclasses import replace
import os
from .contracts import ContractError


def absolute(value, base):
    if not isinstance(value, str) or not value or '\0' in value:
        raise ContractError('invalid_arguments', 'Invalid path.')
    return os.path.normpath(os.path.join(base, value))


def fields(operation):
    return {'launch': ('cwd',), 'session.start': ('artifacts', 'dependency_root'),
            'screenshot': ('output',), 'doctor': ('dependency_root',)}.get(operation, ())


def normalize(request):
    args = dict(request.arguments)
    cwd = absolute(request.caller_cwd, '/')
    for field in fields(request.operation):
        value = args.get(field)
        if field == 'cwd' and value is None:
            value = cwd
        if value is not None:
            args[field] = absolute(value, cwd)
    return replace(request, caller_cwd=cwd, arguments=args)


def validate_wire(operation, caller_cwd, arguments):
    values = [caller_cwd]
    values.extend(arguments.get(key) for key in fields(operation)
                  if key == 'cwd' or arguments.get(key) is not None)
    for value in values:
        if (not isinstance(value, str) or not value.startswith('/') or '\0' in value
                or os.path.normpath(value) != value):
            raise ContractError('protocol_error', 'Request paths must be normalized absolute paths.')
