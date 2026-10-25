"""Experience extraction: first trajectory alone (warms the prompt cache), rest in parallel."""
import asyncio
from types import SimpleNamespace

from openviking.session.train import ExperienceGradientContext, ExperienceGradientEstimator


class _Estimator(ExperienceGradientEstimator):
    def __init__(self):
        super().__init__()
        self.events = []

    async def _run_extract_loop(self, trajectory, context):
        self.events.append(("start", trajectory.uri))
        await asyncio.sleep(0.01)
        self.events.append(("end", trajectory.uri))
        return None


def _run(uris):
    est = _Estimator()
    analysis = SimpleNamespace(trajectories=[SimpleNamespace(uri=u) for u in uris], metadata={})
    ctx = ExperienceGradientContext(request_context=object(), messages=[])
    assert asyncio.run(est.estimate(analysis, None, ctx)) == []
    return est.events


def test_first_call_finishes_before_the_rest_start_together():
    assert _run(["a", "b", "c"]) == [
        ("start", "a"), ("end", "a"),
        ("start", "b"), ("start", "c"), ("end", "b"), ("end", "c"),
    ]


def test_no_trajectories_and_single():
    assert _run([]) == []
    assert _run(["a"]) == [("start", "a"), ("end", "a")]
