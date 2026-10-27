"""Bounded, offline evaluator for the portable JSON rule vocabulary.

No driver imports, eval, Python source, I/O, executable paths or plugins.
Definitions select supplied reply/context data and apply explicit operations.
"""
from __future__ import annotations

import math
import hashlib
import operator
import re
import xml.etree.ElementTree as ET
import json
from copy import deepcopy


class RuleError(ValueError):
    pass


class Evaluator:
    def __init__(self, rules, *, algorithms=None, max_steps=200000, program=None):
        self.rules = rules
        self.max_steps = max_steps
        self.steps = 0
        self.stack = []
        self.algorithms = algorithms
        self.program = program
        from .bounded_regex import RegexBudget
        self.regex = RegexBudget()
        self._machine = None
        self._algorithm_results = {}

    def run(self, expr, inputs, variables=None):
        self.steps += 1
        if self.steps > self.max_steps:
            raise RuleError('rule_step_limit')
        variables = {} if variables is None else variables
        if not isinstance(expr, dict) or 'op' not in expr:
            return expr
        op = expr['op']
        def ev(e): return self.run(e, inputs, variables)
        if op == 'literal': return expr['value']
        if op == 'require':
            if not ev(expr['condition']):raise RuleError(expr['reason'])
            return ev(expr['value'])
        if op == 'xml_text':
            value=ev(expr['value'])
            if not isinstance(value,str) or len(value)>100000 or '<!DOCTYPE' in value.upper():return ev(expr.get('default',''))
            try:
                element=ET.fromstring(value).find(expr['path'])
                return element.text.strip() if element is not None and element.text else ev(expr.get('default',''))
            except ET.ParseError:return ev(expr.get('default',''))
        if op == 'algorithm':
            if self.algorithms is None:raise RuleError('algorithm_library_required')
            if self._machine is None:
                from arch4.modem_live.profile_definition.algorithm import Machine
                self._machine=Machine(self.algorithms['functions'],self.algorithms['constants'],initializers=self.algorithms.get('initializers'),regex_budget=self.regex,program=self.program)
            arguments=[ev(v) for v in expr.get('args',[])]
            if 'variadic_args' in expr:arguments.extend(ev(expr['variadic_args']))
            keywords={k:ev(v) for k,v in expr.get('kwargs',{}).items()}
            # Top-level algorithms are data-to-data transforms. Their local
            # mutation never owns application state; copy cached results.
            key=json.dumps([expr['name'],arguments,keywords],sort_keys=True,allow_nan=False)
            if key not in self._algorithm_results:
                self._algorithm_results[key]=self._machine.invoke(expr['name'],*deepcopy(arguments),**deepcopy(keywords))
            return deepcopy(self._algorithm_results[key])
        if op == 'input':
            value = inputs
            for key in expr['path']:
                try: value = value[key]
                except (KeyError, IndexError, TypeError): return ev(expr.get('default'))
            return value
        if op == 'variable': return variables[expr['name']]
        if op == 'get':
            value, key = ev(expr['value']), ev(expr['key'])
            try: return value[key]
            except (KeyError, IndexError, TypeError): return ev(expr.get('default'))
        if op == 'let':
            scope = dict(variables)
            for name, value in expr['bindings']:
                scope[name] = self.run(value, inputs, scope)
            return self.run(expr['result'], inputs, scope)
        if op == 'if': return ev(expr['then'] if ev(expr['condition']) else expr['else'])
        if op in {'and', 'or'}:
            result = None
            for arg in expr['args']:
                result = ev(arg)
                if (op == 'and' and not result) or (op == 'or' and result): break
            return result
        if op == 'first_present':
            for arg in expr['args']:
                value = ev(arg)
                if value is not None: return value
            return None
        if op == 'try':
            try: return ev(expr['value'])
            except (ValueError, TypeError, OverflowError, ZeroDivisionError): return ev(expr.get('default'))
        if op == 'call':
            name = expr['rule']
            if name in self.stack: raise RuleError('recursive_rule:' + name)
            if name not in self.rules: raise RuleError('unknown_rule:' + name)
            rule = self.rules[name]
            args = {name: ev(value) for name, value in expr.get('arguments', {}).items()}
            if set(args) != set(rule['parameters']): raise RuleError('rule_arguments:' + name)
            self.stack.append(name)
            try: return self.run(rule['expression'], inputs, args)
            finally: self.stack.pop()
        if op in {'map', 'filter', 'every', 'some'}:
            sequence = ev(expr['items'])
            if not isinstance(sequence, (list, tuple)) or len(sequence) > 10000:
                raise RuleError('bounded_list_required')
            values = []
            for item in sequence:
                scope = {**variables, expr.get('as', 'item'): item}
                value = self.run(expr['expression'], inputs, scope)
                values.append(item if op == 'filter' and value else value if op != 'filter' else None)
            if op == 'filter': return [v for v in values if v is not None]
            if op == 'every': return all(values)
            if op == 'some': return any(values)
            return values
        if op == 'object': return {k: ev(v) for k, v in expr['members'].items()}
        if op == 'list': return [ev(v) for v in expr['items']]
        args = [ev(v) for v in expr.get('args', [])]
        binary = {'eq': operator.eq, 'ne': operator.ne, 'lt': operator.lt, 'le': operator.le,
                  'gt': operator.gt, 'ge': operator.ge, 'add': operator.add, 'subtract': operator.sub,
                  'multiply': operator.mul, 'divide': operator.truediv, 'floor_divide': operator.floordiv,
                  'modulo': operator.mod, 'bit_and': operator.and_, 'shift_left': operator.lshift}
        if op in binary:
            if op in {'eq','ne','lt','le','gt','ge'}:return binary[op](*args)
            from .limits import binary as bounded_binary
            return bounded_binary(op,*args)
        if op == 'not': return not args[0]
        if op == 'contains': return args[1] in args[0]
        if op == 'is_type':
            return type(args[0]).__name__ == expr['type']
        if op == 'text': return str(args[0])
        if op == 'strip': return args[0].strip(expr.get('characters'))
        if op == 'lower': return args[0].lower()
        if op == 'upper': return args[0].upper()
        if op == 'replace':
            from .limits import replace
            return replace(args[0],expr['old'],expr['new'])
        if op == 'split': return args[0].split(expr.get('separator'))
        if op == 'join':
            from .limits import join
            return join(expr['separator'],args[0])
        if op == 'length': return len(args[0])
        if op == 'slice': return args[0][expr.get('start'):expr.get('stop')]
        if op == 'entries': return [list(pair) for pair in args[0].items()]
        if op == 'keys': return list(args[0])
        if op == 'concat': return [item for arg in args for item in arg]
        if op == 'flatten': return [item for arg in args[0] for item in arg]
        if op == 'unique':
            result = []
            for value in args[0]:
                if value not in result: result.append(value)
            return result
        if op == 'integer': return int(args[0], expr['base']) if isinstance(args[0], str) and 'base' in expr else int(args[0])
        if op == 'number': return float(args[0])
        if op == 'finite': return math.isfinite(args[0])
        if op == 'integral': return float(args[0]).is_integer()
        if op == 'abs': return abs(args[0])
        if op == 'round': return round(args[0], expr.get('digits',0))
        if op == 'sha256_utf8': return hashlib.sha256(args[0].encode('utf-8')).hexdigest()
        if op == 'indexed': return [[index,value] for index,value in enumerate(args[0])]
        if op == 'log10': return math.log10(args[0])
        if op == 'minimum': return min(args)
        if op == 'maximum': return max(args)
        if op == 'sorted_unique': return sorted(set(args[0]))
        if op == 'intersection': return sorted(set(args[0]).intersection(*[set(a) for a in args[1:]]))
        if op == 'enum': return expr['values'].get(str(args[0]), expr.get('default'))
        if op in {'match', 'extract', 'find_all'}:
            flags = re.IGNORECASE if expr.get('ignore_case') else 0
            value = str(args[0])
            if len(value) > 100000: raise RuleError('text_limit')
            if op == 'find_all': return self.regex.call('findall',expr['pattern'],value,flags=flags)
            match = self.regex.call('fullmatch'if op=='match'else'search',expr['pattern'],value,flags=flags)
            if op == 'match': return match is not None
            return match.group(expr.get('group', 0)) if match else expr.get('default')
        raise RuleError('unknown_operation:' + op)
