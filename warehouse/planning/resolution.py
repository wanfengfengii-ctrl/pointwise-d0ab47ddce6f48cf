from dataclasses import dataclass, replace
from warehouse.expressions.model import BindingReference
from warehouse.expressions.validation import collect_binding_references
from warehouse.planning.model import PlanValidationError, ScopeDefinition


@dataclass(frozen=True)
class ResolvedBinding:
    binding_id: str
    owner_scope: str
    name: str


@dataclass
class ScopeTable:
    scope_id: str
    bindings: dict
    aliases: frozenset
    parent: object = None
    child: object = None


def build_scope_table(root):
    counter = 0
    def build(scope, parent=None, aliases=frozenset({'root'})):
        nonlocal counter
        scope_id = f's{counter}'
        counter += 1
        table = ScopeTable(scope_id, {
            b.name: ResolvedBinding(f'{scope_id}:{b.name}', scope_id, b.name)
            for b in scope.bindings}, aliases, parent)
        if scope.expand:
            table.child = build(scope.expand.scope, table,
                                aliases | {scope.expand.alias})
        return table
    return build(root)


def resolve_reference(name, table):
    current = table
    while current:
        if name in current.bindings:
            b = current.bindings[name]
            return BindingReference(b.binding_id, b.owner_scope, b.name)
        current = current.parent
    raise PlanValidationError(f'{table.scope_id}: unknown binding {name}')


def validate_binding_graph(root):
    graph = {}
    def visit(scope):
        for binding in scope.bindings:
            graph[binding.binding_id] = [
                ref.binding_id for ref in collect_binding_references(binding.expr)]
        if scope.expand:
            visit(scope.expand.scope)
    visit(root)
    done, active = set(), set()
    def check(key):
        if key in active:
            raise PlanValidationError(f'binding dependency cycle at {key}')
        if key in done:
            return
        active.add(key)
        for target in graph[key]:
            check(target)
        active.remove(key)
        done.add(key)
    for key in graph:
        check(key)


def resolve_plan(plan):
    tables = build_scope_table(plan.root)
    def expression(expr, table):
        value = expr.value
        if expr.op == 'ref':
            value = resolve_reference(value, table)
        if expr.op == 'field' and value.base not in table.aliases:
            raise PlanValidationError(f'{expr.location}: unknown field base {value.base}')
        return replace(expr, value=value,
                       args=tuple(expression(arg, table) for arg in expr.args))
    def scope(node, table):
        bindings = tuple(replace(
            b, expr=expression(b.expr, table),
            binding_id=table.bindings[b.name].binding_id,
            owner_scope=table.scope_id) for b in node.bindings)
        outputs = tuple(replace(o, expr=expression(o.expr, table)) for o in node.output)
        expand = None
        if node.expand:
            expand = replace(node.expand, expr=expression(node.expand.expr, table),
                             scope=scope(node.expand.scope, table.child))
        return ScopeDefinition(bindings,
                               expression(node.where, table) if node.where else None,
                               expand, outputs, table.scope_id)
    resolved = scope(plan.root, tables)
    validate_binding_graph(resolved)
    return resolved
