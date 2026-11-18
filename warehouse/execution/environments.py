from warehouse.execution.bindings import BindingCell
from warehouse.source.coordinates import OccurrenceCoordinate, child_coordinate
from warehouse.expressions.errors import BindingCycleError, ErrorLocation


class _EmptySide:
    pass


EMPTY_SIDE = _EmptySide()


class Environment:
    def __init__(self, scope, coordinate, root_record, parent=None,
                 alias=None, element=None):
        self.scope = scope
        self.coordinate = coordinate
        self.root_record = root_record
        self.parent = parent
        self.alias = alias
        self.element = element
        self.bindings = {b.binding_id: b for b in scope.bindings}
        self.cells = {b.binding_id: BindingCell() for b in scope.bindings}

    @classmethod
    def root(cls, scope, source, record):
        return cls(scope, OccurrenceCoordinate(source), record)

    def child(self, scope, alias, element, index):
        return Environment(scope, child_coordinate(self.coordinate, scope.scope_id, index),
                           self.root_record, self, alias, element)

    def owner_for_scope(self, scope_id):
        current = self
        while current:
            if current.scope.scope_id == scope_id:
                return current
            current = current.parent
        raise BindingCycleError(f'no owning environment for {scope_id}')

    def resolve_binding(self, reference, context, demand_coordinate):
        from warehouse.expressions.semantics import evaluate_expression
        owner = self.owner_for_scope(reference.owner_scope)
        if reference.binding_id not in owner.bindings:
            raise BindingCycleError(f'unknown compiled binding {reference.binding_id}')
        binding = owner.bindings[reference.binding_id]
        return owner.cells[reference.binding_id].demand(
            lambda: evaluate_expression(binding.expr, owner, context, demand_coordinate),
            ErrorLocation(binding.expr.location, demand_coordinate))

    def read_base(self, name):
        if name == 'root':
            return self.root_record
        current = self
        while current:
            if current.alias == name:
                return current.element
            current = current.parent
        raise BindingCycleError(f'no field base {name}')
