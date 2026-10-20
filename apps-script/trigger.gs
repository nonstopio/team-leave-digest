/**
 * External trigger for the team leave digest.
 *
 * GitHub's own cron has been starting this workflow hours late (or not at all), so this script
 * asks GitHub to start it on time instead. See "External trigger" in the repository README.
 *
 * Setup: paste this file into a Google Apps Script project, add the script property GITHUB_TOKEN,
 * run testPreview() once to authorise it, then run installTriggers() once.
 *
 * The workflow itself skips weekends, company holidays and a day that was already sent, so a
 * duplicate call (for example GitHub's own cron firing late) is harmless.
 */

var REPO = 'nonstopio/team-leave-digest';
var WORKFLOW_FILE = 'digest.yml';
var REF = 'main';
var TIMEZONE = 'Asia/Kolkata';

/** Time-driven trigger: post today's digest. */
function runLive() {
  if (isWeekday_()) dispatch_('live');
}

/** Time-driven trigger: read-only check that today's digest was sent; alerts the owners if not. */
function runVerify() {
  if (isWeekday_()) dispatch_('verify');
}

/** Safe manual test: validates everything, writes nothing and posts nothing. */
function testPreview() {
  dispatch_('preview');
}

/** Run once. Re-running replaces the old triggers instead of adding duplicates. */
function installTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (['runLive', 'runVerify'].indexOf(t.getHandlerFunction()) !== -1) ScriptApp.deleteTrigger(t);
  });
  // Apps Script fires "near" the minute, within about 15 minutes either side.
  ScriptApp.newTrigger('runLive').timeBased().everyDays(1).atHour(11).nearMinute(30).inTimezone(TIMEZONE).create();
  ScriptApp.newTrigger('runVerify').timeBased().everyDays(1).atHour(12).nearMinute(45).inTimezone(TIMEZONE).create();
}

function isWeekday_() {
  // 'u' is the ISO day of week: 1 = Monday ... 7 = Sunday.
  return Number(Utilities.formatDate(new Date(), TIMEZONE, 'u')) <= 5;
}

function dispatch_(mode) {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    notifyFailure_(mode, 'Script property GITHUB_TOKEN is not set.');
    throw new Error('GITHUB_TOKEN is not set');
  }
  var response;
  try {
    response = UrlFetchApp.fetch(
      'https://api.github.com/repos/' + REPO + '/actions/workflows/' + WORKFLOW_FILE + '/dispatches', {
        method: 'post',
        contentType: 'application/json',
        headers: {
          Authorization: 'Bearer ' + token,
          Accept: 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28'
        },
        payload: JSON.stringify({ ref: REF, inputs: { mode: mode } }),
        muteHttpExceptions: true
      });
  } catch (e) {
    notifyFailure_(mode, 'Could not reach GitHub: ' + e);
    throw e;
  }
  var code = response.getResponseCode();
  if (code !== 204) {  // GitHub answers 204 No Content when the run was queued.
    var detail = 'GitHub answered HTTP ' + code + ': ' + String(response.getContentText()).slice(0, 300);
    notifyFailure_(mode, detail);
    throw new Error(detail);
  }
}

/** Email the script owner. This is deliberately independent of GitHub and Teams. */
function notifyFailure_(mode, detail) {
  MailApp.sendEmail(
    Session.getEffectiveUser().getEmail(),
    'Leave digest trigger failed (' + mode + ')',
    'The Apps Script trigger could not start the "' + mode + '" run of ' + REPO + '.\n\n' + detail +
    '\n\nRun the workflow manually from the repository Actions tab if today\'s digest is still needed. ' +
    'If the error mentions 401 or 403, the GITHUB_TOKEN has probably expired or lost access.');
}
