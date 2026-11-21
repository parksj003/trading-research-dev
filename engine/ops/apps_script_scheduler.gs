// Apps Script에서 기존 runHourlyTasks 함수부터 아래 부분을 이 내용으로 교체합니다.
// 기존 GITHUB_TOKEN/소유자/저장소 설정과 triggerGitHubWorkflow 함수는 유지하세요.
// 이 파일에는 토큰을 넣거나 Git에 커밋하지 않습니다.

function runHourlyTasks() {
  const now = new Date();
  const day = now.getUTCDay();
  const hour = now.getUTCHours();
  // 기존 Collector 실행 범위를 유지합니다. 미국 금요일 장과 월요일 준비 포함.
  const isWeekday = day >= 1 && day <= 5;
  const isSatMorning = day === 6 && (hour === 0 || hour === 1);
  const isMonPrep = day === 0 && hour === 21;
  if (isWeekday || isSatMorning || isMonPrep) {
    triggerGitHubWorkflow('trading_journal.yml', {
      ref: 'main', inputs: { mode: 'auto' }
    });
  } else {
    console.log('Collector 실행 시간 외: 건너뜁니다.');
  }
}

function schedulerKoreaWeekday(now) {
  // 프로젝트의 시간대 설정과 무관하게 한국 날짜를 사용합니다.
  return new Date(now.getTime() + 9 * 60 * 60 * 1000).getUTCDay();
}

function runMorningWatchlist() {
  const day = schedulerKoreaWeekday(new Date());
  if (day >= 2 && day <= 6) { // 한국시간 화~토: 미국 월~금 정규장 마감 후
    triggerGitHubWorkflow('watchlist.yml', {
      ref: 'main', inputs: { market: 'ALL', refresh_cache: 'false' }
    });
  } else {
    console.log('미국장 마감 분석 요일 외: 건너뜁니다.');
  }
}

function runAfternoonWatchlist() {
  const day = schedulerKoreaWeekday(new Date());
  if (day >= 1 && day <= 5) { // 한국시간 월~금: 한국장 마감 후
    triggerGitHubWorkflow('watchlist.yml', {
      ref: 'main', inputs: { market: 'ALL', refresh_cache: 'false' }
    });
  } else {
    console.log('한국장 마감 분석 요일 외: 건너뜁니다.');
  }
}

function configureSchedulerTriggers() {
  // 한 번 수동 실행합니다. 재실행해도 관리 대상 트리거는 세 개만 유지합니다.
  // 이 함수는 GitHub Workflow를 호출하지 않고 시간 트리거만 설정합니다.
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const handlers = ['runHourlyTasks', 'runMorningWatchlist', 'runAfternoonWatchlist'];
    const old = ScriptApp.getProjectTriggers().filter(t => handlers.includes(t.getHandlerFunction()));
    const created = [];
    try {
      // 새 트리거 생성에 실패하면 기존 설정을 유지합니다.
      created.push(ScriptApp.newTrigger('runHourlyTasks').timeBased().everyHours(1).create());
      created.push(ScriptApp.newTrigger('runMorningWatchlist').timeBased()
        .inTimezone('Asia/Seoul').atHour(7).nearMinute(0).everyDays(1).create());
      created.push(ScriptApp.newTrigger('runAfternoonWatchlist').timeBased()
        .inTimezone('Asia/Seoul').atHour(19).nearMinute(0).everyDays(1).create());
    } catch (error) {
      created.forEach(t => ScriptApp.deleteTrigger(t));
      throw error;
    }
    old.forEach(t => ScriptApp.deleteTrigger(t));
    console.log('설정 완료: Collector 매시간 / Watchlist 화~토 07:00, 월~금 19:00 (한국시간, 약 ±15분).');
  } finally {
    lock.releaseLock();
  }
}

