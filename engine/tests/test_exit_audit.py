"""Independent causal/revision regressions for the exit research audit."""
import pandas as pd
import pytest

from test_exit_policy_core import fixture
from test_exit_experiment_publish import make, previous
from watchlist.exit_policy_experiments import entry_session, prepare, simulate
from watchlist.exit_experiment_publish import build


def test_publication_at_open_cannot_capture_that_open():
    signal, _, _ = fixture()
    signal['published_at'] = '2026-01-05T14:30:00Z'
    assert entry_session(signal) == pd.Timestamp('2026-01-06')


def test_added_earlier_history_is_an_input_revision_not_silent_atr_reseed():
    signal, history, cutoff = fixture(pre=20)
    first = make(signals=[signal], prices=history)
    _, expanded, _ = fixture(pre=40)
    expanded.loc[expanded.index < history.index[0], ['High', 'Low']] = [130., 70.]
    old = prepare(signal, history, cutoff)
    new = prepare(signal, expanded, cutoff)
    prior = history.index[19]
    assert old.indicators['atr14'][prior] != new.indicators['atr14'][prior]
    updated = make(signals=[signal], prices=expanded,
                   previous=previous(first), registry=first['registry'])
    assert all(r.get('exclusion_reason') == 'PRICE_REVISION_REQUIRES_REVIEW'
               for r in updated['results'])


def test_earlier_cutoff_cannot_reuse_later_certified_results():
    signal, _, _ = fixture()
    first = make()
    with pytest.raises(ValueError, match='EVALUATION_CUTOFF_REGRESSION'):
        build([signal], lambda *_: pd.DataFrame(), core={'run_id': 'earlier',
              'generated_at': signal['generated_at']}, source_commit='a'*40,
              source_signals_sha256='b'*64, cutoff='2026-01-06T22:00:00Z',
              created_at='2026-11-13T00:00:00Z', cost_pct=.3,
              registry=first['registry'], previous=previous(first))


def test_ma_warmup_at_first_close_is_valid_without_preopen_ma():
    signal, history, cutoff = fixture(pre=49)
    result = simulate(prepare(signal, history, cutoff), 'MA50_2C', 20)
    # MA is evaluated only at the close: 49 prior bars plus today's closed
    # bar are enough. Do not impose an unnecessary extra warm-up session.
    assert result['eligibility_status'] == 'eligible'


def test_atr_spike_stop_above_close_fills_next_open_not_impossible_stop():
    signal, history, cutoff = fixture()
    entry = pd.Timestamp('2026-01-05')
    history.loc[entry, ['High', 'Low', 'Close']] = [130., 99., 101.]
    result = simulate(prepare(signal, history, cutoff), 'ATR_TRAIL', 20)
    # Prior TRs are 4. Entry TR is 31, so Wilder ATR=(13*4+31)/14.
    expected_stop = 130. - 3. * 83. / 14.
    assert result['stop_path'][0]['active_stop'] == 90.
    assert result['stop_path'][1]['active_stop'] == pytest.approx(expected_stop)
    assert expected_stop > 101.
    assert result['holding_sessions'] == 2
    assert result['exit_reason'] == 'gap_atr_stop'
    assert result['exit_price'] == 100.
    assert result['net_R'] == pytest.approx(-.03)


def test_v2_publication_preserves_v1_files(tmp_path):
    from watchlist.exit_experiment_publish import publish
    from watchlist.exit_policy_experiments import canonical
    old = tmp_path/'exit_experiments/exit-research-v1/registry.json'
    old.parent.mkdir(parents=True)
    old.write_bytes(b'archived v1 bytes')
    (tmp_path/'latest').mkdir()
    (tmp_path/'latest/manifest.json').write_bytes(canonical({
        'run_id': 'source-current', 'generated_at': '2026-01-02T22:00:00Z'}))
    publish(tmp_path, make())
    assert old.read_bytes() == b'archived v1 bytes'
    assert (tmp_path/'exit_experiments/exit-research-v2/latest.json').exists()

