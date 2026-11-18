from dataclasses import dataclass
from warehouse.expressions.functions import FunctionContext
from warehouse.results.publication import ResultUnavailable as _ResultUnavailable

ResultUnavailable = _ResultUnavailable


@dataclass(frozen=True)
class RunReceipt:
    run_id: str
    row_count: int


class ResultService:
    def __init__(self, database, source, plans, runs, staging, publication,
                 runner, isolated_runner):
        self.db, self.source, self.plans = database, source, plans
        self.runs, self.staging = runs, staging
        self.publication, self.runner = publication, runner
        self.isolated_runner = isolated_runner

    def _context(self, function_context):
        context = FunctionContext() if function_context is None else function_context
        if not isinstance(context, FunctionContext):
            raise TypeError('function_context must be FunctionContext')
        return context

    def _fail(self, run_id, error):
        try:
            self.runs.mark_failed(run_id, error)
        except Exception as storage_error:
            from warehouse.results.database import StorageFailure
            failure = StorageFailure(f'could not persist failed run {run_id}')
            failure.run_id = run_id
            raise failure from storage_error
        error.run_id = run_id
        raise error

    def run(self, batch_id, plan_revision, function_context=None):
        sources = self.source.read_batch(batch_id)
        revision = self.plans.load_revision(plan_revision)
        context = self._context(function_context)
        run_id = self.runs.create(batch_id, plan_revision)
        try:
            with self.db.transaction('run.before_commit'):
                self.runs.start(run_id)
                count = self.runner.execute(
                    sources, revision.compiled, context,
                    lambda row: self.staging.append(run_id, row))
                self.publication.publish(run_id, count)
        except Exception as error:
            self._fail(run_id, error)
        return RunReceipt(run_id, count)

    def run_isolated(self, batch_id, plan_revision, function_context=None):
        sources = self.source.read_batch(batch_id)
        revision = self.plans.load_revision(plan_revision)
        context = self._context(function_context)
        run_id = self.runs.create(batch_id, plan_revision, mode='isolated')
        try:
            with self.db.transaction('run.before_commit'):
                self.runs.start(run_id)
                rows, rejections = self.isolated_runner.execute(
                    sources, revision.compiled, context)
                self.publication.publish_isolated(run_id, rows, rejections)
        except Exception as error:
            self._fail(run_id, error)
        return RunReceipt(run_id, len(rows))

    def read_result(self, run_id):
        return self.publication.read_success(run_id)

    def read_isolated_result(self, run_id):
        return self.publication.read_isolated(run_id)
