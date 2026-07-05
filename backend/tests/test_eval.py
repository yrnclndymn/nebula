"""Unit tests for the deterministic eval checks (no network / no model)."""

from evals.run_eval import check_expectations, trajectory_checks


def test_check_expectations_all_pass():
    saved = {
        "year_founded": 2021,
        "hq_location": "San Francisco, California, USA",
        "leadership": ["Dario Amodei | CEO", "Daniela Amodei | President"],
        "about": "AI safety company",
    }
    expected = {
        "year_founded": 2021,
        "hq_contains": "San Francisco",
        "leader_contains": "Amodei",
        "non_empty": ["about", "hq_location"],
    }
    assert all(ok for _, ok, _ in check_expectations(saved, expected))


def test_check_expectations_catches_misses():
    saved = {"year_founded": 1999, "hq_location": "Berlin"}  # wrong year, wrong hq, no about
    expected = {
        "year_founded": 2021,
        "hq_contains": "San Francisco",
        "non_empty": ["about"],
    }
    assert not any(ok for _, ok, _ in check_expectations(saved, expected))


def test_check_expectations_year_tolerance():
    # ±1 is allowed
    assert check_expectations({"year_founded": 2020}, {"year_founded": 2021})[0][1] is True


def test_trajectory_checks():
    good = {
        label: ok
        for label, ok, _ in trajectory_checks(["fetch_page", "web_search", "save_company"])
    }
    assert all(good.values())
    twice = {label: ok for label, ok, _ in trajectory_checks(["save_company", "save_company"])}
    assert twice["saved exactly once"] is False
