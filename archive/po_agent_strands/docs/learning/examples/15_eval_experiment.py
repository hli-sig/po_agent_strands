"""Lesson 15 — an eval with strands-agents-evals, offline (scripted model, no judge).

strands-agents-evals is NOT a project dependency (you add it in lesson 15), so
run this with an overlay:

    uv run --with strands-agents-evals==1.4.0 docs/learning/examples/15_eval_experiment.py

Cases -> a task that returns a TaskOutput-shaped dict -> evaluators -> a report.
The custom evaluator checks a PROPERTY (the route), not exact wording.
"""

from _common import banner, model

from strands import Agent, tool
from strands_evals import Case, Experiment
from strands_evals.evaluators import Evaluator, ToolCalled
from strands_evals.types import EvaluationData, EvaluationOutput
from tests.fakes import call, text

banner("15 eval experiment")


@tool
def get_sprint_status_by_number(sprint_number: int) -> str:
    """Sprint status by visible number."""
    return "Sprint 31: 62% done"


def task(case: Case) -> dict:
    """Run the agent once; return what evaluators need, in the TaskOutput shape."""
    agent = Agent(model=model([[call("get_sprint_status_by_number", sprint_number=31)],
                               [text("Sprint 31 is 62% done.")]]),
                  tools=[get_sprint_status_by_number], callback_handler=None)
    reply = str(agent(case.input)).strip()
    requested = [b["toolUse"]["name"] for m in agent.messages for b in m["content"] if "toolUse" in b]
    return {"output": {"reply": reply, "route": "sprint"}, "trajectory": requested}


class RouteIs(Evaluator):
    """A deterministic property check: the task chose the expected route."""

    def evaluate(self, data: EvaluationData) -> list[EvaluationOutput]:
        want = (data.metadata or {}).get("route")
        got = (data.actual_output or {}).get("route")
        return [EvaluationOutput(score=float(got == want), test_pass=got == want,
                                 reason=f"route {got!r}, expected {want!r}")]


cases = [Case(name="sprint status", input="How is sprint 31?", metadata={"route": "sprint"})]
report = Experiment(cases=cases, evaluators=[RouteIs(), ToolCalled("get_sprint_status_by_number")]) \
    .run_evaluations(task)
# One report for the whole experiment: a row per (evaluator, case) with whether it
# passed and why, and one overall score.
for case, passed, reason in zip(report.cases, report.test_passes, report.reasons):
    print(f"{case['evaluator']:<12} {case['name']:<16} pass={passed}  {reason}")
print(f"{'':<12} overall score {report.overall_score}")
