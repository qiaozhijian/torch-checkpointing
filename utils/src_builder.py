import torch
import torch.jit
from .graph_node import Node
from .utils import to_pyid, validate_indice


def make_function(func_name, params, body):
    result = \
'''def {}(self, {}):
        {}
'''
    return result.format(func_name, ", ".join(params), ("\n" + 8 * " ").join(body))

def make_torch_checkpoint_call(func_name, params):
    return f'torch.utils.checkpoint.checkpoint({func_name}, args=({", ".join(params)}))'

@validate_indice
def local_variables(start: int, end: int, parsed_code: list) -> set:
    return set((code.output_var for code in (parsed_code[i] for i in range(start, end + 1))))

@validate_indice
def free_variables(start: int, end: int, parsed_code: list, local_vars=None) -> set:
    '''
        Collect free varibles if we lift codes in [start, end] are lifted to
        a lambda.

        :params:
            start         the start index of lifting
            end           the end index of lifting
            parsed_code   ParsedCode objects that are sorted in
                          topological order with respect to the original node
        
        :returns:
            a set of free varible names
    '''
    if local_vars:
        local_ref = local_vars
    else:
        local_ref = local_variables(start, end, parsed_code)
    free_variables  = set()
    for i in range(start, end + 1):
        for param in parsed_code[i].args:
            if isinstance(param, str) and param not in local_ref:
                free_variables.add(param)
    return free_variables

def checkpointing(parsed_code: list, checkpoints: list, output_var: str) -> str:
    '''
        Compile a checkpointed model forward pass code

        :params:
            parsed_code     Function calls and parameters in each line in SSA form
            checkpoints     Node ids that are marked as checkpoints
            output_var       The variable that stores the output result of the graph
        
        :returns:
            An executable python code that represents the checkpointed model
    '''
    def func_name_generator():
        cnt = 0
        while True:
            yield f'jojo_{cnt}'
            cnt += 1
    name_iter = func_name_generator()

    local_code = []
    declared_code = []
    cons = lambda elem, xs: [ elem ] + xs
    tail = len(parsed_code) - 1
    # Process the trailing segment (the last checkpoint to the end of the graph)
    head = tail
    while head >= 0 and parsed_code[head].node_id not in checkpoints:
        local_code = cons(parsed_code[head].code, local_code)
        head -= 1
    # Get the local refs of trailing segment
    if head >= 0:
        local_refs = local_variables(head, tail, parsed_code)
    else:
        return {
            'class_declared': [],
            'forward_local' : [x.code for x in parsed_code]
        }
    tail = head
    while tail >= 0:
        # Ensures: tail points either 0, -1 or a checkpoint
        local_code = cons(parsed_code[tail].code, local_code)
        local_refs  = local_refs.union(set(filter(lambda x: isinstance(x, str), parsed_code[tail].args)))
        head = tail - 1
        while head >= 0 and parsed_code[head].node_id not in checkpoints:
            head -= 1
        # The adjacent precedence of the last checkpoint is not a checkpoint
        if head != tail - 1:
            func_name = next(name_iter)
            body = []  # the lifted lambda body
            referred = set()             # variables got referred later in the context (should not be lifted)
            for i in range(head + 1, tail):
                body.append(parsed_code[i].code)
                # if the output later is referred
                if parsed_code[i].output_var in local_refs:
                    referred.add(parsed_code[i].output_var)
            args_after_lift = free_variables(head + 1, tail - 1, parsed_code)
            body.append(f'return {", ".join(referred)}')  # return the variables that are referred later
            declared_code.append(make_function(func_name, args_after_lift, body))
            local_code = cons(f'{", ".join(referred)} = self.{make_torch_checkpoint_call(func_name, args_after_lift)}', local_code)
            local_refs = local_refs.union(args_after_lift)
        tail = head
    local_code.append(f'return {output_var}')
    return {
        'forward_local':  local_code,
        'class_declared': declared_code
    }


def build_src(name: str, forward_args: str, class_defined: list, forward_pass: list):
    foward_template = '''def forward({}):
        {}
    '''
    return '''class {}(torch.nn.Module):
    {}
    {}
    '''.format(name, "\n    ".join(map(lambda x: f'{x}', class_defined)),\
                               foward_template.format(forward_args, ("\n" + 8 * " ").join(forward_pass)))

def to_python_src(module_name: str, params: Node, start: Node, graph: dict, checkpoints: list):
    '''
        Compile the computation graph to Python source code

        :params:
            params: parameters (input) of the graph
            start : the entry node of the graph
            graph : a string->Node map represents the nodes in the graph
    '''
    env = dict(((k, v) for k, v in zip(params.outputs, map(to_pyid, params.outputs))))
    lines = []
    nodes = list(graph.values())
    nodes.sort(key=lambda node: node.outputs[0])
    for n in nodes:
        new_line = n.to_python(env, src=True, inline=True)
        if new_line:
            lines.append(new_line)
    result_checkpoint = checkpointing(lines, checkpoints, lines[-1].output_var)
    return build_src(module_name, ", ".join((to_pyid(x) for x in params.outputs)),\
                     result_checkpoint['class_declared'], result_checkpoint['forward_local'])
