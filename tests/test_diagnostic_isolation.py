"""Regression guard (owner-mandated): diagnostic-only agents (daily_exam,
inner_critic, dream, etc.) must NEVER gate promotion or veto trades. These tests
fail loudly if that wiring is reintroduced."""
import inspect

import promotion_board as pb
import scalp_autotrader as sa


def test_daily_exam_not_a_hard_promotion_gate():
    # daily_exam is an AI self-exam (echo chamber) -> must not be a hard gate.
    assert "daily_exam_avg" not in pb.REQUIREMENTS
    src = inspect.getsource(pb)
    assert "daily_exam_below_threshold" not in src, "daily_exam must not append a promotion failure"


def test_inner_critic_is_advisory_only_in_open_paper():
    # open_paper must NOT early-return on an inner_critic 'block' verdict.
    src = inspect.getsource(sa.ScalpAutoTrader.open_paper)
    # the advisory log must exist and there must be no veto-return tied to the block
    assert "inner_critic_advisory_block" in src
    assert "inner_critic_block" not in src.replace("inner_critic_advisory_block", "")


def test_promotion_still_has_the_legit_gates():
    # ensure we only removed the diagnostic gate, not the real evidence gates
    for req in ("paper_trades", "shadow_closes", "lifecycle_completeness", "trial_days"):
        assert req in pb.REQUIREMENTS
