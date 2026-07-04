"""Map liveness — cell status DERIVED from run results at read time (not stored)."""
import sm_proxy


def test_derive_cell_status_reads_true_state_cold():
    grid = [{"surface": "V-015", "status": "whitespace"}, {"surface": "untested", "status": "gated"}]
    runs = [
        {"parent": "new-search-surface:V-015", "result": {"gate_pass": False, "t": 1.61, "n": 576,
         "edge_pct_per_day": 0.14, "gate": {"min_abs_t": 2.0, "direction": "positive"}}},
        {"parent": "new-search-surface:V-015", "result": {"gate_pass": False, "t": 0.03, "n": 1704,
         "edge_pct_per_day": 0.0018, "gate": {"min_abs_t": 2.0, "direction": "positive"}}},
        {"kind": "reterminus", "result": {}},                     # ignored
    ]
    sm_proxy._derive_cell_status(grid, runs)
    # stored 'whitespace' overridden by the DERIVED state (one flow inconclusive → OPEN)
    assert grid[0]["status"] == "tested-inconclusive"
    # a surface with no runs keeps its generator status
    assert grid[1]["status"] == "gated"


def test_surface_of():
    assert sm_proxy._surface_of("new-search-surface:V-015#V-015-TDF") == "V-015"
    assert sm_proxy._surface_of("new-search-surface:V-015") == "V-015"
