import argparse
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from samples import binding, call, expand, field, literal, operation, ref
from warehouse import FunctionContext, Warehouse
from warehouse.expressions.errors import EvaluationError
from warehouse.results.api import ResultUnavailable
from warehouse.results.database import StorageFailure
from warehouse.results.runs import RunState
from warehouse.source.coordinates import encode_coordinate


CHILD = "$.expand.scope"
ROOT_ERROR = "$.bindings[0].expr"
LOCAL_ERROR = CHILD + ".bindings[0].expr"
CODES = {"FunctionFailure": "FUNCTION_FAILURE", "MissingField": "MISSING_FIELD",
         "TypeMismatch": "TYPE_MISMATCH"}


@dataclass
class Example:
    records: list
    plan: dict
    rows: tuple
    rejections: tuple
    calls: tuple


def ticket(position):
    return "next_ticket", (), position


def late_failure(root_owner):
    error = call("fail", literal("late calibration"))
    child = {
        "bindings": [binding("local", ref("q") if root_owner else error)],
        "output": [binding("ticket", call("next_ticket")), binding("checked",
                   operation("if", field("item"), ref("local"), literal("ok")))],
    }
    plan = {"expand": expand(field("root", "items"), child)}
    if root_owner:
        plan["bindings"] = [binding("q", error)]
    records = [{"items": [False, True, False]}, {"items": [False]}]
    owner = (0,) if root_owner else (0, 1)
    location = ROOT_ERROR if root_owner else LOCAL_ERROR
    rejection = (owner, (0, 1), location, "FunctionFailure", "late calibration")
    calls = (ticket((0, 0)), ticket((0, 1)), ("fail", ("late calibration",), owner))
    if root_owner:
        return Example(records, plan, (((1, 0), 3, "ok"),), (rejection,),
                       calls + (ticket((1, 0)),))
    return Example(records, plan,
                   (((0, 0), 1, "ok"), ((0, 2), 3, "ok"), ((1, 0), 4, "ok")),
                   (rejection,), calls + (ticket((0, 2)), ticket((1, 0))))


def nested_failure():
    local = operation("eq", field("leaf"), literal("local"))
    parent = operation("eq", field("leaf"), literal("parent"))
    checked = operation("if", local, field("root", "reading"),
                        operation("if", parent, ref("q"), literal("ok")))
    leaf = {"output": [binding("ticket", call("next_ticket")), binding("checked", checked)]}
    section = {"bindings": [binding("q", call("fail", literal("bad section")))],
               "expand": expand(field("item"), leaf, alias="leaf")}
    plan = {"expand": expand(field("root", "items"), section)}
    return Example(
        [{"items": [["ok", "local", "parent", "skip"], ["ok"]]}], plan,
        (((0, 1, 0), 4, "ok"),),
        (((0, 0, 1), (0, 0, 1), CHILD + ".expand.scope.output[1].expr.args[1]",
          "MissingField", "missing field: reading"),
         ((0, 0), (0, 0, 2), LOCAL_ERROR, "FunctionFailure", "bad section")),
        (ticket((0, 0, 0)), ticket((0, 0, 1)), ticket((0, 0, 2)),
         ("fail", ("bad section",), (0, 0)), ticket((0, 1, 0))),
    )


def boundary_failures():
    leaf = {"output": [binding("ticket", call("next_ticket")),
                       binding("checked", field("item", "reading"))]}
    plan = {"where": field("root", "ok"),
            "expand": expand(field("root", "items"), leaf, mode="left")}
    return Example(
        [{"ok": 1}, {"ok": True, "items": None}, {"ok": True, "items": []},
         {"ok": True, "items": [None]}, {"ok": False}], plan,
        (((2, None), 1, None),),
        (((0,), (0,), "$.where", "TypeMismatch", "expected boolean"),
         ((1,), (1,), "$.expand.expr", "TypeMismatch", "expand requires array"),
         ((3, 0), (3, 0), CHILD + ".output[1].expr", "TypeMismatch",
          "field key requires object")),
        (ticket((2, None)), ticket((3, 0))),
    )


def coordinate(batch_id, position):
    return {"batch_id": batch_id, "record_index": position[0],
            "elements": [{"scope_id": f"s{scope}", "element_index": index}
                         for scope, index in enumerate(position[1:], 1)]}


def expected_errors(batch_id, rejections):
    return [{"owner": coordinate(batch_id, owner),
             "error": {"type": kind, "code": CODES[kind], "message": message,
                       "location": {"expression": expression,
                                    "coordinate": coordinate(batch_id, demand)}}}
            for owner, demand, expression, kind, message in rejections]


def expected_trace(batch_id, calls):
    return [{"function": name, "args": list(args),
             "coordinate": coordinate(batch_id, position)} for name, args, position in calls]


def load_example(warehouse, name, example):
    batch = warehouse.ingest(name, example.records)
    plan = warehouse.register_plan(name, example.plan)
    return batch.batch_id, plan.plan_revision


def expect_exception(kind, operation, *args):
    try:
        operation(*args)
    except kind as error:
        return error
    raise AssertionError(f"expected {kind.__name__} from {operation.__name__}")


def view(result):
    return {"run_id": result.run_id, "batch_id": result.batch_id,
            "plan_revision": result.plan_revision, "row_count": result.row_count,
            "rows": [{"output_index": row.output_index,
                      "coordinate": encode_coordinate(row.coordinate), "values": row.values}
                     for row in result.rows], "rejections": list(result.rejections)}


def check_isolated(warehouse, batch_id, revision, example):
    context = FunctionContext()
    receipt = warehouse.run_isolated(batch_id, revision, context)
    saved = view(warehouse.read_isolated_result(receipt.run_id))
    rows = [{"output_index": i, "coordinate": coordinate(batch_id, position),
             "values": {"ticket": number, "checked": value}}
            for i, (position, number, value) in enumerate(example.rows)]
    assert saved == {"run_id": receipt.run_id, "batch_id": batch_id,
                     "plan_revision": revision, "row_count": len(rows), "rows": rows,
                     "rejections": expected_errors(batch_id, example.rejections)}, saved
    assert receipt.row_count == len(rows)
    assert context.trace == expected_trace(batch_id, example.calls), context.trace
    assert warehouse.runs.get(receipt.run_id).state is RunState.SUCCEEDED
    expect_exception(ResultUnavailable, warehouse.read_result, receipt.run_id)
    return saved, context.trace


def baseline():
    from tests.oracle import interpret

    with Warehouse() as warehouse:
        for name, example in (("root", late_failure(True)), ("element", late_failure(False)),
                              ("nested", nested_failure()), ("boundary", boundary_failures())):
            batch, revision = load_example(warehouse, name, example)
            context = FunctionContext()
            error = expect_exception(EvaluationError, warehouse.run, batch, revision, context)
            strict = interpret(example.records, example.plan, batch)
            expected = expected_errors(batch, example.rejections)[0]["error"]
            assert error.to_data() == strict["error"] == expected, error.to_data()
            assert context.trace == strict["trace"], context.trace
            assert warehouse.runs.get(error.run_id).state is RunState.FAILED
            expect_exception(ResultUnavailable, warehouse.read_result, error.run_id)
            print(json.dumps({"example": name, "strict_error": error.to_data(),
                              "trace": context.trace}, ensure_ascii=False))


def verify():
    suite = unittest.defaultTestLoader.discover("tests", top_level_dir=".")
    if not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():
        return 1
    root = late_failure(True)
    empty = Example([], root.plan, (), (), ())
    all_bad = Example([{"items": [True]}], root.plan, (),
                      (((0,), (0, 0), ROOT_ERROR, "FunctionFailure", "late calibration"),),
                      (ticket((0, 0)), ("fail", ("late calibration",), (0,))))
    with tempfile.TemporaryDirectory(prefix="warehouse-isolation-") as directory:
        path = Path(directory) / "warehouse.sqlite"
        saved = []
        with Warehouse(path) as warehouse:
            batch, revision = load_example(warehouse, "root", root)
            normal_plan = warehouse.register_plan("normal", {"output": [
                binding("v", literal(1))]}).plan_revision
            strict_id = warehouse.run(batch, normal_plan).run_id
            strict_result = warehouse.read_result(strict_id)
            for name, example in (("root", root), ("element", late_failure(False)),
                                  ("nested", nested_failure()), ("boundary", boundary_failures()),
                                  ("empty", empty), ("all-bad", all_bad)):
                identity = (batch, revision) if name == "root" else load_example(
                    warehouse, name, example)
                result, _ = check_isolated(warehouse, *identity, example)
                saved.append(result)
                warehouse.register_plan(name, {})
            context = FunctionContext()
            warehouse.database.inject_once("run.before_commit")
            failure = expect_exception(StorageFailure, warehouse.run_isolated,
                                       batch, revision, context)
            assert context.trace == expected_trace(batch, root.calls)
            assert warehouse.runs.get(failure.run_id).state is RunState.FAILED
        with Warehouse(path) as reopened:
            assert reopened.read_result(strict_id) == strict_result
            expect_exception(ResultUnavailable, reopened.read_isolated_result, strict_id)
            for result in saved:
                assert view(reopened.read_isolated_result(result["run_id"])) == result
            for unreadable in (failure.run_id, "missing"):
                for read in (reopened.read_result, reopened.read_isolated_result):
                    expect_exception(ResultUnavailable, read, unreadable)
    print("Verified isolated details, rejections and SQLite publication.")
    return 0


def demo():
    with tempfile.TemporaryDirectory(prefix="warehouse-isolation-") as directory:
        path = Path(directory) / "warehouse.sqlite"
        results = []
        with Warehouse(path) as warehouse:
            for name, root_owner in (("root", True), ("element", False)):
                example = late_failure(root_owner)
                identity = load_example(warehouse, name, example)
                result, trace = check_isolated(warehouse, *identity, example)
                results.append((name, result["run_id"], trace))
        with Warehouse(path) as reopened:
            for name, run_id, trace in results:
                print(json.dumps({"example": name, "result": view(
                    reopened.read_isolated_result(run_id)), "trace": trace}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("baseline", "verify", "demo"))
    mode = parser.parse_args().mode
    if mode == "baseline":
        baseline()
    elif mode == "verify":
        return verify()
    else:
        demo()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
