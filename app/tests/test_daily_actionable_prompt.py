"""Daily instructions must reach downloaded files without changing their data."""
import pytest
from gpt_prompts import (build_prompt, DAILY_ACTIONABLE_CHANGE_RULES, MODE_DETAILS, MODE_PROMPTS, MODE_OUTPUT_TEMPLATES,
                         COMMON_ANALYSIS_RULES, COMMON_DATA_VALIDATION_RULES, COMMON_VISUALIZATION_RULES)
from gpt_export import build_analysis_export
from test_gpt_export import inputs, payload


@pytest.mark.parametrize('mode',['morning','evening'])
def test_daily_guards_are_in_prompt_and_download_without_payload_changes(mode):
    args=inputs()
    export=build_analysis_export(*args, mode=mode)
    prompt=build_prompt(mode)
    assert export.text.count(DAILY_ACTIONABLE_CHANGE_RULES)==1
    for common in [COMMON_ANALYSIS_RULES,COMMON_DATA_VALIDATION_RULES,COMMON_VISUALIZATION_RULES]:
        assert common in prompt
    for rule in ['이전 비교 자료 없음', '시장별 직전 확정 거래일', '동일 가격 기준일',
                 '이전 적격 수가 0', '적격 제외 ≠ 추세 이탈', '30~50%',
                 'candidate_sample', 'history_update_ok=false', '모집단', '코드/설정 버전',
                 '순감소 9개', '트랙 이동', '거래일별 이력', '제외 사유 확인 불가']:
        assert rule in prompt
    a=payload(export);b=payload(build_analysis_export(*args))
    assert a['scope'].pop('analysis_mode')==mode
    assert b['scope'].pop('analysis_mode')=='comprehensive'
    for field in ['source','scope','regimes','sectors','candidate_sample','actionable_watchlist','units','research']:
        assert a[field]==b[field]


@pytest.mark.parametrize('mode',['comprehensive','weekly','stock_deep_dive'])
def test_daily_policy_does_not_change_other_mode_instructions(mode):
    assert DAILY_ACTIONABLE_CHANGE_RULES not in build_prompt(mode)
    expected="\n\n".join((COMMON_ANALYSIS_RULES,COMMON_DATA_VALIDATION_RULES,COMMON_VISUALIZATION_RULES,
        "【분석 모드】"+MODE_DETAILS[mode][0]+"\n"+MODE_PROMPTS[mode],
        "【출력 구성】"+MODE_OUTPUT_TEMPLATES[mode]))
    assert build_prompt(mode)==expected

