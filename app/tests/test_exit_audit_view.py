import pytest
from test_exit_experiment_view import doc
from exit_experiment_view import signal_figure


def test_archived_path_cannot_be_plotted_as_a_verified_current_signal():
    data = doc()
    rows = [dict(signal_id=r['signal_id'], rule_id=r['rule_id'], horizon=20,
                 eligibility_status='excluded',
                 exclusion_reason='PRICE_REVISION_REQUIRES_REVIEW')
            for r in data['results'] if r['horizon'] == 20]
    with pytest.raises(ValueError, match='UNVERIFIED_SIGNAL_CHART'):
        signal_figure(next(iter(data['paths'].values())), rows, ['ATR_TRAIL'])


def test_excluded_signal_shows_warning_instead_of_archived_chart():
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_string('''
from test_exit_experiment_view import doc
from exit_experiment_view import render_exit_experiments
data=doc()
for row in data['results']:
    row['eligibility_status']='excluded'
    row['exclusion_reason']='PRICE_REVISION_REQUIRES_REVIEW'
render_exit_experiments(data, {'status':'ready'})
''', default_timeout=30).run()
    app.selectbox(key='exit_vintage').set_value('회고적').run()
    assert not app.exception
    assert any('보관된 과거 차트' in w.value for w in app.warning)


def test_v1_is_not_silently_presented_as_v2():
    from exit_experiment_view import load_exit_publication
    requested=[]
    def read(path):
        requested.append(path)
        return None
    result, state = load_exit_publication('v2-migration', 'current-core', read)
    assert result == {} and state['status'] == 'absent'
    assert requested == ['exit_experiments/exit-research-v2/latest.json',
                         'exit_experiments/exit-research-v2/previous.json']

