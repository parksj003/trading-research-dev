from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from watchlist import trend_watchlist as engine


@pytest.mark.parametrize('moment,expired', [('2026-12-31T23:59:00+09:00',False),
    ('2026-12-31T15:01:00+00:00',True),('2027-01-04T20:00:00+09:00',True)])
def test_coverage_expiration_is_not_reported_as_not_due(monkeypatch,moment,expired):
    now=datetime.fromisoformat(moment)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    monkeypatch.setattr(engine,'datetime',Clock)
    old=pd.DataFrame({'Close':[100.]},index=pd.to_datetime(['2026-12-30']))
    before=old.copy(deep=True)
    health=engine.kr_price_health({'KOSPI':old},['2026-12-30'])
    assert health['kr_special_hours_reviewed_through']==2026
    assert (health['kr_price_status']=='calendar_unreviewed') is expired
    if expired:
        assert '이전 가격' in health['kr_price_message'] and '최신성 보증' in health['kr_price_message']
    pd.testing.assert_frame_equal(old,before)


def test_diagnostic_does_not_promote_unknown_future_bars():
    frame=pd.DataFrame({'Close':[100.], '_session_confirmed':[True], '_session_finality_version':[3]},index=pd.to_datetime(['2027-01-04']))
    assert pd.isna(engine.session_ready_times(frame.index,'KR').iloc[0])
    assert engine.kr_completed_cached_bars(Path('unused.pkl'),frame).empty

