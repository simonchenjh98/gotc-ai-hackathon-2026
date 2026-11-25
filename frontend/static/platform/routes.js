'use strict';

// Paths are relative to static/platform beside this document. Script evaluation
// declares controllers. Only the current route may mount one and issue reads.
const draftAssets = ['team/draft-store.js', 'submissions/materials.js', 'team/recovery.js'];
const teamAssets = [
  ...draftAssets, 'team/delegation.js', 'team/repository.js', 'submissions/uploads.js',
  'submissions/history.js', 'gateway/usage.js', 'gateway/keys.js', 'team/submission.js'
];
const reviewAssets = ['judging/rules.js', 'submissions/materials.js'];
const finalsAssets = ['finals/finals.css', 'polling.js', 'finals/shared.js'];
const roadshowLoginAssets = ['roadshow/access.css', 'roadshow/login.js'];
const roadshowAssets = [...reviewAssets, ...finalsAssets, ...roadshowLoginAssets, 'roadshow/scoring.js'];
const organizerAssets = [
  ...reviewAssets, 'submissions/history.js', 'admin/settings.js', 'admin/submissions.js',
  'admin/directory.js', 'claims/form.js', 'claims/review.js', '../registration/admin.js',
  'polling.js', 'finals/shared.js', 'finals/dashboard.js', 'admin/dashboard.css', 'roadshow/scoring.js', 'admin/event.js'
];

function workspace(title, role, files, render) {
  return {title, role, files: ['workspaces.css', ...files], render};
}

const closedBoard = {
  title: '成绩查看已关闭',
  files: [],
  render: () => {
    root.innerHTML = heading('RESULTS / 成绩管理', '全部队伍成绩看板已关闭',
      '全队排名与其他队伍分数仅供组织方后台核对。选手可登录“本队查分”查看自己队伍已发布的成绩。',
      '<a class="btn secondary small" href="#team-score">查看本队成绩 →</a>');
  }
};
const platformRoutes = {
  team: workspace('选手工作台', 'team', teamAssets, version => renderTeam(version)),
  'team-profile': workspace('本队资料与成员', 'team', ['../registration/profile.js'], version => renderTeamProfile(version)),
  'team-score': workspace('本队查分', 'team', ['team/score.js'], version => renderTeamScore(version)),
  claim: workspace('领取队伍参赛码', null, ['claims/form.js', 'claims/application.js'], version => renderClaim(version)),
  judge: workspace('专业评分（六维100分）', 'judge', [...reviewAssets, 'judging/judges.js'], version => renderJudge(version)),
  'judge-exhibition': workspace('展示评审记录', 'judge', [...reviewAssets, 'judging/judges.js'], version => renderExhibitionJudge(version)),
  'board-exhibition': workspace('展示评审进度（未计分）', null, ['judging/rules.js', 'polling.js', 'judging/boards.js'], version => renderExhibitionBoard(version)),
  'judge-roadshow': workspace('路演评分（六维100分）', 'judge', roadshowAssets, version => renderRoadshowJudge(version)),
  // Finals use a separate event access token and judge-seat flow.
  'judge-finals': workspace('决赛评分与专项奖提名', null, [...finalsAssets, 'finals/judges.js'], version => renderFinalsJudge(version)),
  audience: workspace('观众投票', 'audience', ['judging/audience.js'], version => renderAudience(version)),
  admin: workspace('组织方后台', 'admin', organizerAssets, version => renderAdmin(version)),
  'admin-roadshow': workspace('路演名单与实时成绩', 'admin', [...roadshowAssets, 'roadshow/roster.js'], version => renderRoadshowAccessAdmin(version)),
  'admin-finals': workspace('决赛实时控制台', 'admin', [...finalsAssets, 'finals/organizer.js'], version => renderFinalsAdmin(version)),
  'admin-api': workspace('AI API 管理', 'admin', ['admin/api.css', 'gateway/usage.js', 'gateway/logs.js', 'admin/api.js'], version => renderApiAdmin(version)),
  board: closedBoard,
  'board-roadshow': closedBoard,
  'board-finals': closedBoard
};

function routeTarget(page, login) {
  const target = platformRoutes[page];
  if (!target.role || target.role === login.role) return target;
  if (!login.role) {
    return {
      files: page === 'judge-roadshow' ? roadshowLoginAssets : [],
      render: () => renderLogin(target.role)
    };
  }
  if (login.role === 'admin') {
    if (page === 'team-profile') return target;
    if (page === 'team' || page === 'team-score') {
      return {files: ['team/delegation.js'], render: version => renderAdminTeamWorkbench(version)};
    }
  }
  return {files: [], render: () => renderWrongRole(target.role)};
}

// A packaged page pins its lazy manifest by URL and integrity. Source pages resolve directly.
const builtRouteManifest = null;
const platformAssetRequests = new Map();
function loadPlatformAsset(path, built) {
  if (platformAssetRequests.has(path)) return platformAssetRequests.get(path);
  const style = path.endsWith('.css');
  const element = document.createElement(style ? 'link' : 'script');
  const url = new URL(built ? built.path : 'static/platform/' + path, base).href;
  if (built) element.integrity = built.integrity;
  if (style) {
    element.rel = 'stylesheet';
    element.href = url;
  } else {
    element.src = url;
  }
  const pending = new Promise((resolve, reject) => {
    element.onload = resolve;
    element.onerror = () => {
      platformAssetRequests.delete(path);
      element.remove();
      const error = new Error(built ? '页面资源未能载入。请刷新页面，读取当前活动平台版本。' : '页面资源未能载入，请检查网络后重新连接。');
      error.refreshDocument = Boolean(built);
      reject(error);
    };
  });
  platformAssetRequests.set(path, pending);
  document.head.append(element);
  return pending;
}
async function loadPlatformAssets(paths, current = () => true) {
  if (!paths.length) return;
  if (builtRouteManifest) {
    await loadPlatformAsset('route-assets.js', builtRouteManifest);
    if (!current()) return;
  }
  await Promise.all(paths.map(path => {
    const built = builtRouteManifest && platformReleaseAssets[path];
    if (builtRouteManifest && !built) {
      const error = new Error('页面资源清单不完整，请刷新页面。');
      error.refreshDocument = true;
      throw error;
    }
    return loadPlatformAsset(path, built);
  }));
}
