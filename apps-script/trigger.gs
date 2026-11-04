// Google Apps Script replacement for GitHub Actions' native "schedule" trigger,
// which repeatedly failed to fire at all for this repo even with 3x redundancy.
// This calls GitHub's workflow_dispatch API directly (the same mechanism `gh
// workflow run` uses, which has fired reliably every time it's been tried
// manually), driven by Apps Script's own time-based triggers instead of
// GitHub's.
//
// Setup (one-time, at script.google.com):
//   1. Project Settings -> set time zone to America/Los_Angeles (so atHour()
//      below means Pacific time year-round, DST handled automatically).
//   2. Project Settings -> Script Properties -> add GH_TOKEN = <fine-grained
//      PAT scoped to just this repo, Actions: Read and write>.
//   3. Run installTriggers() once (via the function dropdown + Run button).
//   4. Approve the authorization prompt (this script only calls out to
//      api.github.com, nothing else).

const REPO = 'tommyakashi/pollevnotifier';
const WORKFLOW_FILE = 'watch.yml';

function triggerPollEvWatch() {
  const token = PropertiesService.getScriptProperties().getProperty('GH_TOKEN');
  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;

  const options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + token,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    payload: JSON.stringify({ ref: 'main', inputs: { selftest: 'false' } }),
    muteHttpExceptions: true,
  };

  const response = UrlFetchApp.fetch(url, options);
  console.log(response.getResponseCode(), response.getContentText());
}

function installTriggers() {
  ScriptApp.getProjectTriggers().forEach((t) => {
    if (t.getHandlerFunction() === 'triggerPollEvWatch') ScriptApp.deleteTrigger(t);
  });

  // Each window gets a trigger at its start time plus a backup ~10 minutes
  // later, in case Apps Script itself ever skips one (watcher.py checks the
  // real Pacific time on every run and exits immediately if called outside
  // any configured window or if a window is already being watched, so a
  // redundant firing is harmless).
  const windows = [
    { day: ScriptApp.WeekDay.TUESDAY, hour: 11 }, // religion
    { day: ScriptApp.WeekDay.THURSDAY, hour: 11 }, // religion
    { day: ScriptApp.WeekDay.MONDAY, hour: 8 }, // donhanna710
    { day: ScriptApp.WeekDay.WEDNESDAY, hour: 8 }, // donhanna710
  ];

  windows.forEach((w) => {
    ScriptApp.newTrigger('triggerPollEvWatch').timeBased().onWeekDay(w.day).atHour(w.hour).nearMinute(0).create();
    ScriptApp.newTrigger('triggerPollEvWatch').timeBased().onWeekDay(w.day).atHour(w.hour).nearMinute(10).create();
  });

  console.log('Installed ' + ScriptApp.getProjectTriggers().length + ' triggers.');
}
