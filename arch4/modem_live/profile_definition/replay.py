"""Standalone definition replay. No catalogue, source extraction or driver imports."""
from copy import deepcopy
from arch4.modem_live.profile_definition.expressions import Evaluator,RuleError
from .dependencies import reply_dependencies


class _WithheldInputEvaluator(Evaluator):
    """Offline evidence observer; do not replace or invent masked values."""
    withheld = False

    def run(self, expr, inputs, variables=None):
        value = super().run(expr, inputs, variables)
        if isinstance(expr,dict) and expr.get('op') in {'input','get','xml_text'} and isinstance(value,str):
            from arch4.privacy_scrub import SCRUB_MASK
            # Only the selected XML text is tainted, never masked siblings in
            # the whole input document returned by an input/get expression.
            if value == SCRUB_MASK or expr.get('op') == 'xml_text' and SCRUB_MASK in value:
                self.withheld = True
        return value


def decode(data,replies,context=None,*,addresses=None,issues=None,admit_outputs=False,withheld_inputs=None,program=None):
    inputs={'profile':data['metadata'],'replies':deepcopy(replies),'context':context or {}}
    evaluator=(_WithheldInputEvaluator if withheld_inputs is not None else Evaluator)(
        data['functions'],algorithms=data['algorithms'],max_steps=2000000,program=program)
    prepared={step['output']:step for step in data.get('preprocessing',[])}
    # Derived aliases can only be produced by their declared native input path.
    for name in prepared:inputs['replies'].pop(name,None)
    complete=set();active=set()
    def prepare(names):
        for name in names:
            if name not in prepared or name in complete:continue
            if name in active:raise RuleError('recursive_preprocessing:'+name)
            active.add(name)
            try:
                step=prepared[name]
                required=frozenset(step.get('requires',[]))|reply_dependencies(step['expression'],data['metadata'],data['functions'],context=inputs['context'])
                prepare(required)
                if not required.issubset(inputs['replies']):raise RuleError('native_preprocessing_reply_missing')
                inputs['replies'][name]=evaluator.run(step['expression'],inputs)
                complete.add(name)
            finally:active.remove(name)
    result={}
    for slot in data['fields']:
        address=slot['definition']['address']
        if slot['read'] is None or addresses is not None and address not in addresses:continue
        if withheld_inputs is not None:evaluator.withheld=False
        try:
            prepare(reply_dependencies(slot['read']['expression'],data['metadata'],data['functions'],context=inputs['context']))
            value=evaluator.run(slot['read']['expression'],inputs)
            if admit_outputs:
                from .results import admit
                value=admit(value,slot['definition']['value_type'])
            result[address]=value
        except (ValueError,TypeError,KeyError,IndexError,OverflowError,ZeroDivisionError):
            if issues is None:raise
            from .results import FieldIssue
            issues.append(FieldIssue(address,'/fields/'+address+'/read','field_conversion_unavailable'))
            result[address]=None
        finally:
            if withheld_inputs is not None and evaluator.withheld:withheld_inputs.add(address)
    return result
