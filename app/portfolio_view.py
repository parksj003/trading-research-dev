"""Private portfolio UI. Authorize before I/O; never cache personal data globally."""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st


def setting(name, default=''):
    """Use the same resolver in Cloud and locally; arrays stay native values.

    Streamlit promotes only root scalar Secrets to os.environ, and does so when
    parsing them. Reading Secrets directly avoids relying on that side effect.
    """
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    try:
        value = st.secrets.get(name, default)
        return default if value is None else value
    except Exception:
        return default


def allowed_emails():
    value = setting('PORTFOLIO_ALLOWED_EMAILS')
    entries = value.split(',') if isinstance(value, str) else value
    if not isinstance(entries, (list, tuple)):
        return set()
    return {email.strip().casefold() for email in entries if isinstance(email, str) and email.strip()}


def auth_fields_present():
    names = ('redirect_uri', 'cookie_secret', 'client_id', 'client_secret', 'server_metadata_url')
    try:
        auth = st.secrets.get('auth', {})
        return {name: bool(auth.get(name)) for name in names}
    except Exception:
        return dict.fromkeys(names, False)


def allowed_user(user, allowed_emails):
    return (bool(getattr(user, 'is_logged_in', False))
            and user.get('email_verified') is True
            and str(user.get('email', '')).casefold() in allowed_emails)


def enabled():
    # A partially configured private deployment must show an actionable warning,
    # rather than silently hiding the screen. Unconfigured market apps stay as-is.
    return (any(bool(setting(name)) for name in
                ('PORTFOLIO_ALLOWED_EMAILS', 'PORTFOLIO_REPO', 'PORTFOLIO_FILE', 'PORTFOLIO_TOKEN'))
            or any(auth_fields_present().values()))


def read_snapshot():
    # Called only after render() authorizes this session. No cache decorator.
    local = setting('PORTFOLIO_FILE')
    if local:
        data = json.loads(Path(local).read_text(encoding='utf-8'))
    else:
        repo = str(setting('PORTFOLIO_REPO')).strip()
        if len(repo.split('/')) != 2 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./' for c in repo) or '..' in repo:
            raise ValueError('Invalid repository')
        token = setting('PORTFOLIO_TOKEN') or setting('GITHUB_TOKEN')
        if not token:
            raise ValueError('Missing token')
        headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}
        url = f'https://api.github.com/repos/{repo}'
        response = requests.get(url, headers=headers, timeout=(5, 20))
        response.raise_for_status()
        if response.json().get('private') is not True:
            raise ValueError('Public repositories are forbidden')
        response = requests.get(url + '/contents/private/portfolio/latest.json',
                                headers={**headers, 'Accept': 'application/vnd.github.raw+json'},
                                params={'ref': setting('PORTFOLIO_BRANCH', 'main')}, timeout=(5, 20))
        response.raise_for_status()
        data = response.json()
    if data.get('schema_version') != '1.0' or not isinstance(data.get('positions'), list):
        raise ValueError('Unsupported portfolio contract')
    return data


def security_label(position):
    code = str(position.get('code', ''))
    name = position.get('name')
    if isinstance(name, str) and name.strip().casefold() not in {'', 'none', 'nan', '<na>', code.casefold()}:
        return f'{name.strip()} ({code})'
    return code


def partition_positions(data):
    """Keep old snapshots readable without hiding uncertain inventories."""
    held, closed, review = [], list(data.get('closed') or []), []
    flat_warnings = {'order_ambiguous', 'quote_missing', 'quote_stale', 'technical_missing', 'stop_missing'}
    for position in data['positions']:
        try:
            qty = float(position['qty'])
        except (KeyError, TypeError, ValueError):
            qty = float('nan')
        if math.isfinite(qty) and qty > 0:
            held.append(position)
        elif qty == 0 and set(position.get('quality') or []) <= flat_warnings:
            closed.append(position)
        else:
            review.append(position)
    return held, closed, review


def render_position_history(closed, review):
    def rows(items):
        return [{'계좌': p.get('account'), '시장': p.get('market'), '종목': security_label(p),
                 '통화': p.get('currency'), '수량': p.get('qty'), '실현손익': p.get('realized_pnl'),
                 '신뢰도': ', '.join(p.get('quality') or []) or '정상'} for p in items]
    if review:
        st.warning('수량 계산을 확인해야 하는 항목이 있습니다. 아래 경고를 확인하세요. '
                   '보유 표에서 분리해도 스냅샷의 위험 계산 보류 상태는 유지됩니다.')
        with st.expander('보유수량 계산 확인 필요', expanded=True):
            st.dataframe(pd.DataFrame(rows(review)), hide_index=True)
    with st.expander('청산 종목 실현손익 (계좌·통화별)'):
        st.dataframe(pd.DataFrame(rows(closed)), hide_index=True)


def render():
    st.subheader('💼 내 포트폴리오')
    allowed = allowed_emails()
    if not allowed:
        st.warning('Streamlit Cloud Secrets의 루트에 PORTFOLIO_ALLOWED_EMAILS를 설정하세요. '
                   '쉼표 구분 문자열 또는 이메일 배열을 사용할 수 있습니다.')
        return
    if not allowed_user(st.user, allowed):
        st.info('허용된 사용자로 로그인해야 포트폴리오를 조회할 수 있습니다.')
        if not getattr(st.user, "is_logged_in", False) and st.button('로그인', key='portfolio_login'):
            try:
                st.login()
            except Exception:
                st.error('로그인을 시작할 수 없습니다. OIDC 설정을 확인하세요.')
        return
    if st.button('로그아웃', key='portfolio_logout'):
        for key in list(st.session_state):
            if key.startswith('portfolio_') and key != 'portfolio_logout':
                del st.session_state[key]
        st.logout()
        return
    try:
        data = read_snapshot()
        age = datetime.now(timezone.utc) - datetime.fromisoformat(data['generated_at'])
        stale = age.total_seconds() > 86400 or age.total_seconds() < 0
    except Exception:
        st.error('비공개 포트폴리오를 읽을 수 없습니다. 접근 설정과 스냅샷 상태를 확인하세요.')
        return
    if stale:
        st.warning('스냅샷이 오래되었거나 기준 시각을 확인할 수 없습니다. 아래 수치는 해당 생성 시점 기준입니다.')
    st.caption(f"생성 기준: {data['generated_at']} · 수수료·세금 제외 이동평균법 · 소수점 거래 계산 제외")
    rec = data.get('reconciliation', {})
    st.caption(f"원장 대조: {rec.get('status', 'not_checked')} · 불일치 {rec.get('mismatch_count', '미확인')}건")
    summary = data['summary']
    if not summary['complete']:
        st.warning('가격·원가·손절·환율 또는 계산 신뢰도가 불완전하여 통합 위험을 확정하지 않습니다.')
    if summary['equity_basis'] == 'stocks_only':
        st.info('비중·Heat의 자산 기준은 주식 평가액입니다. 현금 포함 NAV 및 신규 투자 가능 한도가 아닙니다.')
    compact = st.toggle('간단 보기 (휴대폰 권장)', value=True, key='portfolio_compact')
    cols = [st, st, st] if compact else st.columns(3)
    def fmt(value, suffix=''):
        return '미산정' if value is None else f'{value:,.2f}{suffix}'
    cols[0].metric('주식 평가액 (원)', fmt(summary['value_krw']))
    cols[1].metric('현재가 → 손절 위험 (원)', fmt(summary['heat_krw']))
    cols[2].metric('Heat', fmt(summary['heat_pct'], '%'))
    held, closed, review = partition_positions(data)
    rows = []
    for p in held:
        risk, tech = p['risk'], p.get('diagnostic', {})
        rows.append({'계좌': p['account'], '시장': p['market'], '종목': security_label(p), '통화': p['currency'],
                     '수량': p['qty'], '평단': p['avg_cost'], '평가액': p['value'],
                     '평가손익': p['unrealized_pnl'], '실현손익': p['realized_pnl'],
                     '손절가': risk.get('stop'), '손절 상태': risk['status'],
                     '손절 위험': risk['open_risk'],
                     '트레일링 이탈': bool((tech.get('trail') or {}).get('broken', False)),
                     '추세': tech.get('exit_signal') or tech.get('status', '미확인'),
                     '추세 상태': p.get('trend_state', 'data_unknown'),
                     'RS': tech.get('rs_score'), '섹터': tech.get('sector'),
                     '고급 진단': tech.get('advanced_status', 'computed' if tech.get('sma200') else '미확인'),
                     '시세 기준일': p['quote_as_of'], '신뢰도': ', '.join(p['quality']) or '정상'})
    frame = pd.DataFrame(rows)
    if frame.empty:
        st.info('표시할 보유 포지션이 없습니다.')
        render_position_history(closed, review)
        render_analysis(data, stale)
        return
    st.markdown('#### 우선 점검')
    alerts = frame[(frame['손절 상태'].isin(['breach', 'near'])) | frame['신뢰도'].ne('정상')
                   | frame['트레일링 이탈'] | frame['추세 상태'].isin(['trend_broken', 'weakening', 'early_weakening']) | frame['추세'].astype(str).str.contains('🚨|⚠|✂|🔴', regex=True)]
    st.dataframe(alerts, hide_index=True)
    st.markdown('#### 시장별 평가액 (통화별, 합산 환산 전)')
    st.dataframe(frame.groupby(['시장', '통화'], as_index=False)['평가액'].sum(min_count=1), hide_index=True)
    st.markdown('#### 보유 종목')
    markets = st.multiselect('시장', ['KR', 'US'], default=['KR', 'US'], key='portfolio_markets')
    selected = frame[frame['시장'].isin(markets)]
    simple_columns = ['종목', '시장', '통화', '평가손익', '손절 상태', '추세 상태', '신뢰도']
    st.dataframe(selected[simple_columns] if compact else selected, hide_index=True, width='stretch')
    if compact:
        st.caption('표는 좌우로 밀어서 확인할 수 있습니다. 계좌·수량·원가 등 전체 열은 간단 보기를 끄면 표시됩니다.')
    with st.expander('손절 시뮬레이션 (저장·주문 없음)'):
        options = [p for p in held if p['price'] is not None]
        if options:
            idx = st.selectbox('종목 선택', range(len(options)),
                               format_func=lambda i: f"{options[i]['account']} / {security_label(options[i])}", key='portfolio_sim_symbol')
            p = options[idx]
            stop = st.number_input('가정 손절가 (종목 통화)', min_value=0.0,
                                   value=float(p['risk'].get('stop') or p['price']), key=f"portfolio_sim_stop_{idx}")
            gap = st.slider('손절가 대비 추가 하락 가정 (%)', 0, 20, 0, key='portfolio_sim_gap')
            exit_price = min(p['price'], stop * (1-gap/100))
            st.metric(f"현재 평가액 대비 감소 ({p['currency']})", fmt(p['qty'] * (p['price']-exit_price)))
            if p['avg_cost'] is not None:
                st.metric(f"원가 대비 손익 ({p['currency']})", fmt(p['qty'] * (exit_price-p['avg_cost'])))
            st.caption('수수료·세금 제외 가정치입니다. 실제 체결가격은 보장되지 않습니다.')
    st.caption('손절가 이탈은 위험금액이 0으로 표시되어도 점검 대상입니다. 주문은 사용자가 결정합니다.')
    render_position_history(closed, review)
    render_analysis(data, stale)


def render_analysis(data, stale=False):
    """Private-only derived analysis; old snapshots remain readable."""
    allocation = data.get('allocation') or {}
    with st.expander('실제 보유 위험 반영 신규 배분', expanded=True):
        if stale:
            st.warning('지연된 스냅샷의 신규 배분은 사용하지 않습니다. 갱신이 필요합니다.')
        elif allocation.get('status') == 'ready':
            cols = [st, st, st] if st.session_state.get('portfolio_compact', False) else st.columns(3)
            cols[0].metric('기존 보유 노출', f"{allocation['held_alloc_pct']:.1f}%")
            cols[1].metric('추가 배분', f"{allocation['total_alloc_pct']:.1f}%")
            cols[2].metric('보유 + 신규 손절 위험', f"{allocation['combined_risk_pct']:.2f}%")
            st.dataframe(pd.DataFrame(allocation.get('rows', [])), hide_index=True)
            st.caption('NAV·시장·섹터·테마·동일 종목 한도를 모두 차감한 가정 배분입니다. 자동 주문하지 않습니다.')
        elif allocation.get('status') == 'no_candidates':
            st.info('현재 추가 배분 후보가 없습니다.')
        else:
            reasons = allocation.get('reasons') or ['새 형식의 스냅샷 갱신 필요']
            st.info('신규 배분 보류: ' + ', '.join(reasons))
    analytics = data.get('analytics') or {}
    with st.expander('거래 이력 · 이동평균 / FIFO 비교'):
        trades = analytics.get('trades') or []
        if trades:
            rows = []
            for trade in trades:
                signal = trade.get('entry_signal') or {}
                rows.append({**{k: trade.get(k) for k in ('source_row', 'account', 'market', 'code', 'currency',
                               'date', 'side', 'qty', 'amount', 'ma_realized', 'fifo_realized', 'fifo_recorded_costs_net')},
                             'quality': ', '.join(trade.get('quality') or []),
                             'entry_signal': signal.get('setup_stage') or signal.get('status'),
                             'signal_published_at': signal.get('published_at')})
            st.dataframe(pd.DataFrame(rows), hide_index=True)
        else:
            st.info('거래 분석은 새 스냅샷 생성 후 제공됩니다.')
        st.caption('FIFO는 보조 분석입니다. 비용 반영 값은 원장에 기록된 비용만 반영합니다. '
                   '소수점 거래는 행 전체 제외, 같은 날은 매수 우선이며 모호성 경고를 확인하세요.')
    with st.expander('실제 매매 성과와 진입 당시 신호'):
        stats = analytics.get('signal_performance') or []
        if stats:
            table = pd.DataFrame(stats)
            table['win_rate_pct'] = table['wins'] / table['count'] * 100
            st.dataframe(table, hide_index=True)
        else:
            st.info('신뢰 가능한 청산 사이클이 아직 없습니다.')
        st.dataframe(pd.DataFrame(analytics.get('cycles') or []), hide_index=True)
        st.caption('거래 전에 게시된 시각이 확인되는 신호만 연결합니다. 시각이 없는 거래는 현지 거래일 '
                   '시작 전까지의 신호만 사용합니다. 미확인 신호는 별도 분류하며 계좌 NAV 성과가 아닙니다.')
    with st.expander('매도 후 재진입 관찰'):
        rows = data.get('reentry') or []
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True)
        else:
            st.info('관찰할 청산 사이클이 없습니다.')
        st.caption('청산 후 형성된 베이스 또는 새 돌파, 현재 적격 셋업과 레짐을 함께 확인합니다. '
                   '데이터 미확인은 매수 적격을 뜻하지 않습니다. 최근 청산 20건까지 진단을 보완합니다.')


def configuration_status():
    """Presence-only preflight; never prints credentials or loads account data."""
    import importlib.util
    names = ('PORTFOLIO_ALLOWED_EMAILS', 'PORTFOLIO_REPO', 'PORTFOLIO_FILE',
             'PORTFOLIO_BRANCH', 'PORTFOLIO_TOKEN', 'GITHUB_TOKEN')
    present = {name: bool(setting(name)) for name in names}
    auth_file_present = any(p.exists() for p in (Path.cwd() / '.streamlit/secrets.toml', Path.home() / '.streamlit/secrets.toml'))
    return {'variables_present': present, 'oidc_secrets_file_present': auth_file_present,
            'oidc_fields_present': auth_fields_present(), 'allowlist_configured': bool(allowed_emails()),
            'authlib_installed': importlib.util.find_spec('authlib') is not None,
            'live_login_verified': False,
            'source_configured': present['PORTFOLIO_FILE'] or (present['PORTFOLIO_REPO'] and (present['PORTFOLIO_TOKEN'] or present['GITHUB_TOKEN']))}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Private portfolio configuration presence check')
    parser.add_argument('--check-config', action='store_true', required=True)
    parser.parse_args()
    status = configuration_status()
    print(json.dumps(status, ensure_ascii=False, indent=2))
    # Presence does not prove OIDC correctness or remote authorization.
    raise SystemExit(0 if status['source_configured'] and status['allowlist_configured']
                     and status['authlib_installed'] and all(status['oidc_fields_present'].values()) else 2)

