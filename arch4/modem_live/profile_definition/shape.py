"""Validate the deliberately small JSON Schema subset used by schema.json.

This is not a general JSON Schema implementation. Unsupported keywords fail
closed so extending the schema cannot silently weaken validation. No downloads.
"""
import json
import math
import re


class ShapeError(ValueError):
    pass


KEYWORDS = {'$schema', '$id', '$defs', 'title', 'description', '$ref', 'type',
            'additionalProperties', 'required', 'properties', 'const', 'enum',
            'items', 'uniqueItems', 'minItems', 'minLength', 'minimum', 'pattern',
            'oneOf', 'if', 'then', 'else'}


def _kind(value, kind):
    return {
        'null': value is None,
        'boolean': type(value) is bool,
        'integer': type(value) is int,
        'number': type(value) in (int, float) and math.isfinite(value),
        'string': type(value) is str,
        'array': type(value) is list,
        'object': type(value) is dict,
    }.get(kind, False)


def validate_shape(value, schema, root=None):
    root = schema if root is None else root
    if set(schema) - KEYWORDS:
        raise ShapeError('unsupported_schema_keyword')
    if '$ref' in schema:
        ref = schema['$ref']
        if not ref.startswith('#/'):
            raise ShapeError('external_schema_reference_forbidden')
        target = root
        for part in ref[2:].split('/'):
            target = target[part.replace('~1', '/').replace('~0', '~')]
        validate_shape(value, target, root)
    if 'type' in schema:
        kinds = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
        if not any(_kind(value, kind) for kind in kinds): raise ShapeError('wrong_type')
    if 'const' in schema and (type(value) is not type(schema['const']) or value != schema['const']):
        raise ShapeError('wrong_constant')
    if 'enum' in schema and not any(type(value) is type(v) and value == v for v in schema['enum']):
        raise ShapeError('wrong_enum')
    if 'oneOf' in schema:
        matches = 0
        for branch in schema['oneOf']:
            try: validate_shape(value, branch, root)
            except ShapeError: pass
            else: matches += 1
        if matches != 1: raise ShapeError('oneof_mismatch')
    if 'if' in schema:
        try: validate_shape(value, schema['if'], root)
        except ShapeError: branch = schema.get('else', {})
        else: branch = schema.get('then', {})
        validate_shape(value, branch, root)
    if isinstance(value, dict):
        if set(schema.get('required', ())) - set(value): raise ShapeError('required_key_missing')
        properties = schema.get('properties', {})
        for key, item in value.items():
            if key in properties: validate_shape(item, properties[key], root)
            elif schema.get('additionalProperties') is False: raise ShapeError('unknown_key')
            elif isinstance(schema.get('additionalProperties'), dict):
                validate_shape(item, schema['additionalProperties'], root)
    elif isinstance(value, list):
        if len(value) < schema.get('minItems', 0): raise ShapeError('array_too_short')
        if schema.get('uniqueItems') and len({json.dumps(v, sort_keys=True, allow_nan=False) for v in value}) != len(value):
            raise ShapeError('duplicate_array_item')
        if 'items' in schema:
            for item in value: validate_shape(item, schema['items'], root)
    elif isinstance(value, str):
        if len(value) < schema.get('minLength', 0): raise ShapeError('string_too_short')
        if 'pattern' in schema and re.search(schema['pattern'], value) is None:
            raise ShapeError('pattern_mismatch')
    elif type(value) in (int, float):
        if not math.isfinite(value): raise ShapeError('nonfinite_number')
        if 'minimum' in schema and value < schema['minimum']: raise ShapeError('below_minimum')
