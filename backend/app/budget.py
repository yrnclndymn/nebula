"""Per-run budget caps for agent jobs.

Scheduled / ambient jobs multiply spend: a single tick can fan out to many
companies, each fetching pages and calling the LLM. This module puts a hard cap
on that spend **in the tool layer — where the money is actually spent** — rather
than trusting a prompt to behave. It is deliberately not a rate limiter: it
bounds one *run*.

A `Budget` holds per-run counters and their limits. It is installed on a
`ContextVar` for the duration of a job run (`use_budget`), so the shared helpers
— page fetch (`app.tools.web.fetch_page`), web search (`web_search`), and the LLM
call (`app.genai_retry.generate_with_retry`) — can charge it without threading a
parameter through every call. Each helper calls the matching module-level
`charge_*()`; that reads the ContextVar and, if a budget is installed, increments
the counter and raises `BudgetExhausted` once the cap is reached.

**No budget on the context = unlimited.** The ContextVar defaults to `None`, so
`charge_*()` is a no-op unless a run explicitly installs a budget. Interactive
paths (chat, one-off enrich) never call `use_budget`, so they are completely
unaffected — this is what keeps the guardrail off the human-in-the-loop flows.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

# The kinds of spend a run is charged for. `companies` is charged by the job
# loop; the other three by the shared tool helpers.
LIMITS = ("pages", "searches", "llm_calls", "companies")


class BudgetExhausted(Exception):
    """Raised by a `charge_*` helper when a run reaches one of its caps.

    Carries which limit tripped and how far the run got, so the job runner can
    record a precise "budget exhausted" marker instead of erroring out.
    """

    def __init__(self, limit: str, cap: int, count: int):
        self.limit = limit  # one of LIMITS
        self.cap = cap  # the configured ceiling
        self.count = count  # how many had already been charged (== cap)
        super().__init__(f"budget exhausted: {limit} limit of {cap} reached")


@dataclass
class Budget:
    """Per-run limits + live counters. A `None` limit means that dimension is
    uncapped (still counted, for observability). Charge *before* doing the work:
    `charge_*` raises when the counter has already reached the cap, so the capped
    unit of work is never started."""

    max_pages: int | None = None
    max_searches: int | None = None
    max_llm_calls: int | None = None
    max_companies: int | None = None

    pages: int = 0
    searches: int = 0
    llm_calls: int = 0
    companies: int = 0

    def _charge(self, limit: str, cap: int | None, counter: str) -> None:
        current = getattr(self, counter)
        if cap is not None and current >= cap:
            raise BudgetExhausted(limit, cap, current)
        setattr(self, counter, current + 1)

    def charge_page(self) -> None:
        self._charge("pages", self.max_pages, "pages")

    def charge_search(self) -> None:
        self._charge("searches", self.max_searches, "searches")

    def charge_llm(self) -> None:
        self._charge("llm_calls", self.max_llm_calls, "llm_calls")

    def charge_company(self) -> None:
        self._charge("companies", self.max_companies, "companies")

    def usage(self) -> dict:
        """Counters snapshot, for recording on a job's payload."""
        return {
            "pages": self.pages,
            "searches": self.searches,
            "llm_calls": self.llm_calls,
            "companies": self.companies,
        }


_current: ContextVar[Budget | None] = ContextVar("nebula_budget", default=None)


def current_budget() -> Budget | None:
    """The budget installed for the current run, or None (unlimited)."""
    return _current.get()


@contextmanager
def use_budget(budget: Budget | None):
    """Install `budget` on the ContextVar for the duration of the block. Passing
    `None` explicitly means "unlimited" — the same as never calling this."""
    token = _current.set(budget)
    try:
        yield budget
    finally:
        _current.reset(token)


def charge_page() -> None:
    """Charge one page fetch against the active budget (no-op if none)."""
    budget = _current.get()
    if budget is not None:
        budget.charge_page()


def charge_search() -> None:
    """Charge one web search against the active budget (no-op if none)."""
    budget = _current.get()
    if budget is not None:
        budget.charge_search()


def charge_llm() -> None:
    """Charge one LLM call against the active budget (no-op if none)."""
    budget = _current.get()
    if budget is not None:
        budget.charge_llm()


def charge_company() -> None:
    """Charge one company against the active budget (no-op if none). Called by a
    job loop before it processes the next company."""
    budget = _current.get()
    if budget is not None:
        budget.charge_company()


def budget_for(job_type: str, overrides: dict | None = None) -> Budget | None:
    """Build the `Budget` for a job run: per-job-type defaults from settings,
    with a job's payload `overrides` merged on top. Returns None (unlimited) when
    the job type has no configured defaults and no overrides — so unconfigured /
    interactive work stays uncapped. An override value of `None` uncaps that one
    dimension."""
    from app.config import settings

    defaults = settings.job_budgets.get(job_type)
    if defaults is None and not overrides:
        return None
    merged = {**(defaults or {}), **(overrides or {})}
    return Budget(
        max_pages=merged.get("max_pages"),
        max_searches=merged.get("max_searches"),
        max_llm_calls=merged.get("max_llm_calls"),
        max_companies=merged.get("max_companies"),
    )
