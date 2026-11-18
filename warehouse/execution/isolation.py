from dataclasses import dataclass
from warehouse.execution.environments import Environment
from warehouse.execution.expand import expand_scope
from warehouse.execution.runner import OutputRow
from warehouse.expressions.semantics import evaluate_expression, require_boolean
from warehouse.expressions.errors import (
    EvaluationError, ErrorLocation, FunctionFailure, MissingField, TypeMismatch,
    attach_first_location)

QUALITY_ERRORS = (FunctionFailure, MissingField, TypeMismatch)


@dataclass(frozen=True)
class IsolatedRejection:
    owner: object
    error: object


class IsolationAbort(Exception):
    """Carries one quality failure outward to its owning occurrence frame."""

    def __init__(self, owner, error):
        super().__init__(str(error))
        self.owner, self.error = owner, error


def scope_base_path(environment):
    current, depth = environment, 0
    while current.parent is not None:
        current, depth = current.parent, depth + 1
    return '$' + '.expand.scope' * depth


class IsolatedRunner:
    """Depth-first execution that quarantines quality failures per occurrence.

    Uses the same environments, demand cells and evaluator as the strict
    runner, so laziness, traversal order, short circuiting and function
    effects are identical. Candidate rows are buffered in memory; after the
    traversal, rows within any rejection owner's scope are withdrawn and the
    survivors are renumbered contiguously. Evaluation is never rerun.
    """

    def __init__(self):
        self.rows, self.rejections, self._seen = [], [], set()

    def execute(self, sources, plan, context):
        self.rows, self.rejections, self._seen = [], [], set()
        for source, record in sources:
            environment = Environment.root(plan.root, source, record)
            for coordinate, values in self.execute_scope(environment, context, ()):
                self.rows.append((coordinate, values))
        owners = [rejection.owner for rejection in self.rejections]
        survivors = [(coordinate, values) for coordinate, values in self.rows
                     if not any(self._within(coordinate, owner) for owner in owners)]
        return ([OutputRow(index, coordinate, values)
                 for index, (coordinate, values) in enumerate(survivors)],
                list(self.rejections))

    @staticmethod
    def _within(coordinate, owner):
        return (coordinate.source == owner.source and
                coordinate.elements[:len(owner.elements)] == owner.elements)

    def execute_scope(self, environment, context, ancestors):
        scope = environment.scope
        lineage = ancestors + (environment,)
        try:
            if scope.where is not None:
                try:
                    selected = require_boolean(
                        evaluate_expression(scope.where, environment, context))
                except EvaluationError as error:
                    raise attach_first_location(error, ErrorLocation(
                        scope.where.location, environment.coordinate))
                if not selected:
                    return
            if scope.expand is not None:
                for child in expand_scope(scope.expand, environment, context):
                    yield from self.execute_scope(child, context, lineage)
            else:
                yield environment.coordinate, self.project_outputs(lineage, context)
        except IsolationAbort as abort:
            if abort.owner is not environment:
                raise
            self.record(abort.owner, abort.error)
        except EvaluationError as error:
            if not isinstance(error, QUALITY_ERRORS):
                raise
            owner = self.find_owner(error, lineage)
            if owner is not environment:
                raise IsolationAbort(owner, error) from error
            self.record(owner, error)

    def project_outputs(self, lineage, context):
        result = {}
        demand = lineage[-1].coordinate
        for environment in lineage:
            for output in environment.scope.output:
                result[output.name] = evaluate_expression(
                    output.expr, environment, context, demand)
        return result

    @staticmethod
    def find_owner(error, lineage):
        path = error.location.expression if error.location is not None else None
        owner = lineage[-1]
        if path is not None:
            for environment in lineage:
                base = scope_base_path(environment)
                if path == base or path.startswith(base + '.'):
                    owner = environment
        return owner

    def record(self, owner, error):
        if id(error) in self._seen:
            return
        self._seen.add(id(error))
        self.rejections.append(IsolatedRejection(owner.coordinate, error))
