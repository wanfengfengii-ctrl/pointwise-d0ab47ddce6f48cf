from dataclasses import dataclass
from warehouse.execution.environments import Environment
from warehouse.execution.expand import expand_scope
from warehouse.expressions.semantics import evaluate_expression, require_boolean
from warehouse.expressions.errors import EvaluationError, ErrorLocation, attach_first_location


@dataclass(frozen=True)
class OutputRow:
    output_index: int
    coordinate: object
    values: dict


class ExecutionRunner:
    def execute(self, sources, plan, context, append):
        index = 0
        for source, record in sources:
            environment = Environment.root(plan.root, source, record)
            for coordinate, values in self.execute_scope(environment, context, ()):
                append(OutputRow(index, coordinate, values))
                index += 1
        return index

    def execute_scope(self, environment, context, ancestors):
        scope = environment.scope
        if scope.where is not None:
            try:
                selected = require_boolean(evaluate_expression(scope.where, environment, context))
            except EvaluationError as error:
                raise attach_first_location(error, ErrorLocation(
                    scope.where.location, environment.coordinate))
            if not selected:
                return
        lineage = ancestors + (environment,)
        if scope.expand is not None:
            for child in expand_scope(scope.expand, environment, context):
                yield from self.execute_scope(child, context, lineage)
        else:
            yield environment.coordinate, self.project_outputs(lineage, context)

    def project_outputs(self, lineage, context):
        result = {}
        demand = lineage[-1].coordinate
        for environment in lineage:
            for output in environment.scope.output:
                result[output.name] = evaluate_expression(
                    output.expr, environment, context, demand)
        return result
