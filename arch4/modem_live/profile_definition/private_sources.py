"""Derive native private labels from admitted rules, never observed values."""
from arch4.privacy_scrub import _is_standard_sensitive_key
from .readable import DefinitionError


def native_privacy_plan(plan):
    keys=set();opaque=False;functions=plan['functions']
    steps=0
    def charge():
        nonlocal steps
        steps+=1
        if steps>200000:raise DefinitionError('/fields: private native source plan work budget exceeded')
    def is_native(node,bindings=None,active=()):
        charge()
        bindings=bindings or{}
        if isinstance(node,list):return any(is_native(v,bindings,active)for v in node)
        if not isinstance(node,dict):return False
        if node.get('op')=='input':return node.get('path',[])[:1]==['replies']
        if node.get('op')=='variable':return is_native(bindings.get(node.get('name')),{},active)
        if node.get('op')=='call':
            name=node.get('rule')
            if name in functions and name not in active:
                return is_native(functions[name]['expression'],node.get('arguments',{}),(*active,name))
        return any(is_native(v,bindings,active)for v in node.values())
    def literal_keys(node,bindings):
        charge()
        if isinstance(node,list)and all(isinstance(v,str)for v in node):return set(node)
        if isinstance(node,dict):
            if node.get('op')=='variable':return literal_keys(bindings.get(node.get('name')),bindings)
            if node.get('op')=='if':
                a=literal_keys(node.get('then'),bindings);b=literal_keys(node.get('else'),bindings)
                return a|b if a is not None and b is not None else None
        return None
    def source_keys(node,bindings=None,active=()):
        nonlocal opaque
        charge()
        bindings=bindings or{};found=set();native=False
        if isinstance(node,list):
            for value in node:
                labels,present=source_keys(value,bindings,active);found.update(labels);native|=present
            return found,native
        if not isinstance(node,dict):return found,native
        operation=node.get('op')
        if operation=='variable'and node.get('name')in bindings:
            return source_keys(bindings[node['name']],{},active)
        if operation=='input':
            path=node.get('path',[])
            if path[:1]==['replies']:
                labels=({path[-1]}if len(path)>2 and isinstance(path[-1],str)else set())
                opaque|=not labels
                fallback,_=source_keys(node.get('default'),bindings,active)
                return labels|fallback,True
        if operation=='get':
            key=node.get('key')
            if isinstance(key,dict)and key.get('op')=='variable':key=bindings.get(key.get('name'))
            # The outer leaf is private, not every structural parent such as
            # the RPC data/result envelope surrounding that leaf.
            if isinstance(key,str)and is_native(node.get('value'),bindings,active):
                fallback,_=source_keys(node.get('default'),bindings,active)
                return {key}|fallback,True
            return source_keys(node.get('value'),bindings,active)
        if operation=='if':
            return source_keys([node.get('then'),node.get('else')],bindings,active)
        if operation=='map':
            return source_keys(node.get('expression'),{**bindings,'item':node.get('items')},active)
        if operation=='filter':return source_keys(node.get('items'),bindings,active)
        if operation=='algorithm':
            args=node.get('args',[])
            if not is_native(args,bindings,active):return set(),False
            # The fixed codec explicitly takes a list of native field names.
            # Do not treat structural envelopes or its guard conditions as
            # private leaves. Other opaque algorithms remain fail-closed.
            if node.get('name')=='device_text'and len(args)>=2:
                labels=literal_keys(args[1],bindings)
                if labels is not None:return labels,True
            if node.get('name')=='_clean_ip_address'and len(args)==1:
                return source_keys(args[0],bindings,active)
            opaque=True
            return set(),True
        if operation=='xml_text':
            path=node.get('path')
            if isinstance(path,str)and path:
                fallback,_=source_keys(node.get('default'),bindings,active)
                return {path.rsplit('/',1)[-1]}|fallback,True
        if operation=='call':
            name=node.get('rule')
            if name in functions and name not in active:
                return source_keys(functions[name]['expression'],node.get('arguments',{}),(*active,name))
        for value in node.values():
            labels,present=source_keys(value,bindings,active);found.update(labels);native|=present
        return found,native
    def private_objects(node):
        nonlocal opaque
        charge()
        if isinstance(node,dict):
            for key,value in node.items():
                if _is_standard_sensitive_key(key):
                    labels,native=source_keys(value);keys.update(labels)
                    opaque|=native and not labels
                private_objects(value)
        elif isinstance(node,list):
            for value in node:private_objects(value)
    for field in plan['fields']:
        if field['read']is None:continue
        expression=field['read']['expression']
        if _is_standard_sensitive_key(field['definition']['address']):
            labels,native=source_keys(expression);keys.update(labels)
            opaque|=native and not labels
        private_objects(expression)
    # Object-valued fields can contain private children even when the outer
    # field itself is public (for example a Wi-Fi row with an SSID).
    for helper in functions.values():private_objects(helper['expression'])
    for write in plan.get('writes',{}).values():
        for name,field in write.get('fields',{}).items():
            if _is_standard_sensitive_key(name):keys.add(field['key'])
    if len(keys)>512 or any(not isinstance(key,str)or len(key)>512 for key in keys):
        raise DefinitionError('/fields: private native source label bounds exceeded')
    return frozenset(keys),bool(opaque)
