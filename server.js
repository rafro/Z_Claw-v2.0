// OpenClaw Mission Control — API + Static Server + Scheduler
// Port: 3000

const http   = require('http');
const https  = require('https');
const fs     = require('fs');
const path   = require('path');
const url    = require('url');
const crypto = require('crypto');
const { spawn, spawnSync } = require('child_process');
const cron   = require('node-cron');

const ROOT      = __dirname;
const STATE_DIR = path.join(ROOT, 'state');
const PORT      = 3000;

// ── Mobile SSE subscribers ──
const _mobileAlertSubscribers = new Set();

// ── Gamification SSE subscribers (PC + mobile) ──
const _gamifSubscribers = new Set();

// ── Pending coding approvals: sessionId → { preSessionHead, filesChanged, diffStat, timer } ──
const _pendingCodingApprovals = new Map();

// ── Load .env into process.env ──
function loadEnv() {
  try {
    fs.readFileSync(path.join(ROOT, '.env'), 'utf8').split('\n').forEach(line => {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) return;
      const eq = trimmed.indexOf('=');
      if (eq > 0) process.env[trimmed.slice(0, eq).trim()] = trimmed.slice(eq + 1).trim();
    });
  } catch(e) {}
}
loadEnv();

// ── MIME types ──
const MIME = {
  '.html': 'text/html',
  '.css':  'text/css',
  '.js':   'application/javascript',
  '.json': 'application/json',
  '.png':  'image/png',
  '.gif':  'image/gif',
  '.ico':  'image/x-icon',
  '.svg':  'image/svg+xml',
  '.woff2':'font/woff2',
  '.woff': 'font/woff',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.pdf':  'application/pdf',
};

// ── State helpers ──
function readState(file) {
  try { return JSON.parse(fs.readFileSync(path.join(STATE_DIR, file), 'utf8').replace(/^\uFEFF/, '')); }
  catch { return null; }
}

function writeState(file, data) {
  fs.writeFileSync(path.join(STATE_DIR, file), JSON.stringify(data, null, 2));
}

function simpleId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

// ── Logging helpers ──
function logActivity(division, message, color) {
  try {
    const log = readState('activity-log.json') || { entries: [] };
    log.entries.push({ time: new Date().toISOString(), division, message, color });
    if (log.entries.length > 50) log.entries = log.entries.slice(-50);
    writeState('activity-log.json', log);
    console.log(`  [${division}] ${message}`);
  } catch(e) {}
}

function updateDivisionState(division, status) {
  try {
    const os = readState('orchestrator-state.json') || { divisions: {} };
    if (!os.divisions) os.divisions = {};
    if (!os.divisions[division]) os.divisions[division] = {};
    os.divisions[division].last_run = new Date().toISOString();
    os.divisions[division].status = status;
    writeState('orchestrator-state.json', os);
  } catch(e) {}
}

// ── HTTP GET helper (native, no deps) ──
function httpGet(reqUrl, redirects = 0) {
  return new Promise((resolve, reject) => {
    if (redirects > 3) return reject(new Error('too many redirects'));
    const parsed = new url.URL(reqUrl);
    const lib = parsed.protocol === 'https:' ? https : http;
    const options = {
      hostname: parsed.hostname,
      port: parsed.port || (parsed.protocol === 'https:' ? 443 : 80),
      path: parsed.pathname + parsed.search,
      headers: { 'User-Agent': 'OpenClaw/1.0', 'Accept': '*/*' },
      timeout: 12000,
    };
    const req = lib.get(options, res => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        return httpGet(res.headers.location, redirects + 1).then(resolve).catch(reject);
      }
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => resolve(data));
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('timeout')); });
  });
}

// ── XP / Rank helpers ──
const BASE_RANKS = [
  { minLevel: 1,  maxLevel: 4,   title: 'Apprentice of the Realm' },
  { minLevel: 5,  maxLevel: 9,   title: 'Keeper of Systems' },
  { minLevel: 10, maxLevel: 19,  title: 'Commander of the Realm' },
  { minLevel: 20, maxLevel: 34,  title: 'Warlord of Automation' },
  { minLevel: 35, maxLevel: 49,  title: 'Grand Sovereign' },
  { minLevel: 50, maxLevel: 999, title: 'The Eternal Orchestrator' },
];

const DIV_RANKS = {
  trading:        ['Market Scout', 'Market Adept', 'Market Expert', 'Trading Master', 'Oracle of Markets'],
  opportunity:    ['Hunter', 'Opportunity Adept', 'Grand Hunter', 'Grand Headhunter', 'Sovereign Headhunter'],
  dev_automation: ['Code Ward', 'Code Adept', 'Code Expert', 'Code Architect', 'Architect of the Realm'],
  personal:       ['Keeper', 'Wellness Adept', 'Wellness Expert', 'Guardian of the Flame', 'Eternal Guardian'],
  op_sec:         ['Watchman', 'Security Adept', 'Security Expert', 'Grand Sentinel', 'Sovereign Sentinel'],
};

const DIV_XP_THRESHOLDS = [0, 51, 151, 301, 500];

// XP granted per skill completion (server-side, deterministic)
const SKILL_XP = {
  'job-intake':       { division: 'opportunity',    amount: 10 },
  'hard-filter':      { division: 'opportunity',    amount: 5  },
  'virtual-trader':   { division: 'trading',        amount: 10 },
  'trading-report':   { division: 'trading',        amount: 15 },
  'repo-monitor':     { division: 'dev_automation', amount: 10 },
  'security-scan':    { division: 'op_sec',         amount: 15 },
  'device-posture':   { division: 'op_sec',         amount: 5  },
  'breach-check':     { division: 'op_sec',         amount: 10 },
  'threat-surface':   { division: 'op_sec',         amount: 10 },
  'cred-audit':       { division: 'op_sec',         amount: 15 },
  'privacy-scan':     { division: 'op_sec',         amount: 10 },
  'health-logger':    { division: 'personal',       amount: 15 },
  'perf-correlation': { division: 'personal',       amount: 10 },
  'funding-finder':   { division: 'opportunity',    amount: 5  },
};

const PYTHON_EXE = 'C:/Users/Tyler/AppData/Local/Microsoft/WindowsApps/PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0/python.exe';

// Maps skill name → divState (orchestrator-state.json key) + division + task (run_division.py args)
// divState uses underscore (legacy state file key); division uses hyphen (run_division.py arg)
const SKILL_TASK_MAP = {
  'job-intake':       { divState: 'opportunity',    division: 'opportunity',    task: 'job-intake'       },
  'hard-filter':      { divState: 'opportunity',    division: 'opportunity',    task: 'job-intake'       }, // hard-filter runs inside job-intake Python pipeline
  'funding-finder':   { divState: 'opportunity',    division: 'opportunity',    task: 'funding-finder'   },
  'virtual-trader':   { divState: 'trading',        division: 'trading',        task: 'virtual-trader'   },
  'trading-report':   { divState: 'trading',        division: 'trading',        task: 'trading-report'   },
  'market-scan':      { divState: 'trading',        division: 'trading',        task: 'market-scan'      },
  'health-logger':    { divState: 'personal',       division: 'personal',       task: 'health-logger'    },
  'perf-correlation': { divState: 'personal',       division: 'personal',       task: 'perf-correlation' },
  'burnout-monitor':  { divState: 'personal',       division: 'personal',       task: 'burnout-monitor'  },
  'personal-digest':  { divState: 'personal',       division: 'personal',       task: 'personal-digest'  },
  'repo-monitor':     { divState: 'dev_automation', division: 'dev-automation', task: 'repo-monitor'     },
  'debug-agent':      { divState: 'dev_automation', division: 'dev-automation', task: 'debug-agent'      },
  'refactor-scan':    { divState: 'dev_automation', division: 'dev-automation', task: 'refactor-scan'    },
  'doc-update':       { divState: 'dev_automation', division: 'dev-automation', task: 'doc-update'       },
  'artifact-manager': { divState: 'dev_automation', division: 'dev-automation', task: 'artifact-manager' },
  'dev-digest':       { divState: 'dev_automation', division: 'dev-automation', task: 'dev-digest'       },
  // OP-Sec Division
  'mobile-audit-review': { divState: 'op_sec', division: 'op-sec', task: 'mobile-audit-review' },
  'device-posture':   { divState: 'op_sec', division: 'op-sec', task: 'device-posture'  },
  'breach-check':     { divState: 'op_sec', division: 'op-sec', task: 'breach-check'    },
  'threat-surface':   { divState: 'op_sec', division: 'op-sec', task: 'threat-surface'  },
  'cred-audit':       { divState: 'op_sec', division: 'op-sec', task: 'cred-audit'      },
  'privacy-scan':     { divState: 'op_sec', division: 'op-sec', task: 'privacy-scan'    },
  'security-scan':    { divState: 'op_sec', division: 'op-sec', task: 'security-scan'   },
  'opsec-digest':     { divState: 'op_sec', division: 'op-sec', task: 'opsec-digest'    },
};

function rankForLevel(level) {
  return (BASE_RANKS.find(r => level >= r.minLevel && level <= r.maxLevel) || BASE_RANKS[0]).title;
}

// XP required to advance FROM each level (index = level).
// Calibrated for ~2 months to reach Level 10 at 100-150 XP/day.
// Total XP to reach Level 10: 7,480 XP.
const XP_PER_LEVEL = [0, 100, 180, 300, 450, 650, 900, 1200, 1600, 2100];

function xpForNextLevel(level) {
  if (level < XP_PER_LEVEL.length) return XP_PER_LEVEL[level];
  // Level 10+: geometric growth (~30% harder per level)
  return Math.round(2100 * Math.pow(1.3, level - 9));
}

function applyXP(stats, amount) {
  // Guard against missing fields (Python xp.py writes a different schema)
  if (!stats.xp_to_next_level) stats.xp_to_next_level = xpForNextLevel(stats.level || 1);
  if (!stats.total_xp_earned)  stats.total_xp_earned  = 0;
  stats.base_xp += amount;
  stats.total_xp_earned += amount;
  let leveled = false;
  while (stats.base_xp >= stats.xp_to_next_level) {
    stats.base_xp -= stats.xp_to_next_level;
    stats.level++;
    stats.xp_to_next_level = xpForNextLevel(stats.level);
    leveled = true;
  }
  const newRank = rankForLevel(stats.level);
  const rankChanged = newRank !== stats.rank;
  stats.rank = newRank;
  stats.last_updated = new Date().toISOString();
  return { leveled, rankChanged };
}

// Grant division XP — called after every successful skill run.
// Division XP tracks skill activity and division rank only.
// Base XP is granted exclusively by the Ruler via /api/bestow.
function grantDivisionXP(division, amount, skillName = null) {
  try {
    const stats = readState('jclaw-stats.json');
    if (!stats) return;

    if (!stats.divisions[division]) {
      stats.divisions[division] = { xp: 0, rank: (DIV_RANKS[division] || ['Unknown'])[0] };
    }

    const oldDivXP = stats.divisions[division].xp;
    stats.divisions[division].xp += amount;
    const newDivXP = stats.divisions[division].xp;

    // Update division rank based on thresholds
    const divRanks = DIV_RANKS[division] || [];
    const rankIdx = DIV_XP_THRESHOLDS.filter(t => newDivXP >= t).length - 1;
    stats.divisions[division].rank = divRanks[Math.min(rankIdx, divRanks.length - 1)] || stats.divisions[division].rank;

    // Log division rank milestone (new tier unlocked)
    const oldRankIdx = DIV_XP_THRESHOLDS.filter(t => oldDivXP >= t).length - 1;
    if (rankIdx > oldRankIdx) {
      logActivity('SYS', `⚔ ${division} rank up: ${stats.divisions[division].rank}`, 'purple');
    }

    stats.last_updated = new Date().toISOString();
    writeState('jclaw-stats.json', stats);

    const resolved = skillName || Object.keys(SKILL_XP).find(k => SKILL_XP[k].division === division) || 'unknown';
    setImmediate(() => handleGamifCheck(resolved, division));
  } catch(e) {}
}

// ── Gamification Engine ──────────────────────────────────────────────────────

function _getWeekKey(date) {
  const d = new Date(date);
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() + 3 - ((d.getDay() + 6) % 7));
  const week = Math.floor((d - new Date(d.getFullYear(), 0, 4)) / 604800000);
  return `${d.getFullYear()}-${String(week + 1).padStart(2, '0')}`;
}

function _ensureStreaks(stats) {
  if (!stats.streaks) stats.streaks = {};
  for (const d of ['opportunity', 'trading', 'dev_automation', 'personal', 'op_sec']) {
    if (!stats.streaks[d]) {
      stats.streaks[d] = { current: 0, longest: 0, last_date: null, shield_this_week: false, week: null };
    }
  }
}

// Returns streak milestone count if hit a multiple of 7, else false.
function _updateStreak(stats, division) {
  _ensureStreaks(stats);
  const s = stats.streaks[division];
  if (!s) return false;

  const today     = new Date().toISOString().slice(0, 10);
  const week      = _getWeekKey(new Date());
  if (s.week !== week) { s.shield_this_week = false; s.week = week; }
  if (s.last_date === today) return false;

  const prev = new Date();
  prev.setDate(prev.getDate() - 1);
  const yesterday = prev.toISOString().slice(0, 10);

  if (!s.last_date || s.last_date === yesterday) {
    s.current++;
  } else if (!s.shield_this_week) {
    s.shield_this_week = true;
    s.current++;
  } else {
    s.current = 1;
  }

  s.longest   = Math.max(s.longest, s.current);
  s.last_date = today;
  return (s.current > 0 && s.current % 7 === 0) ? s.current : false;
}

function _checkAchievements(stats) {
  const earned   = new Set(stats.achievements || []);
  const unlocked = [];
  const divXP    = div => ((stats.divisions || {})[div] || {}).xp || 0;
  const divIdx   = div => DIV_XP_THRESHOLDS.filter(t => divXP(div) >= t).length - 1;

  const checks = [
    { id: 'first_hunt',      cond: () => divXP('opportunity') > 0 },
    { id: 'market_watcher',  cond: () => divXP('trading') > 0 },
    { id: 'code_warden',     cond: () => divXP('dev_automation') > 0 },
    { id: 'healthy_habits',  cond: () => Object.values(stats.streaks || {}).some(s => (s.longest || 0) >= 7) },
    { id: 'division_master', cond: () => Object.keys(stats.divisions || {}).some(d => divIdx(d) >= 3) },
    { id: 'realm_commander', cond: () => (stats.level || 1) >= 10 },
    { id: 'eternal',         cond: () => (stats.level || 1) >= 50 },
  ];

  for (const { id, cond } of checks) {
    if (!earned.has(id) && cond()) {
      stats.achievements.push(id);
      earned.add(id);
      unlocked.push(id);
    }
  }
  return unlocked;
}

function _broadcastGamifEvent(event) {
  if (_gamifSubscribers.size === 0) return;
  const payload = JSON.stringify({ type: 'gamif', ...event });
  for (const res of _gamifSubscribers) {
    try { res.write(`data: ${payload}\n\n`); } catch(e) { _gamifSubscribers.delete(res); }
  }
}

function _appendXpHistory(entry) {
  try {
    const line = JSON.stringify({ ts: new Date().toISOString(), ...entry }) + '\n';
    fs.appendFileSync(path.join(STATE_DIR, 'xp-history.jsonl'), line);
  } catch(e) {}
}

// Called after every skill completion. Updates streak, checks achievements,
// broadcasts SSE, appends telemetry. Does NOT modify division XP.
function handleGamifCheck(skillName, divisionKey) {
  try {
    const stats = readState('jclaw-stats.json');
    if (!stats) return;

    _ensureStreaks(stats);
    const streakMilestone = _updateStreak(stats, divisionKey);
    const newAchievements = _checkAchievements(stats);

    stats.last_updated = new Date().toISOString();
    writeState('jclaw-stats.json', stats);

    const divStats = (stats.divisions || {})[divisionKey] || {};
    const xpGrant  = (SKILL_XP[skillName] || {}).amount || 0;
    _broadcastGamifEvent({
      event: 'skill_complete', skill: skillName, division: divisionKey,
      xp_granted: xpGrant, division_xp: divStats.xp, division_rank: divStats.rank,
      streak: ((stats.streaks[divisionKey] || {}).current) || 0,
    });

    if (streakMilestone) {
      _broadcastGamifEvent({ event: 'streak_milestone', division: divisionKey, streak: streakMilestone });
      logActivity('SYS', `🔥 ${divisionKey} streak: ${streakMilestone} days`, 'yellow');
    }

    for (const achievement of newAchievements) {
      _broadcastGamifEvent({ event: 'achievement_unlock', achievement });
      logActivity('SYS', `🏆 Achievement unlocked: ${achievement}`, 'yellow');
    }

    _appendXpHistory({ event: 'skill_complete', skill: skillName, div: divisionKey, xp: xpGrant,
      streak: ((stats.streaks[divisionKey] || {}).current) || 0 });
  } catch(e) {}
}

function handleGamifStream(req, res) {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache',
    'Connection': 'keep-alive', 'X-Accel-Buffering': 'no',
    'Access-Control-Allow-Origin': '*',
  });
  try {
    const stats = readState('jclaw-stats.json');
    if (stats) res.write(`data: ${JSON.stringify({ type: 'gamif', event: 'init', stats })}\n\n`);
  } catch(e) {}
  const hb = setInterval(() => { try { res.write(': ping\n\n'); } catch(e) {} }, 25000);
  _gamifSubscribers.add(res);
  req.on('close', () => { clearInterval(hb); _gamifSubscribers.delete(res); });
}

// ── API handlers ──

// POST /api/control  { skill: "job-intake" }
function handleControl(body, res) {
  const { skill } = body;
  if (!skill) return jsonError(res, 400, 'skill required');

  const state = readState('control.json') || { queue: [], last_processed: null };
  state.queue.push({ id: simpleId(), skill, requested_at: new Date().toISOString(), status: 'queued' });
  writeState('control.json', state);

  logActivity('SYS', `Run requested: ${skill}`, 'yellow');
  jsonOk(res, { ok: true, skill, status: 'queued' });
}

// POST /api/bestow  { amount, reason }
function handleBestow(body, res) {
  const amount = parseInt(body.amount) || 50;
  const reason = body.reason || 'Ruler\'s decree';
  if (amount <= 0 || amount > 10000) return jsonError(res, 400, 'invalid amount');

  const stats = readState('jclaw-stats.json');
  if (!stats) return jsonError(res, 500, 'jclaw-stats.json not found');

  const oldRank  = stats.rank;
  const oldLevel = stats.level;
  stats.total_rewards_from_ruler++;

  if (!stats.achievements.includes('rulers_blessing')) {
    stats.achievements.push('rulers_blessing');
  }

  _ensureStreaks(stats);
  const { leveled, rankChanged } = applyXP(stats, amount);
  const newAchievements = _checkAchievements(stats);
  writeState('jclaw-stats.json', stats);

  logActivity('SYS', `⚔ Ruler bestowed ${amount} XP — ${reason}`, 'yellow');
  if (rankChanged) {
    logActivity('SYS', `⚔ RANK UP: ${oldRank} → ${stats.rank} (Lvl ${stats.level})`, 'purple');
  }

  _broadcastGamifEvent({
    event: 'xp_grant', source: 'ruler', amount,
    level: stats.level, rank: stats.rank, rank_up: rankChanged,
    old_rank: oldRank, base_xp: stats.base_xp, xp_to_next_level: stats.xp_to_next_level,
  });
  for (const achievement of newAchievements) {
    _broadcastGamifEvent({ event: 'achievement_unlock', achievement });
    logActivity('SYS', `🏆 Achievement unlocked: ${achievement}`, 'yellow');
  }
  _appendXpHistory({ event: 'ruler_bestow', amount, level: stats.level, rank: stats.rank, reason });

  jsonOk(res, {
    ok: true, amount, reason,
    new_level: stats.level, new_rank: stats.rank,
    base_xp: stats.base_xp, xp_to_next_level: stats.xp_to_next_level,
    rank_up: rankChanged, old_rank: oldRank,
    achievements_unlocked: newAchievements,
  });
}

// POST /api/applications/:id/status  { status: "applied|skipped|archived" }
function handleAppStatus(jobId, body, res) {
  const { status } = body;
  const valid = ['applied', 'skipped', 'archived', 'interview', 'rejected'];
  if (!valid.includes(status)) return jsonError(res, 400, 'invalid status');

  const apps = readState('applications.json');
  if (!apps) return jsonError(res, 500, 'applications.json not found');

  const job = apps.pipeline.find(j => j.id === jobId);
  if (!job) return jsonError(res, 404, 'job not found');

  job.status = status;
  job.actioned_at = new Date().toISOString();

  const counts = { applied: 0, interviews: 0, rejected: 0, pending_review: 0 };
  apps.pipeline.forEach(j => {
    if (j.status === 'pending_review') counts.pending_review++;
    if (j.status === 'applied')        counts.applied++;
    if (j.status === 'interview')      counts.interviews++;
    if (j.status === 'rejected')       counts.rejected++;
  });
  apps.stats = counts;
  writeState('applications.json', apps);

  jsonOk(res, { ok: true, id: jobId, status });
}

// POST /api/grants/:id/status  { status: "applied|archived" }
function handleGrantStatus(grantId, body, res) {
  const { status } = body;
  const valid = ['applied', 'archived'];
  if (!valid.includes(status)) return jsonError(res, 400, 'invalid status');

  const fp = readState('funding-pipeline.json');
  if (!fp) return jsonError(res, 500, 'funding-pipeline.json not found');

  const grant = fp.pipeline.find(g => g.id === grantId);
  if (!grant) return jsonError(res, 404, 'grant not found');

  grant.status = status;
  grant.actioned_at = new Date().toISOString();

  const counts = { pending_review: 0, applied: 0, archived: 0 };
  fp.pipeline.forEach(g => {
    if (g.status === 'pending_review') counts.pending_review++;
    if (g.status === 'applied')        counts.applied++;
    if (g.status === 'archived')       counts.archived++;
  });
  fp.stats = counts;
  writeState('funding-pipeline.json', fp);

  jsonOk(res, { ok: true, id: grantId, status });
}

// GET /api/jobs  — returns pending/applied jobs for dashboard
function handleGetJobs(res) {
  const apps = readState('applications.json') || { pipeline: [], stats: {} };
  const pending = apps.pipeline.filter(j => j.status === 'pending_review');
  const applied = apps.pipeline.filter(j => ['applied','interview','rejected'].includes(j.status));
  jsonOk(res, { pending, applied, stats: apps.stats });
}

// GET /api/grants  — returns pending grants for dashboard
function handleGetGrants(res) {
  const fp = readState('funding-pipeline.json') || { pipeline: [], stats: {} };
  const pending = fp.pipeline.filter(g => g.status === 'pending_review');
  const applied = fp.pipeline.filter(g => g.status === 'applied');
  jsonOk(res, { pending, applied, stats: fp.stats });
}

// GET /api/packets  — returns all division executive packets organised by division
function handleGetPackets(res) {
  const divisions = ['trading', 'opportunity', 'dev-automation', 'personal', 'op-sec', 'sentinel'];
  const result = {};
  for (const div of divisions) {
    const packetDir = path.join(ROOT, 'divisions', div, 'packets');
    result[div] = {};
    if (!fs.existsSync(packetDir)) continue;
    const files = fs.readdirSync(packetDir).filter(f => f.endsWith('.json'));
    for (const f of files) {
      try {
        const pkt = JSON.parse(fs.readFileSync(path.join(packetDir, f), 'utf8'));
        const skill = f.replace('.json', '');
        result[div][skill] = pkt;
      } catch(e) {}
    }
  }
  jsonOk(res, result);
}

// GET /api/trading/cycle  — returns agent-network cycle state for dashboard
function proxyZenith(method, zenithPath, body, res) {
  const payload = body ? JSON.stringify(body) : null;
  const opts = {
    hostname: '127.0.0.1', port: 8000, path: zenithPath, method,
    headers: { 'Content-Type': 'application/json', ...(payload ? { 'Content-Length': Buffer.byteLength(payload) } : {}) }
  };
  const req = http.request(opts, (r) => {
    let data = '';
    r.on('data', c => data += c);
    r.on('end', () => {
      try { jsonOk(res, JSON.parse(data)); } catch(e) { jsonOk(res, { raw: data }); }
    });
  });
  req.on('error', () => jsonOk(res, { error: 'agent-network offline', available: false }));
  if (payload) req.write(payload);
  req.end();
}

function handleGetTradingCycle(res) {
  try {
    const agentNetworkState = 'C:/Users/Tyler/agent-network/state';
    const fs2 = require('fs');
    const path2 = require('path');

    // Find most recent cycle state file
    let cycleData = null;
    let files = [];
    try {
      files = fs2.readdirSync(agentNetworkState)
        .filter(f => f.endsWith('_cycle_state.json'))
        .map(f => ({ f, mtime: fs2.statSync(path2.join(agentNetworkState, f)).mtimeMs }))
        .sort((a, b) => b.mtime - a.mtime);
      if (files.length) {
        cycleData = JSON.parse(fs2.readFileSync(path2.join(agentNetworkState, files[0].f), 'utf8'));
      }
    } catch(e) {}

    if (!cycleData) return jsonOk(res, { available: false });

    const strat = cycleData.active_strategy || {};
    const recentTrades = (cycleData.trade_log || []).slice(-50).reverse();
    const weekly = (cycleData.weekly_reviews || []);
    const lastWeekly = weekly[weekly.length - 1] || {};

    // Staleness check
    const statFile = path2.join(agentNetworkState, files[0].f);
    const mtimeMs  = fs2.statSync(statFile).mtimeMs;
    const hoursSince = (Date.now() - mtimeMs) / 3_600_000;
    const stale = hoursSince > 6;

    // Today's trades
    const todayStr = new Date().toISOString().slice(0, 10);
    const todayTrades = (cycleData.trade_log || []).filter(t => {
      if (!t.timestamp) return false;
      return new Date(t.timestamp).toISOString().slice(0, 10) === todayStr;
    });

    jsonOk(res, {
      available: true,
      cycle_number:     cycleData.cycle_number,
      risk_multiplier:  cycleData.risk_multiplier,
      stale,
      hours_since_update: Math.round(hoursSince * 10) / 10,
      last_modified: new Date(mtimeMs).toISOString(),
      active_strategy: {
        name:           strat.strategy_name || 'None',
        sharpe:         strat.sharpe,
        sortino:        strat.sortino,
        win_rate:       strat.win_rate ? Math.round(strat.win_rate * 100) : null,
        avg_r:          strat.avg_r,
        avg_win_r:      strat.avg_win_r,
        avg_loss_r:     strat.avg_loss_r,
        rr_ratio:       strat.rr_ratio,
        rr_display:     strat.rr_display,
        profit_factor:  strat.profit_factor,
        max_drawdown_pct: strat.max_drawdown_pct != null ? strat.max_drawdown_pct : (strat.max_drawdown != null ? Math.round(strat.max_drawdown * 1000) / 10 : null),
        trade_count:    strat.trade_count,
        oos_sharpe:     strat.oos_sharpe,
        oos_win_rate:   strat.oos_win_rate ? Math.round(strat.oos_win_rate * 100) : null,
        theoretical_ev_r: strat.theoretical_ev_r,
        empirical_ev_r:   strat.empirical_ev_r,
      },
      agents: {
        strategy_builder: { role: 'Generates 100 strategies/cycle',      last_output: `Cycle ${cycleData.cycle_number}` },
        backtester:       { role: 'Evaluates & selects top 3 strategies', last_output: strat.strategy_name || '—' },
        trader:           { role: 'Executes trades with risk controls',   last_output: `${todayTrades.length} trade(s) today` },
        trading_coach:    { role: 'Reviews performance, adjusts risk',    last_output: lastWeekly.health_tier ? `Health: ${lastWeekly.health_tier}` : 'No review yet' },
      },
      recent_trades: recentTrades.map(t => ({
        symbol:     t.symbol,
        pnl:        t.pnl,
        r_multiple: t.r_multiple,
        result:     t.result,
        reason:     t.reason,
        entry_price: t.entry_price,
        exit_price:  t.exit_price,
        risk_usd:    t.risk_usd,
        date: t.timestamp ? new Date(t.timestamp).toISOString().slice(0,10) : null,
      })),
      weekly_reviews: weekly,
      performance_summary: {
        total_trades: (cycleData.trade_log || []).length,
        cycle_number: cycleData.cycle_number,
        asset_key:    cycleData.asset_key,
        risk_multiplier: cycleData.risk_multiplier,
      },
    });
  } catch(e) {
    jsonOk(res, { available: false, error: e.message });
  }
}

// POST /api/agents/toggle  { division: "opportunity", agent: "job-intake" }
function handleToggle(body, res) {
  const { division, agent } = body;
  if (!division || !agent) return jsonError(res, 400, 'division and agent required');

  const overrides = readState('agent-overrides.json') || {};
  if (!overrides[division]) overrides[division] = {};
  overrides[division][agent] = !overrides[division][agent];
  writeState('agent-overrides.json', overrides);

  const enabled = overrides[division][agent];
  jsonOk(res, { ok: true, division, agent, enabled });
}

// POST /api/agents/interval  { division, agent, hours }
function handleInterval(body, res) {
  const { division, agent, hours } = body;
  const h = parseInt(hours);
  if (!division || !agent) return jsonError(res, 400, 'division and agent required');
  if (isNaN(h) || h < 1 || h > 168) return jsonError(res, 400, 'hours must be 1–168');

  const overrides = readState('agent-overrides.json') || {};
  if (!overrides[division]) overrides[division] = {};
  overrides[division][agent + '_interval_hours'] = h;
  writeState('agent-overrides.json', overrides);

  jsonOk(res, { ok: true, division, agent, hours: h });
}

// ── Live context builder (zero API cost) ──
function buildContext() {
  const lines = [];

  try {
    const os = JSON.parse(fs.readFileSync(path.join(STATE_DIR, 'orchestrator-state.json'), 'utf8'));
    lines.push('[ DIVISIONS ]');
    const now = Date.now();
    const divMap = os.divisions || os;
    for (const [div, data] of Object.entries(divMap)) {
      if (!data || typeof data !== 'object') continue;
      const status  = data.status || 'idle';
      const lastRun = data.last_run ? Math.round((now - new Date(data.last_run).getTime()) / 60000) + 'm ago' : 'never';
      const enabled = data.enabled !== false ? 'ON' : 'OFF';
      lines.push(`  ${div}: ${status.toUpperCase()} | last_run: ${lastRun} | ${enabled}`);
    }
  } catch(e) { lines.push('[ DIVISIONS ] — unavailable'); }

  try {
    const apps = JSON.parse(fs.readFileSync(path.join(STATE_DIR, 'applications.json'), 'utf8'));
    const st = apps.stats || {};
    lines.push('\n[ OPPORTUNITY ]');
    lines.push(`  pending_review: ${st.pending_review || 0} | applied: ${st.applied || 0} | interviews: ${st.interviews || 0} | rejected: ${st.rejected || 0}`);
    const pending = (apps.pipeline || []).filter(j => j.status === 'pending_review').sort((a,b) => (b.score||0)-(a.score||0));
    if (pending.length > 0) {
      const top = pending[0];
      lines.push(`  top pending: "${top.title}" at ${top.company} | score: ${top.score} | pay: ${top.pay || 'n/a'}`);
    }
  } catch(e) { lines.push('\n[ OPPORTUNITY ] — unavailable'); }

  try {
    const log = JSON.parse(fs.readFileSync(path.join(STATE_DIR, 'activity-log.json'), 'utf8'));
    lines.push('\n[ RECENT ACTIVITY ]');
    const entries = (log.entries || []).slice(-5);
    entries.forEach(e => {
      const t = new Date(e.time);
      const hhmm = t.getHours().toString().padStart(2,'0') + ':' + t.getMinutes().toString().padStart(2,'0');
      lines.push(`  ${hhmm} ${e.division} — ${e.message}`);
    });
  } catch(e) { lines.push('\n[ RECENT ACTIVITY ] — unavailable'); }

  try {
    const stats = JSON.parse(fs.readFileSync(path.join(STATE_DIR, 'jclaw-stats.json'), 'utf8'));
    lines.push('\n[ J_CLAW STATUS ]');
    lines.push(`  Level: ${stats.level} | Rank: ${stats.rank}`);
    lines.push(`  XP: ${stats.base_xp} / ${stats.xp_to_next_level} | Total earned: ${stats.total_xp_earned}`);
    const divs = stats.divisions || {};
    for (const [d, ddata] of Object.entries(divs)) {
      lines.push(`  ${d}: ${ddata.rank} (${ddata.xp} XP)`);
    }
    if (stats.achievements && stats.achievements.length > 0) {
      lines.push(`  achievements: ${stats.achievements.map(a => (typeof a === 'string' ? a : a.id || a.name || JSON.stringify(a))).join(', ')}`);
    }
  } catch(e) { lines.push('\n[ J_CLAW STATUS ] — unavailable'); }

  try {
    const health = JSON.parse(fs.readFileSync(path.join(STATE_DIR, 'health-log.json'), 'utf8'));
    const entries = health.entries || health.logs || [];
    lines.push('\n[ HEALTH ]');
    if (entries.length > 0) {
      const last = entries[entries.length - 1];
      lines.push(`  last log: ${last.date || last.logged_at || 'unknown'}`);
      if (last.sleep_hours)   lines.push(`  sleep: ${last.sleep_hours}h | quality: ${last.sleep_quality || 'n/a'}`);
      if (last.adderall_dose) lines.push(`  adderall: ${last.adderall_dose}mg at ${last.adderall_time || 'n/a'}`);
    } else {
      lines.push('  no health logs recorded yet');
    }
  } catch(e) { lines.push('\n[ HEALTH ] — no data'); }

  try {
    lines.push('\n[ TRADING ]');
    // Market scan packet
    try {
      const ms = JSON.parse(fs.readFileSync(path.join(ROOT, 'divisions', 'trading', 'packets', 'market-scan.json'), 'utf8'));
      if (ms.summary) lines.push(`  market: ${ms.summary}`);
      if (ms.metrics) {
        const { signals, high } = ms.metrics;
        if (signals != null) lines.push(`  signals: ${signals} | high priority: ${high || 0}`);
      }
    } catch(e) {}
    // Trading report packet
    try {
      const tr = JSON.parse(fs.readFileSync(path.join(ROOT, 'divisions', 'trading', 'packets', 'trading-report.json'), 'utf8'));
      if (tr.metrics) {
        const m = tr.metrics;
        if (m.active_strategy) lines.push(`  strategy: ${m.active_strategy}`);
        if (m.cycle_number != null) lines.push(`  cycle: ${m.cycle_number} | win_rate: ${m.strategy_win_rate_pct != null ? m.strategy_win_rate_pct + '%' : 'n/a'} | sharpe: ${m.strategy_sharpe || 'n/a'}`);
        if (m.total_trades > 0) lines.push(`  trades today: ${m.total_trades} | wins: ${m.wins} | losses: ${m.losses} | P&L: ${m.total_pnl || 'n/a'}`);
        else lines.push(`  trades today: none`);
      }
    } catch(e) {}
    // Fallback to trade-log.json if it exists
    try {
      const trades = JSON.parse(fs.readFileSync(path.join(STATE_DIR, 'trade-log.json'), 'utf8'));
      const sessions = trades.sessions || trades.entries || [];
      if (sessions.length > 0) {
        const last = sessions[sessions.length - 1];
        lines.push(`  last session: ${last.date || last.time || 'unknown'}`);
        if (last.pnl !== undefined) lines.push(`  session P&L: ${last.pnl}`);
      }
    } catch(e) {}
  } catch(e) { lines.push('\n[ TRADING ] — no data'); }

  return lines.join('\n');
}

// POST /api/chat  { message }
function stripSoulForChat(soul) {
  const sectionsToStrip = ['Memory Checkpointing', 'Git Commit Directives',
    'Rank Reference', 'Communication Style', 'Daily Schedule', 'SOUL.md Sync Requirement'];
  let result = soul.replace(/\r\n/g, '\n');
  for (const section of sectionsToStrip) {
    const re = new RegExp('## ' + section + '[\\s\\S]*?(?=\\n## |$)', 'g');
    result = result.replace(re, '');
  }
  return result.replace(/\n{3,}/g, '\n\n').trim();
}

// ── Operator command parser (no LLM needed) ──
function handleCommand(cmd) {
  const parts = cmd.trim().split(/\s+/);
  const command = parts[0].toLowerCase();

  if (command === '/status') {
    const os = readState('orchestrator-state.json') || { divisions: {} };
    const qf = readState('task-queue.json') || { tasks: [] };
    const queued = (qf.tasks || []).filter(t => t.status === 'queued').length;
    const running = (qf.tasks || []).filter(t => t.status === 'running').length;
    return { type: 'command', command: '/status', data: { divisions: os.divisions, task_queue: { queued, running } } };
  }

  if (command === '/approvals') {
    const af = readState('approval-queue.json') || { approvals: [] };
    const pending = (af.approvals || []).filter(a => a.status === 'pending');
    return { type: 'command', command: '/approvals', data: { pending, count: pending.length } };
  }

  if (command === '/approve' && parts[1]) {
    const approvalId = parts[1];
    const af = readState('approval-queue.json') || { approvals: [] };
    const a = (af.approvals || []).find(x => x.id === approvalId && x.status === 'pending');
    if (!a) return { type: 'command', command: '/approve', error: 'approval not found' };
    a.status = 'approved'; a.resolved_at = new Date().toISOString(); a.resolved_by = 'tyler';
    writeState('approval-queue.json', af);
    logActivity('SYS', `Chat command: approved ${approvalId}`, 'green');
    return { type: 'command', command: '/approve', data: { approved: approvalId } };
  }

  if (command === '/reject' && parts[1]) {
    const approvalId = parts[1];
    const af = readState('approval-queue.json') || { approvals: [] };
    const a = (af.approvals || []).find(x => x.id === approvalId && x.status === 'pending');
    if (!a) return { type: 'command', command: '/reject', error: 'approval not found' };
    a.status = 'rejected'; a.resolved_at = new Date().toISOString(); a.resolved_by = 'tyler';
    writeState('approval-queue.json', af);
    return { type: 'command', command: '/reject', data: { rejected: approvalId } };
  }

  if (command === '/logs') {
    try {
      const auditFile = path.join(ROOT, 'logs', 'audit.jsonl');
      if (!fs.existsSync(auditFile)) return { type: 'command', command: '/logs', data: { entries: [] } };
      const lines = fs.readFileSync(auditFile, 'utf8').trim().split('\n').filter(Boolean);
      const entries = lines.slice(-20).map(l => { try { return JSON.parse(l); } catch { return null; }}).filter(Boolean);
      return { type: 'command', command: '/logs', data: { entries } };
    } catch(e) { return { type: 'command', command: '/logs', error: e.message }; }
  }

  if (command === '/sentinel') {
    try {
      const sp = path.join(ROOT, 'divisions', 'sentinel', 'packets', 'provider-health.json');
      if (fs.existsSync(sp)) return { type: 'command', command: '/sentinel', data: JSON.parse(fs.readFileSync(sp, 'utf8')) };
    } catch(e) {}
    return { type: 'command', command: '/sentinel', data: { message: 'No sentinel data — run: sentinel provider-health' } };
  }

  if (command === '/divisions') {
    const os = readState('orchestrator-state.json') || { divisions: {} };
    return { type: 'command', command: '/divisions', data: os.divisions || {} };
  }

  if (command === '/tasks') {
    const qf = readState('task-queue.json') || { tasks: [] };
    return { type: 'command', command: '/tasks', data: { tasks: (qf.tasks || []).slice(-20) } };
  }

  return { type: 'command', command, error: `Unknown command: ${command}. Try /status /approvals /logs /sentinel /divisions /tasks` };
}

async function handleChat(body, res) {
  const message = (body.message || '').trim();
  if (!message) return jsonError(res, 400, 'message required');

  // ── Command handling (deterministic, no LLM) ──
  if (message.startsWith('/')) {
    const result = handleCommand(message);
    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      'Access-Control-Allow-Origin': '*',
      'Connection': 'keep-alive',
    });
    const text = result.error
      ? `Command error: ${result.error}`
      : `\`\`\`json\n${JSON.stringify(result.data, null, 2)}\n\`\`\``;
    const delta = { type: 'content_block_delta', delta: { type: 'text_delta', text } };
    res.write(`data: ${JSON.stringify(delta)}\n\n`);
    res.end();
    return;
  }

  let soul = '';
  try { soul = fs.readFileSync(path.join(ROOT, 'SOUL.md'), 'utf8'); } catch(e) {}
  soul = stripSoulForChat(soul);

  const context = buildContext();
  const systemPrompt = soul + '\n\nIMPORTANT — User Context: There are two users of J_Claw. Tyler is the partner and owner of this local environment — he is the primary operator on the PC desktop and the user you are speaking with now. Matthew is the creator of J_Claw and accesses the system from mobile. Both have full trust in the system.\n\n---\n\n' + context;

  const hist = readState('chat-history.json') || { messages: [], last_updated: null };
  let history = (hist.messages || []).slice(-20);
  if (history.length > 0 && history[0].role !== 'user') history = history.slice(1);

  let conversationText = '';
  history.forEach(m => {
    conversationText += (m.role === 'user' ? 'Tyler: ' : 'J_Claw: ') + m.content + '\n\n';
  });
  conversationText += 'Tyler: ' + message;

  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Access-Control-Allow-Origin': '*',
    'Connection': 'keep-alive',
  });

  // ── Try Ollama first (local, free, fast) ──────────────────────────────────
  const ollamaHost = process.env.OLLAMA_HOST || 'http://localhost:11434';
  const chatModel  = process.env.MODEL_7B || 'qwen2.5:7b-instruct-q4_K_M';
  const ollamaMessages = [
    { role: 'system', content: systemPrompt },
    ...history.map(m => ({ role: m.role, content: m.content })),
    { role: 'user', content: message },
  ];

  let ollamaSucceeded = false;
  try {
    await new Promise((resolve, reject) => {
      const ollamaUrl = new url.URL(ollamaHost + '/api/chat');
      const lib = ollamaUrl.protocol === 'https:' ? https : http;
      const body = JSON.stringify({ model: chatModel, messages: ollamaMessages, stream: true });
      const req = lib.request({
        hostname: ollamaUrl.hostname, port: ollamaUrl.port || (ollamaUrl.protocol === 'https:' ? 443 : 80),
        path: ollamaUrl.pathname, method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
      }, (ollamaRes) => {
        if (ollamaRes.statusCode !== 200) { reject(new Error(`Ollama HTTP ${ollamaRes.statusCode}`)); return; }
        ollamaSucceeded = true;
        let fullOllamaResponse = '';
        let buf = '';
        ollamaRes.on('data', chunk => {
          buf += chunk.toString();
          const lines = buf.split('\n');
          buf = lines.pop();
          for (const line of lines) {
            if (!line.trim()) continue;
            try {
              const evt = JSON.parse(line);
              const text = evt.message && evt.message.content;
              if (text) {
                fullOllamaResponse += text;
                const delta = { type: 'content_block_delta', delta: { type: 'text_delta', text } };
                res.write(`data: ${JSON.stringify(delta)}\n\n`);
              }
            } catch(e) {}
          }
        });
        ollamaRes.on('end', () => {
          if (fullOllamaResponse) {
            try {
              const hist2 = readState('chat-history.json') || { messages: [], last_updated: null };
              hist2.messages.push({ role: 'user', content: message });
              hist2.messages.push({ role: 'assistant', content: fullOllamaResponse });
              if (hist2.messages.length > 100) hist2.messages = hist2.messages.slice(-100);
              hist2.last_updated = new Date().toISOString();
              writeState('chat-history.json', hist2);
            } catch(e) {}
          }
          resolve();
        });
        ollamaRes.on('error', reject);
      });
      req.on('error', reject);
      req.setTimeout(60000, () => { req.destroy(); reject(new Error('Ollama timeout')); });
      req.write(body);
      req.end();
    });
  } catch(ollamaErr) {
    logActivity('SYS', `Ollama chat failed, falling back to Claude CLI: ${ollamaErr.message}`, 'yellow');
  }

  if (ollamaSucceeded) { res.end(); return; }

  // ── Fallback: Claude CLI ──────────────────────────────────────────────────

  // Sanitize to ASCII-safe — SOUL.md may contain Unicode that breaks CP1252 stdin on Windows
  const sanitize = s => s.replace(/[^\x00-\x7F]/g, c => {
    const map = { '\u2190':'<-','\u2192':'->','\u2014':'--','\u2013':'-','\u2018':"'",'\u2019':"'",'\u201c':'"','\u201d':'"','\u2022':'*','\u2026':'...' };
    return map[c] || '';
  });
  const safePrompt = sanitize(systemPrompt);
  const safeConversation = sanitize(conversationText);

  const model = process.env.CLAUDE_MODEL || 'claude-sonnet-4-6';
  const claudeCli = 'C:\\Users\\Tyler\\AppData\\Roaming\\npm\\node_modules\\@anthropic-ai\\claude-code\\cli.js';

  // Strip ANTHROPIC_API_KEY from child env — the OAuth token confuses the CLI;
  // let it use its own ~/.claude/ session instead.
  const childEnv = { ...process.env };
  delete childEnv.ANTHROPIC_API_KEY;
  childEnv.PATH = (childEnv.PATH || '') + ';C:\\Users\\Tyler\\AppData\\Roaming\\npm';

  const claudeArgs = [
    claudeCli,
    '--print',
    '--system-prompt', safePrompt,
    '--model', model,
    '--output-format', 'stream-json',
    '--verbose',
  ];

  const debugLog = path.join(ROOT, 'logs', 'chat-debug.log');
  const debugEntry = `\n[${new Date().toISOString()}] spawn start\n`;
  try { fs.appendFileSync(debugLog, debugEntry); } catch(e) {}

  const claude = spawn(process.execPath, claudeArgs, {
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
    env: childEnv,
  });

  let fullResponse = '';
  let stdoutBuf = '';
  let stderrBuf = '';

  claude.stdin.write(safeConversation, 'utf8');
  claude.stdin.end();

  claude.stdout.on('data', chunk => {
    const s = chunk.toString();
    stdoutBuf += s;
    try { fs.appendFileSync(debugLog, `[stdout] ${s}`); } catch(e) {}
    const lines = stdoutBuf.split('\n');
    stdoutBuf = lines.pop();
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const evt = JSON.parse(trimmed);
        // streaming delta format
        if (evt.type === 'stream_event' && evt.event) {
          const e = evt.event;
          if (e.type === 'content_block_delta' && e.delta && e.delta.type === 'text_delta' && e.delta.text) {
            fullResponse += e.delta.text;
            const delta = { type: 'content_block_delta', delta: { type: 'text_delta', text: e.delta.text } };
            res.write(`data: ${JSON.stringify(delta)}\n\n`);
          }
        }
        // verbose format — full assistant message in one object
        if (evt.type === 'assistant' && evt.message && evt.message.content) {
          for (const block of evt.message.content) {
            if (block.type === 'text' && block.text) {
              const delta = { type: 'content_block_delta', delta: { type: 'text_delta', text: block.text } };
              res.write(`data: ${JSON.stringify(delta)}\n\n`);
            }
          }
        }
        if (evt.type === 'result' && evt.result) fullResponse = evt.result;
      } catch(e) {}
    }
  });

  claude.stderr.on('data', chunk => {
    const s = chunk.toString();
    stderrBuf += s;
    try { fs.appendFileSync(debugLog, `[stderr] ${s}`); } catch(e) {}
  });

  claude.on('close', code => {
    try { fs.appendFileSync(debugLog, `[close] exit=${code} fullResponse=${fullResponse.length}b stderr=${stderrBuf.slice(0,300)}\n`); } catch(e) {}
    if (fullResponse) {
      try {
        const hist2 = readState('chat-history.json') || { messages: [], last_updated: null };
        hist2.messages.push({ role: 'user', content: message });
        hist2.messages.push({ role: 'assistant', content: fullResponse });
        if (hist2.messages.length > 100) hist2.messages = hist2.messages.slice(-100);
        hist2.last_updated = new Date().toISOString();
        writeState('chat-history.json', hist2);
      } catch(e) {}
    }
    if (code !== 0 && !fullResponse) {
      const errDetail = stderrBuf.slice(0, 300) || `exit ${code}`;
      const errEvt = { type: 'content_block_delta', delta: { type: 'text_delta', text: `ERROR — J_Claw: ${errDetail}` } };
      res.write(`data: ${JSON.stringify(errEvt)}\n\n`);
    } else if (!fullResponse) {
      const errEvt = { type: 'content_block_delta', delta: { type: 'text_delta', text: 'ERROR — J_Claw returned no output (exit 0). Check logs/chat-debug.log' } };
      res.write(`data: ${JSON.stringify(errEvt)}\n\n`);
    }
    res.end();
  });

  claude.on('error', err => {
    try { fs.appendFileSync(debugLog, `[error] ${err.message}\n`); } catch(e) {}
    const errEvt = { type: 'content_block_delta', delta: { type: 'text_delta', text: 'ERROR — spawn: ' + err.message } };
    res.write(`data: ${JSON.stringify(errEvt)}\n\n`);
    res.end();
  });
}

// POST /api/chat/clear
function handleChatClear(res) {
  writeState('chat-history.json', { messages: [], last_updated: null });
  jsonOk(res, { ok: true });
}

// ── Mobile Chat: J_Claw mode (Ollama → Groq fallback) ────────────────────────
async function handleMobileChatJClaw(body, res) {
  const message = (body.message || '').trim();
  if (!message) return jsonError(res, 400, 'message required');

  let soul = '';
  try { soul = fs.readFileSync(path.join(ROOT, 'SOUL.md'), 'utf8'); } catch(e) {}
  soul = stripSoulForChat(soul);
  const context = buildContext();
  const systemPrompt = soul + '\n\nIMPORTANT — User Context: There are two users of J_Claw. Matthew is the creator of J_Claw and the user you are speaking with now — he is accessing from mobile. Tyler is Matthew\'s partner and the owner of this local environment; Tyler operates J_Claw from the PC desktop. Both have full trust in the system.\n\n---\n\n' + context;

  const hist = readState('chat-history.json') || { messages: [], last_updated: null };
  let history = (hist.messages || []).slice(-20);
  if (history.length > 0 && history[0].role !== 'user') history = history.slice(1);

  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Access-Control-Allow-Origin': '*',
    'Connection': 'keep-alive',
  });

  const ollamaHost = process.env.OLLAMA_HOST || 'http://localhost:11434';
  const chatModel  = process.env.MODEL_7B || 'qwen2.5:7b-instruct-q4_K_M';
  const ollamaMessages = [
    { role: 'system', content: systemPrompt },
    ...history.map(m => ({ role: m.role, content: m.content })),
    { role: 'user', content: message },
  ];

  try {
    await new Promise((resolve, reject) => {
      const ollamaUrl = new url.URL(ollamaHost + '/api/chat');
      const lib = ollamaUrl.protocol === 'https:' ? https : http;
      const reqBody = JSON.stringify({ model: chatModel, messages: ollamaMessages, stream: true });
      const req = lib.request({
        hostname: ollamaUrl.hostname, port: ollamaUrl.port || 80,
        path: ollamaUrl.pathname, method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(reqBody) },
      }, (ollamaRes) => {
        if (ollamaRes.statusCode !== 200) { reject(new Error(`Ollama HTTP ${ollamaRes.statusCode}`)); return; }
        let fullOllamaResponse = '';
        let buf = '';
        ollamaRes.on('data', chunk => {
          buf += chunk.toString();
          const lines = buf.split('\n');
          buf = lines.pop();
          for (const line of lines) {
            if (!line.trim()) continue;
            try {
              const evt = JSON.parse(line);
              const text = evt.message && evt.message.content;
              if (text) {
                fullOllamaResponse += text;
                res.write(`data: ${JSON.stringify({ type: 'content_block_delta', delta: { type: 'text_delta', text } })}\n\n`);
              }
            } catch(e) {}
          }
        });
        ollamaRes.on('end', () => {
          if (fullOllamaResponse) {
            try {
              const hist2 = readState('chat-history.json') || { messages: [], last_updated: null };
              hist2.messages.push({ role: 'user', content: message });
              hist2.messages.push({ role: 'assistant', content: fullOllamaResponse });
              if (hist2.messages.length > 100) hist2.messages = hist2.messages.slice(-100);
              hist2.last_updated = new Date().toISOString();
              writeState('chat-history.json', hist2);
            } catch(e) {}
          }
          resolve();
        });
        ollamaRes.on('error', reject);
      });
      req.on('error', reject);
      req.setTimeout(60000, () => { req.destroy(); reject(new Error('timeout')); });
      req.write(reqBody);
      req.end();
    });
  } catch(e) {
    // ── Fallback: Ollama failed → try Groq (Llama 3.3 70B) ──────────────────
    const groqKey   = process.env.GROQ_API_KEY;
    const groqModel = process.env.GROQ_MODEL || 'llama-3.3-70b-versatile';

    if (!groqKey) {
      res.write(`data: ${JSON.stringify({ type: 'content_block_delta', delta: { type: 'text_delta', text: `J_Claw (local AI) is offline: ${e.message}` } })}\n\n`);
    } else {
      // Banner so user knows we fell back
      res.write(`data: ${JSON.stringify({ type: 'content_block_delta', delta: { type: 'text_delta', text: `⚡ *Local AI offline — using cloud AI (Groq)*\n\n` } })}\n\n`);

      try {
        await new Promise((resolve, reject) => {
          const groqBody = JSON.stringify({
            model: groqModel,
            messages: ollamaMessages.map(m => ({ role: m.role, content: m.content })),
            stream: true,
            max_tokens: 2048,
          });
          const req = https.request({
            hostname: 'api.groq.com',
            port: 443,
            path: '/openai/v1/chat/completions',
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'Authorization': `Bearer ${groqKey}`,
              'Content-Length': Buffer.byteLength(groqBody),
            },
          }, (groqRes) => {
            if (groqRes.statusCode !== 200) { reject(new Error(`Groq HTTP ${groqRes.statusCode}`)); return; }
            let fullGroqResponse = '';
            let buf = '';
            groqRes.on('data', chunk => {
              buf += chunk.toString();
              const lines = buf.split('\n');
              buf = lines.pop();
              for (const line of lines) {
                const trimmed = line.replace(/^data: /, '').trim();
                if (!trimmed || trimmed === '[DONE]') continue;
                try {
                  const evt = JSON.parse(trimmed);
                  const text = evt.choices && evt.choices[0] && evt.choices[0].delta && evt.choices[0].delta.content;
                  if (text) {
                    fullGroqResponse += text;
                    res.write(`data: ${JSON.stringify({ type: 'content_block_delta', delta: { type: 'text_delta', text } })}\n\n`);
                  }
                } catch(pe) {}
              }
            });
            groqRes.on('end', () => {
              if (fullGroqResponse) {
                try {
                  const hist2 = readState('chat-history.json') || { messages: [], last_updated: null };
                  hist2.messages.push({ role: 'user', content: message });
                  hist2.messages.push({ role: 'assistant', content: fullGroqResponse });
                  if (hist2.messages.length > 100) hist2.messages = hist2.messages.slice(-100);
                  hist2.last_updated = new Date().toISOString();
                  writeState('chat-history.json', hist2);
                } catch(he) {}
              }
              resolve();
            });
            groqRes.on('error', reject);
          });
          req.on('error', reject);
          req.setTimeout(60000, () => { req.destroy(); reject(new Error('Groq timeout')); });
          req.write(groqBody);
          req.end();
        });
      } catch(groqErr) {
        res.write(`data: ${JSON.stringify({ type: 'content_block_delta', delta: { type: 'text_delta', text: `\n\n❌ Both local AI and cloud fallback failed: ${groqErr.message}` } })}\n\n`);
      }
    }
  }
  res.end();
}

// ── Mobile Chat: Coding mode (Claude CLI only — no Ollama) ────────────────────
async function handleMobileChatCoding(body, res) {
  const message = (body.message || '').trim();
  if (!message) return jsonError(res, 400, 'message required');

  const context = buildContext();
  const systemPrompt = `You are Claude, an AI coding assistant helping manage and improve J_Claw — a personal AI orchestration system running on Windows 11. There are two users: Matthew is the creator of J_Claw and the user you are speaking with now, accessing from mobile. Tyler is Matthew's partner and the owner of this local environment; Tyler operates J_Claw from the PC desktop. Both have full trust in the system. You have full context about the system below. Help with code changes, debugging, planning, and answering questions. You CAN make real file edits using your Edit, Write, Read, Glob, and Grep tools. Be direct and concise.\n\n${context}`;

  const hist = readState('coding-history.json') || { messages: [], last_updated: null };
  let history = (hist.messages || []).slice(-20);
  if (history.length > 0 && history[0].role !== 'user') history = history.slice(1);

  let conversationText = '';
  history.forEach(m => {
    conversationText += (m.role === 'user' ? 'Matthew: ' : 'Claude: ') + m.content + '\n\n';
  });
  conversationText += 'Matthew: ' + message;

  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Access-Control-Allow-Origin': '*',
    'Connection': 'keep-alive',
  });

  const sanitize = s => s.replace(/[^\x00-\x7F]/g, c => {
    const map = { '\u2190':'<-','\u2192':'->','\u2014':'--','\u2013':'-','\u2018':"'",'\u2019':"'",'\u201c':'"','\u201d':'"','\u2022':'*','\u2026':'...' };
    return map[c] || '';
  });

  const model = process.env.CLAUDE_MODEL || 'claude-sonnet-4-6';
  const claudeCli = 'C:\\Users\\Tyler\\AppData\\Roaming\\npm\\node_modules\\@anthropic-ai\\claude-code\\cli.js';
  const childEnv = { ...process.env };
  delete childEnv.ANTHROPIC_API_KEY;
  childEnv.PATH = (childEnv.PATH || '') + ';C:\\Users\\Tyler\\AppData\\Roaming\\npm';

  // ── Git checkpoint: capture HEAD before session for rollback reference ──
  const sessionId = crypto.randomBytes(6).toString('hex');
  let preSessionHead = '';
  try {
    const headResult = spawnSync('git', ['rev-parse', 'HEAD'], { cwd: ROOT, encoding: 'utf8', timeout: 5000 });
    preSessionHead = (headResult.stdout || '').trim();
  } catch(e) {}

  // ── Spawn Claude in restricted agent mode (no Bash, no shell execution) ──
  const claudeArgs = [
    claudeCli,
    '--allowedTools', 'Edit,Read,Write,Glob,Grep',
    '--system-prompt', sanitize(systemPrompt),
    '--model', model,
    '--output-format', 'stream-json',
    '--verbose',
  ];
  const debugLog = path.join(ROOT, 'logs', 'chat-debug.log');
  try { fs.appendFileSync(debugLog, `\n[${new Date().toISOString()}] coding-mode spawn (agent, session=${sessionId})\n`); } catch(e) {}

  const claude = spawn(process.execPath, claudeArgs, {
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
    env: childEnv,
    cwd: ROOT,
  });
  let fullResponse = '', stdoutBuf = '', stderrBuf = '';

  claude.stdin.write(sanitize(conversationText), 'utf8');
  claude.stdin.end();

  claude.stdout.on('data', chunk => {
    const s = chunk.toString();
    stdoutBuf += s;
    try { fs.appendFileSync(debugLog, `[coding-stdout] ${s}`); } catch(e) {}
    const lines = stdoutBuf.split('\n');
    stdoutBuf = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const evt = JSON.parse(line);
        if (evt.type === 'stream_event' && evt.event?.type === 'content_block_delta' && evt.event.delta?.type === 'text_delta') {
          const text = evt.event.delta.text;
          fullResponse += text;
          res.write(`data: ${JSON.stringify({ type: 'content_block_delta', delta: { type: 'text_delta', text } })}\n\n`);
        }
        if (evt.type === 'assistant' && evt.message?.content) {
          for (const block of evt.message.content) {
            if (block.type === 'text' && block.text) {
              res.write(`data: ${JSON.stringify({ type: 'content_block_delta', delta: { type: 'text_delta', text: block.text } })}\n\n`);
            }
            if (block.type === 'tool_use' && block.name) {
              res.write(`data: ${JSON.stringify({ type: 'thinking', tool: block.name, input: block.input })}\n\n`);
            }
          }
        }
        if (evt.type === 'system' && evt.subtype === 'task_progress' && evt.last_tool_name) {
          res.write(`data: ${JSON.stringify({ type: 'thinking', tool: evt.last_tool_name })}\n\n`);
        }
        if (evt.type === 'result' && evt.result) fullResponse = evt.result;
      } catch(e) {}
    }
  });

  claude.stderr.on('data', chunk => { stderrBuf += chunk.toString(); });

  claude.on('close', code => {
    // ── Post-session: capture diff and audit ──
    try {
      const SENSITIVE_FILES = ['.env', 'SOUL.md', 'BOOT.md'];
      let diffStat = '', diffFull = '', filesChanged = 0, sensitiveHit = [];

      if (preSessionHead) {
        // Stage all changes including new files so diff captures everything Claude touched
        spawnSync('git', ['add', '-A'], { cwd: ROOT, timeout: 8000 });
        const statResult = spawnSync('git', ['diff', '--cached', preSessionHead, '--stat'], { cwd: ROOT, encoding: 'utf8', timeout: 15000 });
        const fullResult = spawnSync('git', ['diff', '--cached', preSessionHead, '--'], { cwd: ROOT, encoding: 'utf8', timeout: 20000 });
        diffStat = (statResult.stdout || '').slice(0, 3000);
        diffFull = fullResult.stdout || '';
        filesChanged = (diffFull.match(/^diff --git/gm) || []).length;
        sensitiveHit = SENSITIVE_FILES.filter(f => diffFull.includes(`b/${f}`) || diffFull.includes(`a/${f}`));
      }

      mobileAuditLog({
        action:          'coding_session',
        session_id:      sessionId,
        message_preview: message.slice(0, 100),
        files_changed:   filesChanged,
        diff_stat:       diffStat,
        sensitive_files: sensitiveHit,
        exit_code:       code,
      });

      // ── Approval gate: if files changed, require biometric confirmation to keep ──
      if (filesChanged > 0) {
        const autoRevertTimer = setTimeout(() => {
          if (_pendingCodingApprovals.has(sessionId)) {
            _pendingCodingApprovals.delete(sessionId);
            try {
              spawnSync('git', ['reset', '--hard', preSessionHead], { cwd: ROOT, timeout: 15000 });
              logActivity('OP_SEC', `[MOBILE] Session ${sessionId} auto-reverted — no approval within 120s`, 'yellow');
            } catch(e) {}
            _broadcastCodingEvent({ type: 'coding_resolved', session_id: sessionId, decision: 'auto_reverted' });
          }
        }, 120_000);
        _pendingCodingApprovals.set(sessionId, { preSessionHead, filesChanged, diffStat, timer: autoRevertTimer });
        _broadcastCodingEvent({
          type:         'coding_approval',
          session_id:   sessionId,
          files_changed: filesChanged,
          diff_stat:    diffStat.slice(0, 600),
        });
      }

      // ── Tripwire: sensitive file touched → write OP-Sec alert + SSE push ──
      if (sensitiveHit.length > 0) {
        try {
          const alertPkt = {
            skill:             'mobile-audit',
            status:            'alert',
            escalate:          true,
            escalation_reason: `MOBILE CODING SESSION touched sensitive file(s): ${sensitiveHit.join(', ')} — session ${sessionId}`,
            summary:           `Mobile session ${sessionId} modified ${sensitiveHit.join(', ')}. Review git diff immediately.`,
            generated_at:      new Date().toISOString(),
          };
          const pktPath = path.join(ROOT, 'divisions', 'op-sec', 'packets', 'mobile-audit.json');
          fs.writeFileSync(pktPath, JSON.stringify(alertPkt, null, 2));
        } catch(e) {}
        _broadcastAlertUpdate();
      }

      // MEDIUM alert: more than 15 files changed
      if (filesChanged > 15) {
        try {
          const warnPkt = {
            skill:             'mobile-audit',
            status:            'warning',
            escalate:          true,
            escalation_reason: `MOBILE CODING SESSION changed ${filesChanged} files in session ${sessionId} — unusually large change`,
            summary:           `Mobile session ${sessionId} changed ${filesChanged} files. Review diff: git diff ${preSessionHead}`,
            generated_at:      new Date().toISOString(),
          };
          const pktPath = path.join(ROOT, 'divisions', 'op-sec', 'packets', 'mobile-audit.json');
          if (!fs.existsSync(pktPath)) fs.writeFileSync(pktPath, JSON.stringify(warnPkt, null, 2));
        } catch(e) {}
        _broadcastAlertUpdate();
      }
    } catch(e) {}

    if (fullResponse) {
      try {
        const hist2 = readState('coding-history.json') || { messages: [], last_updated: null };
        hist2.messages.push({ role: 'user', content: message });
        hist2.messages.push({ role: 'assistant', content: fullResponse });
        if (hist2.messages.length > 100) hist2.messages = hist2.messages.slice(-100);
        hist2.last_updated = new Date().toISOString();
        writeState('coding-history.json', hist2);
      } catch(e) {}
    }
    if (!fullResponse) {
      const errText = code !== 0 ? `Claude CLI error (exit ${code}): ${stderrBuf.slice(0,200)}` : 'No response received.';
      res.write(`data: ${JSON.stringify({ type: 'content_block_delta', delta: { type: 'text_delta', text: errText } })}\n\n`);
    }
    res.end();
  });

  claude.on('error', err => {
    res.write(`data: ${JSON.stringify({ type: 'content_block_delta', delta: { type: 'text_delta', text: 'Spawn error: ' + err.message } })}\n\n`);
    res.end();
  });
}

// ── Response helpers ──
function jsonOk(res, data) {
  res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
  res.end(JSON.stringify(data));
}

function jsonError(res, code, msg) {
  res.writeHead(code, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
  res.end(JSON.stringify({ error: msg }));
}

// ══════════════════════════════════════════════════════════════════════════════
// ── MOBILE SECURITY LAYER ─────────────────────────────────────────────────────
// Auth, challenge/response, policy enforcement, and audit logging for the
// mobile extension. These functions are only called from /mobile/api/* routes.
// ══════════════════════════════════════════════════════════════════════════════

// Actions the mobile client is allowed to perform (Operator tier).
// Admin-only actions (edit_prompt, write_memory, modify_SOUL, etc.) are
// intentionally absent — they are never reachable from mobile.
const MOBILE_ALLOWED_ACTIONS = new Set([
  'approve_task',   // approve a pending task in approval-queue.json
  'reject_task',    // reject a pending task
  'ack_alert',      // acknowledge/dismiss an escalation alert
  'pause_trading',  // pause the trading cycle via zenith orchestrator
  'resume_trading', // resume the trading cycle
  'pause_division', // disable a division agent via agent-overrides.json
  'resume_division',// re-enable a division agent
  'restart_server', // graceful process.exit(0) — PM2 auto-restarts
  'restart_pm2',    // spawns a new PowerShell window running: pm2 restart openclaw
  'git_sync',       // stash → pull → stash pop → push in a visible PowerShell window
  'approve_coding', // keep file edits made by a mobile coding session
  'revert_coding',  // git reset --hard back to pre-session HEAD
]);

// In-memory challenge store: id → { action, expires, used }
// Challenges expire in 30 seconds and are single-use.
const _mobileChallenges = new Map();

function issueMobileChallenge(action) {
  const id      = crypto.randomBytes(16).toString('hex');
  const expires = Date.now() + 90_000;
  _mobileChallenges.set(id, { action, expires, used: false });
  // Clean up expired entries (avoid unbounded growth)
  for (const [k, v] of _mobileChallenges) {
    if (v.expires < Date.now()) _mobileChallenges.delete(k);
  }
  return { challenge_id: id, expires_in: 90, action };
}

function consumeMobileChallenge(id, action) {
  const ch = _mobileChallenges.get(id);
  if (!ch)       return { ok: false, reason: 'Unknown or expired challenge' };
  if (ch.used)   return { ok: false, reason: 'Challenge already used' };
  if (ch.expires < Date.now()) return { ok: false, reason: 'Challenge expired (90s window)' };
  if (ch.action !== action)    return { ok: false, reason: `Challenge is for "${ch.action}", not "${action}"` };
  ch.used = true;
  return { ok: true };
}

function mobileAuditLog(entry) {
  try {
    const logDir  = path.join(ROOT, 'logs');
    const logFile = path.join(logDir, 'mobile-audit.jsonl');
    if (!fs.existsSync(logDir)) fs.mkdirSync(logDir, { recursive: true });
    const line = JSON.stringify({
      timestamp:    new Date().toISOString(),
      device_class: 'mobile',
      ...entry,
    }) + '\n';
    fs.appendFileSync(logFile, line);
  } catch(e) {
    // Audit log write failure is non-fatal — log to stderr only
    process.stderr.write('[mobile-audit] write failed: ' + e.message + '\n');
  }
}

// ── Mobile action dispatcher ──────────────────────────────────────────────────
async function handleMobileAction(body, req, res) {
  const action     = (body.action || '').trim();
  const challengeId = (body.challenge_id || '').trim();
  const targetId   = (body.target_id || '').trim();
  const division   = (body.division  || '').trim();
  const clientIP   = req.socket.remoteAddress || 'unknown';

  // 1. Validate action is on the allowed list
  if (!MOBILE_ALLOWED_ACTIONS.has(action)) {
    mobileAuditLog({ action, actor: 'mobile-operator', target: targetId, result: 'denied', reason: 'action not on mobile allowlist', ip: clientIP });
    return jsonError(res, 403, `Action "${action}" is not permitted from mobile. Admin-level actions require desktop access.`);
  }

  // 2. Validate and consume the challenge (prevents replay attacks)
  const ch = consumeMobileChallenge(challengeId, action);
  if (!ch.ok) {
    mobileAuditLog({ action, actor: 'mobile-operator', target: targetId, result: 'denied', reason: ch.reason, ip: clientIP });
    return jsonError(res, 403, ch.reason);
  }

  // 3. Execute the action and audit the result
  try {
    let result;
    switch (action) {
      case 'approve_task':
        result = await mobileApproveTask(targetId, 'approve'); break;
      case 'reject_task':
        result = await mobileApproveTask(targetId, 'reject'); break;
      case 'ack_alert':
        result = mobileAckAlert(targetId, division); break;
      case 'pause_trading':
        result = await mobileTradingControl('stop'); break;
      case 'resume_trading':
        result = await mobileTradingControl('run'); break;
      case 'pause_division':
        result = mobileDivisionControl(division, false); break;
      case 'resume_division':
        result = mobileDivisionControl(division, true); break;
      case 'restart_server':
        result = mobileRestartServer(); break;
      case 'restart_pm2':
        result = mobileRestartPm2(); break;
      case 'git_sync':
        result = mobileGitSync(); break;
      case 'approve_coding':
        result = mobileApproveCoding(targetId); break;
      case 'revert_coding':
        result = mobileRevertCoding(targetId); break;
      default:
        result = { ok: false, message: 'Unhandled action' };
    }
    mobileAuditLog({ action, actor: 'mobile-operator', target: targetId || division, result: result.ok ? 'succeeded' : 'failed', reason: result.message || '', ip: clientIP });
    return jsonOk(res, result);
  } catch(e) {
    mobileAuditLog({ action, actor: 'mobile-operator', target: targetId, result: 'error', reason: e.message, ip: clientIP });
    return jsonError(res, 500, e.message);
  }
}

function mobileApproveTask(approvalId, decision) {
  return new Promise(resolve => {
    try {
      const af   = path.join(STATE_DIR, 'approval-queue.json');
      const data = fs.existsSync(af) ? JSON.parse(fs.readFileSync(af, 'utf8')) : { approvals: [] };
      const idx  = (data.approvals || []).findIndex(a => a.id === approvalId);
      if (idx === -1) return resolve({ ok: false, message: `Approval ${approvalId} not found` });
      data.approvals[idx].status      = decision === 'approve' ? 'approved' : 'rejected';
      data.approvals[idx].actioned_at = new Date().toISOString();
      data.approvals[idx].actioned_by = 'mobile-operator';
      fs.writeFileSync(af, JSON.stringify(data, null, 2));
      logActivity('SYS', `[MOBILE] Task ${approvalId} ${decision}d via mobile`, decision === 'approve' ? 'green' : 'red');
      resolve({ ok: true, message: `Task ${decision}d` });
    } catch(e) {
      resolve({ ok: false, message: e.message });
    }
  });
}

const DISMISSED_ALERTS_FILE = path.join(STATE_DIR, 'dismissed-alerts.json');

function _loadDismissedAlerts() {
  try {
    if (fs.existsSync(DISMISSED_ALERTS_FILE)) {
      return new Set(JSON.parse(fs.readFileSync(DISMISSED_ALERTS_FILE, 'utf8')));
    }
  } catch { /* ignore */ }
  return new Set();
}

function _saveDismissedAlerts(set) {
  try {
    fs.writeFileSync(DISMISSED_ALERTS_FILE, JSON.stringify([...set], null, 2));
  } catch { /* ignore */ }
}

function mobileAckAlert(alertId, division) {
  // Persist dismissed alert ID so it stays gone after page reload.
  const dismissed = _loadDismissedAlerts();
  dismissed.add(alertId);
  _saveDismissedAlerts(dismissed);
  logActivity(division || 'SYS', `[MOBILE] Alert ack'd: ${alertId}`, 'yellow');
  _broadcastAlertUpdate();
  return { ok: true, message: 'Alert acknowledged' };
}

function _broadcastCodingEvent(payload) {
  if (_mobileAlertSubscribers.size === 0) return;
  const msg = JSON.stringify(payload);
  for (const r of _mobileAlertSubscribers) {
    try { r.write(`data: ${msg}\n\n`); } catch(e) { _mobileAlertSubscribers.delete(r); }
  }
}

function mobileApproveCoding(sessionId) {
  const pending = _pendingCodingApprovals.get(sessionId);
  if (!pending) return { ok: false, message: 'Session not found or already resolved' };
  clearTimeout(pending.timer);
  _pendingCodingApprovals.delete(sessionId);
  // Commit the staged changes (staged by close handler's git add -A)
  try {
    spawnSync('git', ['commit', '-m', `mobile-session ${sessionId} [approved]`], { cwd: ROOT, timeout: 15000 });
  } catch(e) {}
  logActivity('OP_SEC', `[MOBILE] Coding session ${sessionId} approved — ${pending.filesChanged} file(s) committed`, 'green');
  mobileAuditLog({ action: 'coding_approved', session_id: sessionId, files_changed: pending.filesChanged });
  _broadcastCodingEvent({ type: 'coding_resolved', session_id: sessionId, decision: 'approved' });
  return { ok: true, message: `Changes approved — ${pending.filesChanged} file(s) committed` };
}

function mobileRevertCoding(sessionId) {
  const pending = _pendingCodingApprovals.get(sessionId);
  if (!pending) return { ok: false, message: 'Session not found or already resolved' };
  clearTimeout(pending.timer);
  _pendingCodingApprovals.delete(sessionId);
  try {
    spawnSync('git', ['reset', '--hard', pending.preSessionHead], { cwd: ROOT, timeout: 15000 });
    spawnSync('git', ['clean', '-fd'], { cwd: ROOT, timeout: 10000 });
    logActivity('OP_SEC', `[MOBILE] Coding session ${sessionId} reverted — reset to ${pending.preSessionHead.slice(0,7)}`, 'yellow');
    mobileAuditLog({ action: 'coding_reverted', session_id: sessionId, reverted_to: pending.preSessionHead });
  } catch(e) {
    logActivity('OP_SEC', `[MOBILE] Revert failed for ${sessionId}: ${e.message}`, 'red');
    return { ok: false, message: 'Revert failed: ' + e.message };
  }
  _broadcastCodingEvent({ type: 'coding_resolved', session_id: sessionId, decision: 'reverted' });
  return { ok: true, message: 'Changes reverted — filesystem restored to pre-session state' };
}

function mobileRestartServer() {
  logActivity('SYS', '[MOBILE] Server restart requested — exiting for PM2 auto-restart', 'yellow');
  // Delay exit to allow the HTTP response to flush to the client first
  setTimeout(() => { process.exit(0); }, 300);
  return { ok: true, message: 'Server restarting — reconnect in ~5 seconds' };
}

function mobileRestartPm2() {
  try {
    // Open a new visible PowerShell window on the desktop and run pm2 restart openclaw.
    // cmd /c start spawns an independent window; -NoExit keeps it open so Tyler can see output.
    const proc = spawn(
      'cmd.exe',
      ['/c', 'start', 'powershell.exe', '-NoExit', '-Command', 'pm2 restart openclaw'],
      { detached: true, stdio: 'ignore', windowsHide: false }
    );
    proc.unref();
    logActivity('SYS', '[MOBILE] PM2 restart triggered — PowerShell window opened on desktop', 'yellow');
    mobileAuditLog({ action: 'restart_pm2', actor: 'mobile-operator', result: 'succeeded' });
    return { ok: true, message: 'PowerShell window opened — running pm2 restart openclaw' };
  } catch(e) {
    return { ok: false, message: 'Failed to open PowerShell: ' + e.message };
  }
}

function mobileGitSync() {
  try {
    const repoPath = 'C:\\Users\\Tyler\\Desktop\\J_Claw_Reborn';
    const cmds = [
      `cd "${repoPath}"`,
      'git stash',
      'git pull origin master',
      'git stash pop',
      'git push origin master',
    ].join(' && ');
    const proc = spawn(
      'cmd.exe',
      ['/c', 'start', 'powershell.exe', '-NoExit', '-Command', cmds],
      { detached: true, stdio: 'ignore', windowsHide: false }
    );
    proc.unref();
    logActivity('SYS', '[MOBILE] Git sync triggered — PowerShell window opened on desktop', 'yellow');
    mobileAuditLog({ action: 'git_sync', actor: 'mobile-operator', result: 'succeeded' });
    return { ok: true, message: 'PowerShell window opened — running git stash → pull → push' };
  } catch(e) {
    return { ok: false, message: 'Failed to open PowerShell: ' + e.message };
  }
}

async function mobileTradingControl(cmd) {
  return new Promise(resolve => {
    const endpoint = cmd === 'stop' ? '/stop' : '/run';
    const payload  = cmd === 'run' ? JSON.stringify({ auto: true, cycles: 0 }) : null;
    const zenithHost = '127.0.0.1';
    const zenithPort = 8000;
    const options = {
      hostname: zenithHost,
      port:     zenithPort,
      path:     endpoint,
      method:   payload ? 'POST' : 'POST',
      headers:  payload ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) } : {},
    };
    const r = http.request(options, resp => {
      let body = '';
      resp.on('data', c => { body += c; });
      resp.on('end', () => {
        logActivity('TRADING', `[MOBILE] Trading ${cmd} command sent`, 'yellow');
        resolve({ ok: true, message: `Trading ${cmd} sent`, response: body.slice(0, 100) });
      });
    });
    r.on('error', e => resolve({ ok: false, message: e.message }));
    if (payload) r.write(payload);
    r.end();
  });
}

function mobileDivisionControl(division, enable) {
  try {
    if (!division) return { ok: false, message: 'division required' };
    const overridesPath = path.join(STATE_DIR, 'agent-overrides.json');
    const data = fs.existsSync(overridesPath) ? JSON.parse(fs.readFileSync(overridesPath, 'utf8')) : {};
    if (!data[division]) data[division] = {};
    // Disable/enable all agents in the division
    data[division]._mobile_paused = !enable;
    data[division]._mobile_actioned_at = new Date().toISOString();
    fs.writeFileSync(overridesPath, JSON.stringify(data, null, 2));
    logActivity(division.toUpperCase(), `[MOBILE] Division ${enable ? 'resumed' : 'paused'} via mobile`, enable ? 'green' : 'yellow');
    return { ok: true, message: `Division ${division} ${enable ? 'resumed' : 'paused'}` };
  } catch(e) {
    return { ok: false, message: e.message };
  }
}

// ── Mobile read endpoints ─────────────────────────────────────────────────────

function handleMobileOverview(res) {
  try {
    const orchState = readState('orchestrator-state.json') || {};
    const stats     = readState('jclaw-stats.json')        || {};
    const apps      = readState('applications.json')       || { applications: [] };
    const briefing  = readState('briefing.json')           || {};

    // Division health + packet metrics
    const divMetrics = _readDivisionMetrics();
    const divisions = {};
    const orchDivisions = orchState.divisions || orchState;
    for (const [key, val] of Object.entries(orchDivisions)) {
      if (typeof val === 'object' && val !== null && !Array.isArray(val)) {
        divisions[key] = {
          status:   val.status   || 'unknown',
          last_run: val.last_run || null,
          metrics:  divMetrics[key] || {},
        };
      }
    }

    // Active alerts from escalated packets
    const alerts = _collectMobileAlerts();

    // Approval count
    const af          = path.join(STATE_DIR, 'approval-queue.json');
    const approvalData = fs.existsSync(af) ? JSON.parse(fs.readFileSync(af, 'utf8')) : { approvals: [] };
    const pendingApprovals = (approvalData.approvals || []).filter(a => a.status === 'pending').length;

    // Pending jobs
    const pendingJobs = (apps.applications || []).filter(a => a.status === 'pending_review').length;

    // Sentinel health
    const sentinelPkt = path.join(ROOT, 'divisions', 'sentinel', 'packets', 'provider-health.json');
    let system = {};
    if (fs.existsSync(sentinelPkt)) {
      const s = JSON.parse(fs.readFileSync(sentinelPkt, 'utf8'));
      const providers = (s.metrics || {}).providers || {};
      for (const [k, v] of Object.entries(providers)) {
        system[k.replace('ollama:', 'ollama/')] = v.status;
      }
    }

    return jsonOk(res, {
      divisions,
      alerts:    alerts.slice(0, 5),
      stats:     { pending_jobs: pendingJobs, active_alerts: alerts.length, pending_approvals: pendingApprovals },
      approvals: pendingApprovals,
      system,
      briefing:  briefing.content ? briefing.content.slice(0, 400) : null,
    });
  } catch(e) {
    return jsonError(res, 500, e.message);
  }
}

function handleMobileAlerts(res) {
  try {
    return jsonOk(res, { alerts: _collectMobileAlerts() });
  } catch(e) {
    return jsonError(res, 500, e.message);
  }
}

function _broadcastAlertUpdate() {
  if (_mobileAlertSubscribers.size === 0) return;
  try {
    const payload = JSON.stringify({ type: 'update', alerts: _collectMobileAlerts() });
    for (const res of _mobileAlertSubscribers) {
      try { res.write(`data: ${payload}\n\n`); } catch(e) { _mobileAlertSubscribers.delete(res); }
    }
  } catch(e) {}
}

function handleMobileAlertStream(req, res, token) {
  res.writeHead(200, {
    'Content-Type':  'text/event-stream',
    'Cache-Control': 'no-cache',
    'Connection':    'keep-alive',
    'X-Accel-Buffering': 'no',
  });
  // Send initial state
  try {
    const init = JSON.stringify({ type: 'init', alerts: _collectMobileAlerts() });
    res.write(`data: ${init}\n\n`);
  } catch(e) {}
  // Heartbeat every 25s to prevent proxy timeouts
  const hb = setInterval(() => { try { res.write(': ping\n\n'); } catch(e) {} }, 25000);
  _mobileAlertSubscribers.add(res);
  req.on('close', () => {
    clearInterval(hb);
    _mobileAlertSubscribers.delete(res);
  });
}

function handleMobileDivisions(res) {
  try {
    const readPkt = (div, skill) => {
      try {
        const p = path.join(ROOT, 'divisions', div, 'packets', skill + '.json');
        if (!fs.existsSync(p)) return null;
        const d = JSON.parse(fs.readFileSync(p, 'utf8'));
        return { summary: d.summary, metrics: d.metrics, action_items: d.action_items, status: d.status, generated_at: d.generated_at };
      } catch(e) { return null; }
    };

    return jsonOk(res, {
      opportunity: {
        job_intake:     readPkt('opportunity', 'job-intake'),
        funding_finder: readPkt('opportunity', 'funding-finder'),
      },
      personal: {
        health_logger:   readPkt('personal', 'health-logger'),
        burnout_monitor: readPkt('personal', 'burnout-monitor'),
        perf_correlation: readPkt('personal', 'perf-correlation'),
      },
      dev_automation: {
        repo_monitor:  readPkt('dev-automation', 'repo-monitor'),
        refactor_scan: readPkt('dev-automation', 'refactor-scan'),
        doc_update:    readPkt('dev-automation', 'doc-update'),
        dev_digest:    readPkt('dev-automation', 'dev-digest'),
        artifact_manager: readPkt('dev-automation', 'artifact-manager'),
      },
      op_sec: {
        device_posture: readPkt('op-sec', 'device-posture'),
        threat_surface: readPkt('op-sec', 'threat-surface'),
        breach_check:   readPkt('op-sec', 'breach-check'),
        cred_audit:     readPkt('op-sec', 'cred-audit'),
        security_scan:  readPkt('op-sec', 'security-scan'),
        privacy_scan:   readPkt('op-sec', 'privacy-scan'),
        opsec_digest:   readPkt('op-sec', 'opsec-digest'),
      },
    });
  } catch(e) {
    return jsonError(res, 500, e.message);
  }
}

function _collectMobileAlerts() {
  const alerts = [];
  const dismissed = _loadDismissedAlerts();
  const packetDirs = ['op-sec', 'trading', 'opportunity', 'dev-automation', 'personal', 'sentinel'];
  for (const div of packetDirs) {
    const pktDir = path.join(ROOT, 'divisions', div, 'packets');
    if (!fs.existsSync(pktDir)) continue;
    for (const file of fs.readdirSync(pktDir)) {
      if (!file.endsWith('.json')) continue;
      try {
        const pkt = JSON.parse(fs.readFileSync(path.join(pktDir, file), 'utf8'));
        if (pkt.escalate && pkt.escalation_reason) {
          const id = pkt.skill || file.replace('.json', '');
          if (dismissed.has(id)) continue; // skip ack'd alerts
          alerts.push({
            id,
            division:         div,
            skill:            id,
            message:          pkt.escalation_reason,
            severity:         /\bHIGH\b/i.test(pkt.escalation_reason) ? 'HIGH' : /\bMEDIUM\b/i.test(pkt.escalation_reason) ? 'MEDIUM' : /\bLOW\b/i.test(pkt.escalation_reason) ? 'LOW' : 'HIGH',
            generated_at:     pkt.generated_at || null,
          });
        }
      } catch { /* skip malformed packets */ }
    }
  }
  return alerts;
}

// ── Division packet metrics reader ────────────────────────────────────────────
function _readDivisionMetrics() {
  const m = {};

  // Trading — reads agent-network cycle state
  try {
    const anState = 'C:/Users/Tyler/agent-network/state';
    const files = fs.readdirSync(anState)
      .filter(f => f.endsWith('_cycle_state.json'))
      .map(f => ({ f, mtime: fs.statSync(path.join(anState, f)).mtimeMs }))
      .sort((a, b) => b.mtime - a.mtime);
    if (files.length) {
      const cs = JSON.parse(fs.readFileSync(path.join(anState, files[0].f), 'utf8'));
      const s = cs.active_strategy || {};
      m.trading = {
        cycle_number:  cs.cycle_number,
        strategy_name: s.strategy_name || 'None',
        win_rate:      s.win_rate != null ? Math.round(s.win_rate * 100) : null,
        sharpe:        s.sharpe,
        rr_ratio:         s.rr_ratio,
        rr_display:       s.rr_display,
        max_drawdown_pct: s.max_drawdown_pct != null ? s.max_drawdown_pct : (s.max_drawdown != null ? Math.round(s.max_drawdown * 1000) / 10 : null),
      };
    }
  } catch(e) {}

  // Opportunity
  try {
    const ji = JSON.parse(fs.readFileSync(path.join(ROOT, 'divisions', 'opportunity', 'packets', 'job-intake.json'), 'utf8'));
    m.opportunity = {
      new_jobs_found:        ji.metrics?.new_jobs_found || 0,
      tier_a:                ji.metrics?.tier_a || 0,
      tier_b:                ji.metrics?.tier_b || 0,
      tier_c:                ji.metrics?.tier_c || 0,
      tier_d:                ji.metrics?.tier_d || 0,
      funding_opportunities: 0,
      sources_ok:            Object.values(ji.metrics?.source_status || {}).filter(v => v === 'ok').length,
      sources_total:         Object.keys(ji.metrics?.source_status || {}).length,
    };
    try {
      const ff = JSON.parse(fs.readFileSync(path.join(ROOT, 'divisions', 'opportunity', 'packets', 'funding-finder.json'), 'utf8'));
      m.opportunity.funding_opportunities = ff.metrics?.funding_opportunities || 0;
    } catch(e) {}
  } catch(e) {}

  // Personal
  try {
    const hl = JSON.parse(fs.readFileSync(path.join(ROOT, 'divisions', 'personal', 'packets', 'health-logger.json'), 'utf8'));
    const bm = JSON.parse(fs.readFileSync(path.join(ROOT, 'divisions', 'personal', 'packets', 'burnout-monitor.json'), 'utf8'));
    m.personal = {
      health_logged:   hl.metrics?.health_logged || false,
      sleep_hours:     hl.metrics?.sleep_hours   || null,
      sleep_quality:   hl.metrics?.sleep_quality || null,
      avg_sleep_hours: bm.metrics?.avg_sleep_hours || null,
      burnout_level:   (bm.summary || '').toLowerCase().includes('normal') ? 'normal' : 'check',
      health_entries:  bm.metrics?.health_entries || 0,
    };
  } catch(e) {}

  // Dev Automation
  try {
    const rs = JSON.parse(fs.readFileSync(path.join(ROOT, 'divisions', 'dev-automation', 'packets', 'refactor-scan.json'), 'utf8'));
    const rm = JSON.parse(fs.readFileSync(path.join(ROOT, 'divisions', 'dev-automation', 'packets', 'repo-monitor.json'), 'utf8'));
    const dd = JSON.parse(fs.readFileSync(path.join(ROOT, 'divisions', 'dev-automation', 'packets', 'dev-digest.json'), 'utf8'));
    m.dev_automation = {
      refactor_high:     rs.metrics?.high     || 0,
      files_scanned:     rs.metrics?.files_scanned || 0,
      repos_checked:     rm.metrics?.repos_checked || 0,
      repo_flags_high:   rm.metrics?.flags_high   || 0,
      repo_flags_medium: rm.metrics?.flags_medium || 0,
      total_high:        dd.metrics?.total_high || 0,
    };
  } catch(e) {}

  // OP-Sec
  try {
    const ts = JSON.parse(fs.readFileSync(path.join(ROOT, 'divisions', 'op-sec', 'packets', 'threat-surface.json'), 'utf8'));
    const dp = JSON.parse(fs.readFileSync(path.join(ROOT, 'divisions', 'op-sec', 'packets', 'device-posture.json'), 'utf8'));
    const bc = JSON.parse(fs.readFileSync(path.join(ROOT, 'divisions', 'op-sec', 'packets', 'breach-check.json'), 'utf8'));
    m.op_sec = {
      anomaly_count:  ts.metrics?.anomaly_count || 0,
      high_severity:  ts.metrics?.high_severity || 0,
      threat_level:   (ts.metrics?.high_severity || 0) > 0 ? 'warning' : 'ok',
      device_posture: dp.metrics?.severity || 'unknown',
      breach_status:  bc.status || 'unknown',
    };
  } catch(e) {}

  return m;
}

function handleMobileApprovals(res) {
  try {
    const af   = path.join(STATE_DIR, 'approval-queue.json');
    const data = fs.existsSync(af) ? JSON.parse(fs.readFileSync(af, 'utf8')) : { approvals: [] };
    const pending = (data.approvals || []).filter(a => a.status === 'pending');
    return jsonOk(res, { approvals: pending });
  } catch(e) {
    return jsonError(res, 500, e.message);
  }
}

function handleMobileTasks(req, res) {
  try {
    const qf   = path.join(STATE_DIR, 'task-queue.json');
    const data = fs.existsSync(qf) ? JSON.parse(fs.readFileSync(qf, 'utf8')) : { tasks: [] };
    const params = new url.URL('http://x' + req.url).searchParams;
    const status = params.get('status');
    let tasks = (data.tasks || []).slice(-50);
    if (status) tasks = tasks.filter(t => t.status === status);
    return jsonOk(res, { tasks: tasks.reverse() });
  } catch(e) {
    return jsonError(res, 500, e.message);
  }
}

function handleMobileTrading(res) {
  // Reuse the existing desktop trading handler — same data, mobile just
  // reads fewer fields. The handler already returns structured JSON.
  try {
    return handleGetTradingCycle(res);
  } catch(e) {
    return jsonError(res, 500, e.message);
  }
}

// ── Parse body ──
function parseBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', c => { data += c; if (data.length > 8192) reject(new Error('too large')); });
    req.on('end', () => { try { resolve(JSON.parse(data || '{}')); } catch { resolve({}); } });
    req.on('error', reject);
  });
}

// ── Static file server ──
function serveStatic(reqPath, res) {
  let filePath;
  if (reqPath === '/' || reqPath === '') {
    filePath = path.join(ROOT, 'dashboard', 'index.html');
  } else if (reqPath === '/dashboard' || reqPath === '/dashboard/') {
    filePath = path.join(ROOT, 'dashboard', 'index.html');
  } else {
    filePath = path.join(ROOT, reqPath.startsWith('/') ? reqPath.slice(1) : reqPath);
  }

  if (!filePath.startsWith(ROOT)) {
    res.writeHead(403); res.end('Forbidden'); return;
  }

  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('Not found: ' + reqPath); return;
    }
    const ext  = path.extname(filePath).toLowerCase();
    const mime = MIME[ext] || 'application/octet-stream';
    res.writeHead(200, { 'Content-Type': mime, 'Cache-Control': 'no-cache' });
    res.end(data);
  });
}

// ── Main HTTP server ──
const server = http.createServer(async (req, res) => {
  const parsed  = url.parse(req.url);
  const reqPath = parsed.pathname || '/';
  const method  = req.method.toUpperCase();

  if (method === 'OPTIONS') {
    res.writeHead(204, { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET,POST', 'Access-Control-Allow-Headers': 'Content-Type,Authorization' });
    res.end(); return;
  }

  if (reqPath.startsWith('/api/')) {
    try {
      if (method === 'POST' && reqPath === '/api/bestow') {
        const body = await parseBody(req); return handleBestow(body, res);
      }
      if (method === 'POST' && reqPath === '/api/control') {
        const body = await parseBody(req); return handleControl(body, res);
      }
      if (method === 'GET' && reqPath === '/api/gamif/stream') { return handleGamifStream(req, res); }
      if (method === 'GET' && reqPath === '/api/jobs') { return handleGetJobs(res); }
      if (method === 'GET' && reqPath === '/api/grants') { return handleGetGrants(res); }
      if (method === 'GET' && reqPath === '/api/packets') { return handleGetPackets(res); }
      if (method === 'GET' && reqPath === '/api/trading/cycle') { return handleGetTradingCycle(res); }
      if (method === 'GET' && reqPath === '/api/trading/cycle/status') { return proxyZenith('GET', '/status', null, res); }
      if (method === 'POST' && reqPath === '/api/trading/cycle/run') {
        const body = await parseBody(req); return proxyZenith('POST', '/run', body, res);
      }
      if (method === 'POST' && reqPath === '/api/trading/cycle/stop') { return proxyZenith('POST', '/stop', null, res); }
      if (method === 'POST' && reqPath.startsWith('/api/applications/') && reqPath.endsWith('/status')) {
        const parts = reqPath.split('/');
        const jobId = decodeURIComponent(parts[3]);
        const body  = await parseBody(req);
        return handleAppStatus(jobId, body, res);
      }
      if (method === 'POST' && reqPath.startsWith('/api/grants/') && reqPath.endsWith('/status')) {
        const parts   = reqPath.split('/');
        const grantId = decodeURIComponent(parts[3]);
        const body    = await parseBody(req);
        return handleGrantStatus(grantId, body, res);
      }
      if (method === 'POST' && reqPath === '/api/agents/toggle') {
        const body = await parseBody(req); return handleToggle(body, res);
      }
      if (method === 'POST' && reqPath === '/api/agents/interval') {
        const body = await parseBody(req); return handleInterval(body, res);
      }
      if (method === 'POST' && reqPath === '/api/chat') {
        const body = await parseBody(req); return handleChat(body, res);
      }
      if (method === 'POST' && reqPath === '/api/chat/clear') {
        return handleChatClear(res);
      }
      // ── Mission Control: Task Queue ──────────────────────────────────────
      if (method === 'GET' && reqPath === '/api/tasks') {
        try {
          const qf = path.join(STATE_DIR, 'task-queue.json');
          const data = fs.existsSync(qf) ? JSON.parse(fs.readFileSync(qf, 'utf8')) : { tasks: [] };
          const status = new url.URL('http://x' + req.url).searchParams.get('status');
          let tasks = data.tasks || [];
          if (status) tasks = tasks.filter(t => t.status === status);
          return jsonOk(res, { tasks: tasks.slice(-100) });
        } catch(e) { return jsonError(res, 500, e.message); }
      }
      if (method === 'GET' && reqPath.startsWith('/api/tasks/')) {
        const taskId = reqPath.split('/')[3];
        try {
          const qf = path.join(STATE_DIR, 'task-queue.json');
          const data = fs.existsSync(qf) ? JSON.parse(fs.readFileSync(qf, 'utf8')) : { tasks: [] };
          const task = (data.tasks || []).find(t => t.id === taskId);
          if (!task) return jsonError(res, 404, 'task not found');
          return jsonOk(res, task);
        } catch(e) { return jsonError(res, 500, e.message); }
      }
      if (method === 'POST' && reqPath === '/api/tasks') {
        const body = await parseBody(req);
        if (!body.type || !body.division) return jsonError(res, 400, 'type and division required');
        const task = {
          id: simpleId(),
          type: body.type,
          division: body.division,
          payload: body.payload || {},
          status: 'queued',
          submitted_at: new Date().toISOString(),
        };
        try {
          const qf = path.join(STATE_DIR, 'task-queue.json');
          const data = fs.existsSync(qf) ? JSON.parse(fs.readFileSync(qf, 'utf8')) : { tasks: [] };
          data.tasks.push(task);
          fs.writeFileSync(qf, JSON.stringify(data, null, 2));
          logActivity('SYS', `Task submitted: ${body.type} / ${body.division}`, 'blue');
          return jsonOk(res, { ok: true, task_id: task.id });
        } catch(e) { return jsonError(res, 500, e.message); }
      }
      // ── Mission Control: Approvals ────────────────────────────────────────
      if (method === 'GET' && reqPath === '/api/approvals') {
        try {
          const af = path.join(STATE_DIR, 'approval-queue.json');
          const data = fs.existsSync(af) ? JSON.parse(fs.readFileSync(af, 'utf8')) : { approvals: [] };
          const pending = (data.approvals || []).filter(a => a.status === 'pending');
          return jsonOk(res, { approvals: pending });
        } catch(e) { return jsonError(res, 500, e.message); }
      }
      if (method === 'POST' && reqPath.match(/^\/api\/approvals\/[^/]+$/)) {
        const approvalId = reqPath.split('/')[3];
        const body = await parseBody(req);
        const decision = body.decision;
        if (!['approve', 'reject', 'escalate'].includes(decision)) {
          return jsonError(res, 400, 'decision must be approve|reject|escalate');
        }
        try {
          const af = path.join(STATE_DIR, 'approval-queue.json');
          if (!fs.existsSync(af)) return jsonError(res, 404, 'approval-queue not found');
          const data = JSON.parse(fs.readFileSync(af, 'utf8'));
          const statusMap = { approve: 'approved', reject: 'rejected', escalate: 'escalated' };
          const a = (data.approvals || []).find(x => x.id === approvalId && x.status === 'pending');
          if (!a) return jsonError(res, 404, 'approval not found or already resolved');
          a.status = statusMap[decision];
          a.resolved_at = new Date().toISOString();
          a.resolved_by = 'matthew';
          fs.writeFileSync(af, JSON.stringify(data, null, 2));
          logActivity('SYS', `Approval ${approvalId}: ${decision}`, 'green');
          return jsonOk(res, { ok: true, approval_id: approvalId, status: a.status });
        } catch(e) { return jsonError(res, 500, e.message); }
      }
      // ── Sentinel Health ───────────────────────────────────────────────────
      if (method === 'GET' && reqPath === '/api/sentinel/health') {
        try {
          const sentinelPkt = path.join(__dirname, 'divisions', 'sentinel', 'packets', 'provider-health.json');
          if (fs.existsSync(sentinelPkt)) {
            return jsonOk(res, JSON.parse(fs.readFileSync(sentinelPkt, 'utf8')));
          }
          return jsonOk(res, { status: 'no_data', message: 'Run sentinel provider-health first' });
        } catch(e) { return jsonError(res, 500, e.message); }
      }
      // ── Audit Log ─────────────────────────────────────────────────────────
      if (method === 'GET' && reqPath === '/api/logs/audit') {
        try {
          const auditFile = path.join(__dirname, 'logs', 'audit.jsonl');
          if (!fs.existsSync(auditFile)) return jsonOk(res, { entries: [] });
          const lines = fs.readFileSync(auditFile, 'utf8').trim().split('\n').filter(Boolean);
          const entries = lines.slice(-100).map(l => { try { return JSON.parse(l); } catch { return null; }}).filter(Boolean);
          return jsonOk(res, { entries });
        } catch(e) { return jsonError(res, 500, e.message); }
      }
      if (method === 'GET' && reqPath === '/api/briefing') {
        const briefing = readState('briefing.json');
        return jsonOk(res, briefing || { content: null, last_generated: null });
      }
      if (method === 'POST' && reqPath === '/api/briefing/generate') {
        compileBriefing('manual');
        return jsonOk(res, { ok: true });
      }
      if (method === 'POST' && reqPath === '/api/briefing') {
        const body = await parseBody(req);
        const content = body.content || '';
        if (!content) return jsonError(res, 400, 'content required');
        writeState('briefing.json', { content, type: body.type || 'manual', last_generated: new Date().toISOString() });
        return jsonOk(res, { ok: true });
      }
      // Health check-in: POST /api/health-checkin { reply: "..." }
      if (method === 'GET' && reqPath === '/api/health-prompt') {
        const prompt = readState('health-prompt.json');
        return jsonOk(res, prompt || { active: false });
      }
      if (method === 'POST' && reqPath === '/api/health-checkin') {
        const body = await parseBody(req);
        const reply = (body.reply || '').trim();
        if (!reply) return jsonError(res, 400, 'reply required');
        // Dismiss the prompt
        writeState('health-prompt.json', { active: false, last_submitted: new Date().toISOString() });
        // Run health-logger with reply text as extra arg
        logActivity('PERSONAL', 'Health check-in received — running health-logger...', 'purple');
        runSkillViaPython('health-logger', 'PERSONAL', [reply]).then(ok => {
          if (ok) logActivity('PERSONAL', 'Health log saved successfully', 'green');
          else logActivity('PERSONAL', 'Health-logger failed — check logs', 'red');
        });
        return jsonOk(res, { ok: true, message: 'Health log queued' });
      }
      return jsonError(res, 404, 'unknown endpoint');
    } catch (e) {
      return jsonError(res, 500, e.message);
    }
  }

  // ══════════════════════════════════════════════════════════════════════════
  // ── MOBILE EXTENSION — /mobile/* routes ──────────────────────────────────
  //
  // Trust model: mobile is an UNTRUSTED thin client. Every /mobile/api/*
  // request must carry a valid Bearer token (MOBILE_TOKEN in .env).
  // Desktop (localhost) is bypassed — it never sends a token.
  //
  // Permission tiers:
  //   Observer  — read-only endpoints — token only
  //   Operator  — structured actions  — token + fresh 30s challenge
  //   Admin     — desktop-only, BLOCKED from mobile entirely
  //
  // Mobile CANNOT: edit_prompt, write_memory, modify_SOUL/BOOT, change_model,
  //   run_arbitrary_command, access /api/chat, or inject free text into agents.
  // ══════════════════════════════════════════════════════════════════════════
  if (reqPath === '/mobile' || reqPath === '/mobile/') {
    return serveStatic('/mobile/index.html', res);
  }
  if (reqPath === '/mobile/manifest.json') {
    return serveStatic('/mobile/manifest.json', res);
  }
  if (reqPath.startsWith('/mobile/icons/')) {
    return serveStatic(reqPath, res);
  }

  if (reqPath.startsWith('/mobile/api/')) {
    try {
      // ── Auth: require Bearer token for all /mobile/api/* ──────────────────
      // Desktop (localhost) is exempt since it never sends a token.
      const clientIP = req.socket.remoteAddress || '';
      const isLocalhost = clientIP === '::1' || clientIP === '127.0.0.1' || clientIP === '::ffff:127.0.0.1';
      const mobileToken = process.env.MOBILE_TOKEN || '';

      if (!isLocalhost) {
        if (!mobileToken) {
          return jsonError(res, 503, 'Mobile access not configured — set MOBILE_TOKEN in .env');
        }
        const authHeader = req.headers['authorization'] || '';
        const queryToken = new url.URL('http://x' + req.url).searchParams.get('token') || '';
        const provided   = authHeader.startsWith('Bearer ') ? authHeader.slice(7).trim() : queryToken;
        if (!provided || !crypto.timingSafeEqual(
          Buffer.from(provided.padEnd(64, '\0')),
          Buffer.from(mobileToken.padEnd(64, '\0'))
        )) {
          mobileAuditLog({ action: 'auth_failed', actor: 'unknown', result: 'denied', reason: 'invalid token', ip: clientIP });
          return jsonError(res, 401, 'Unauthorized');
        }
      }

      // ── OPTIONS preflight (mobile clients on HTTPS may send this) ─────────
      if (method === 'OPTIONS') {
        res.writeHead(204, {
          'Access-Control-Allow-Origin':  '*',
          'Access-Control-Allow-Methods': 'GET,POST',
          'Access-Control-Allow-Headers': 'Content-Type,Authorization',
        });
        return res.end();
      }

      // ── Observer endpoints (read-only, token sufficient) ──────────────────

      if (method === 'GET' && reqPath === '/mobile/api/overview') {
        return handleMobileOverview(res);
      }
      if (method === 'GET' && reqPath === '/mobile/api/status') {
        return jsonOk(res, { ok: true, uptime: Math.floor(process.uptime()) });
      }
      if (method === 'GET' && reqPath === '/mobile/api/stats') {
        const s = readState('jclaw-stats.json') || {};
        return jsonOk(res, s);
      }
      if (method === 'GET' && reqPath === '/mobile/api/alerts') {
        return handleMobileAlerts(res);
      }
      if (method === 'GET' && reqPath === '/mobile/api/alerts/stream') {
        return handleMobileAlertStream(req, res);
      }
      if (method === 'GET' && reqPath === '/mobile/api/gamif/stream') {
        return handleGamifStream(req, res);
      }
      if (method === 'GET' && reqPath === '/mobile/api/divisions') {
        return handleMobileDivisions(res);
      }
      if (method === 'POST' && reqPath === '/mobile/api/chat') {
        const body = await parseBody(req);
        return handleChat(body, res);  // same handler as desktop — Ollama→Claude CLI
      }
      if (method === 'POST' && reqPath === '/mobile/api/chat/jclaw') {
        const body = await parseBody(req);
        return handleMobileChatJClaw(body, res);
      }
      if (method === 'POST' && reqPath === '/mobile/api/chat/coding') {
        const body = await parseBody(req);
        return handleMobileChatCoding(body, res);
      }
      if (method === 'POST' && reqPath === '/mobile/api/chat/clear') {
        return handleChatClear(res);
      }
      if (method === 'GET' && reqPath === '/mobile/api/approvals') {
        return handleMobileApprovals(res);
      }
      if (method === 'GET' && reqPath === '/mobile/api/tasks') {
        return handleMobileTasks(req, res);
      }
      if (method === 'GET' && reqPath === '/mobile/api/trading') {
        return handleMobileTrading(res);
      }

      // ── PIN: server-side PIN storage (works over plain HTTP, no crypto.subtle needed) ──
      if (method === 'POST' && reqPath === '/mobile/api/pin/set') {
        const body = await parseBody(req);
        const hash = (body.hash || '').trim();
        if (!hash || hash.length < 8) return jsonError(res, 400, 'Invalid PIN hash');
        try {
          writeState('mobile-pin.json', { hash, updated: new Date().toISOString() });
          return jsonOk(res, { ok: true, message: 'PIN updated' });
        } catch(e) {
          return jsonError(res, 500, 'Failed to save PIN: ' + e.message);
        }
      }

      if (method === 'POST' && reqPath === '/mobile/api/pin/verify') {
        const body = await parseBody(req);
        const hash = (body.hash || '').trim();
        const stored = readState('mobile-pin.json');
        if (!stored || !stored.hash) return jsonError(res, 400, 'No PIN configured — set one in Settings');
        if (stored.hash === hash) return jsonOk(res, { ok: true });
        return jsonOk(res, { ok: false, reason: 'Incorrect PIN' });
      }

      // ── Challenge: issue a 90s single-use token for Operator actions ───────
      if (method === 'GET' && reqPath === '/mobile/api/challenge') {
        const params  = new url.URL('http://x' + req.url).searchParams;
        const action  = params.get('action') || '';
        if (!MOBILE_ALLOWED_ACTIONS.has(action)) {
          return jsonError(res, 400, `Action "${action}" not allowed from mobile`);
        }
        return jsonOk(res, issueMobileChallenge(action));
      }

      // ── Action: Operator-tier structured commands (token + challenge) ───────
      if (method === 'POST' && reqPath === '/mobile/api/action') {
        const body = await parseBody(req);
        return handleMobileAction(body, req, res);
      }

      return jsonError(res, 404, 'Unknown mobile endpoint');
    } catch(e) {
      return jsonError(res, 500, e.message);
    }
  }

  serveStatic(reqPath, res);
});

// ─────────────────────────────────────────────
// ── SKILL RUNNER (Python runtime) ──
// ─────────────────────────────────────────────
// Spawns run_division.py directly — no SKILL.md, no Claude subprocess.
// XP is granted by the Python skill itself via runtime/tools/xp.py — no double-grant here.
function runSkillViaPython(skillName, logDiv, extraArgs = []) {
  return new Promise(resolve => {
    const mapping = SKILL_TASK_MAP[skillName];
    if (!mapping) {
      logActivity(logDiv || 'SYS', `${skillName}: no task mapping defined`, 'red');
      return resolve(false);
    }

    updateDivisionState(mapping.divState, 'running');

    const runDivisionPath = path.join(ROOT, 'run_division.py');
    const proc = spawn(PYTHON_EXE, [runDivisionPath, mapping.division, mapping.task, ...extraArgs], {
      env: { ...process.env },
      windowsHide: true,
      cwd: ROOT,
    });

    let stderr = '';
    proc.stderr.on('data', d => { stderr += d.toString(); });
    proc.stdout.on('data', () => {}); // JSON packet written to disk — no need to capture

    proc.on('close', code => {
      updateDivisionState(mapping.divState, 'idle');
      if (code === 0) {
        logActivity(logDiv || 'SYS', `${skillName} complete`, 'green');
        handleGamifCheck(skillName, mapping.divState);
        resolve(true);
      } else {
        const errLine = stderr.split('\n').filter(l => l.includes('ERROR') || l.includes('FAILED')).pop()
          || `exit ${code}`;
        logActivity(logDiv || 'SYS', `${skillName} failed — ${errLine.trim()}`, 'red');
        resolve(false);
      }
    });

    proc.on('error', err => {
      updateDivisionState(mapping.divState, 'idle');
      logActivity(logDiv || 'SYS', `${skillName} spawn error: ${err.message}`, 'red');
      resolve(false);
    });
  });
}

// ─────────────────────────────────────────────
// ── JOB-INTAKE (Tier 1 — native Node.js fetch) ──
// ─────────────────────────────────────────────
// Fetches RSS/API sources directly — no Claude spawn, zero token cost.
// Calls hard-filter (Claude spawn) only when new jobs are found.
async function runJobIntakeNative() {
  logActivity('OPPS', 'job-intake starting...', 'blue');
  updateDivisionState('opportunity', 'running');

  const seen = readState('jobs-seen.json') || { jobs: [], last_run: null, total_seen: 0 };
  const seenIds = new Set((seen.jobs || []).map(j => j.id));
  const newJobs = [];

  // ── We Work Remotely (RSS) ──
  try {
    const xml = await httpGet('https://weworkremotely.com/remote-jobs.rss');
    const items = xml.match(/<item>([\s\S]*?)<\/item>/g) || [];
    for (const item of items) {
      const rawTitle = (item.match(/<title><!\[CDATA\[(.*?)\]\]><\/title>/) || item.match(/<title>(.*?)<\/title>/))?.[1]?.trim() || '';
      const link     = (item.match(/<link>(.*?)<\/link>/) || [])[1]?.trim() || '';
      if (!link || !rawTitle) continue;
      const id      = 'wwr-' + link;
      if (seenIds.has(id)) continue;
      seenIds.add(id);
      const colon   = rawTitle.indexOf(':');
      const company = colon > -1 ? rawTitle.slice(0, colon).trim() : '';
      const title   = colon > -1 ? rawTitle.slice(colon + 1).trim() : rawTitle;
      newJobs.push({ id, title, company, location: 'Remote', remote: true, pay_min: null, pay_max: null, pay_type: 'unspecified', description_summary: '', url: link, source: 'wwr', fetched_at: new Date().toISOString(), seen: false, filtered: false, tier: null, resume: null });
    }
  } catch(e) { logActivity('OPPS', `WWR fetch failed: ${e.message}`, 'red'); }

  // ── Remote OK (API) ──
  try {
    const raw = await httpGet('https://remoteok.com/api');
    const data = JSON.parse(raw);
    for (const job of (Array.isArray(data) ? data.slice(1) : [])) {
      if (!job.id) continue;
      const id = 'remoteok-' + job.id;
      if (seenIds.has(id)) continue;
      seenIds.add(id);
      newJobs.push({ id, title: job.position || '', company: job.company || '', location: 'Remote', remote: true, pay_min: job.salary_min || null, pay_max: job.salary_max || null, pay_type: job.salary_min ? 'salary' : 'unspecified', description_summary: '', url: job.url || '', source: 'remoteok', fetched_at: new Date().toISOString(), seen: false, filtered: false, tier: null, resume: null });
    }
  } catch(e) { logActivity('OPPS', `RemoteOK fetch failed: ${e.message}`, 'red'); }

  // ── Remotive (API) ──
  try {
    const raw = await httpGet('https://remotive.com/api/remote-jobs');
    const data = JSON.parse(raw);
    for (const job of (data.jobs || [])) {
      if (!job.id) continue;
      const id = 'remotive-' + job.id;
      if (seenIds.has(id)) continue;
      seenIds.add(id);
      newJobs.push({ id, title: job.title || '', company: job.company_name || '', location: job.candidate_required_location || 'Remote', remote: true, pay_min: null, pay_max: null, pay_type: job.salary ? 'salary' : 'unspecified', description_summary: job.salary || '', url: job.url || '', source: 'remotive', fetched_at: new Date().toISOString(), seen: false, filtered: false, tier: null, resume: null });
    }
  } catch(e) { logActivity('OPPS', `Remotive fetch failed: ${e.message}`, 'red'); }

  // ── Adzuna (API — US endpoint) ──
  const azId  = process.env.ADZUNA_APP_ID;
  const azKey = process.env.ADZUNA_APP_KEY;
  if (azId && azKey) {
    const queries = [
      `blockchain+OR+solidity+OR+web3+OR+defi+OR+AI+developer&where=remote&salary_min=60000&results_per_page=50`,
      `software+developer+OR+engineer+OR+technical+analyst&where=remote&salary_min=100000&results_per_page=50`,
      `telecom+sales+OR+customer+support+OR+technical+support&where=remote&salary_min=35000&results_per_page=20`,
    ];
    let azQuotaHit = false;
    for (const q of queries) {
      if (azQuotaHit) break;
      try {
        const raw = await httpGet(`https://api.adzuna.com/v1/api/jobs/us/search/1?app_id=${azId}&app_key=${azKey}&what=${q}&sort_by=date`);
        if (raw.trim().startsWith('<')) { logActivity('SYS', 'Adzuna returning HTML — quota issue', 'yellow'); azQuotaHit = true; break; }
        const data = JSON.parse(raw);
        for (const job of (data.results || [])) {
          const id = 'adzuna-' + job.id;
          if (seenIds.has(id)) continue;
          seenIds.add(id);
          newJobs.push({ id, title: job.title || '', company: (job.company || {}).display_name || '', location: (job.location || {}).display_name || '', remote: true, pay_min: job.salary_min || null, pay_max: job.salary_max || null, pay_type: job.salary_min ? 'salary' : 'unspecified', description_summary: '', url: job.redirect_url || '', source: 'adzuna', fetched_at: new Date().toISOString(), seen: false, filtered: false, tier: null, resume: null });
        }
      } catch(e) { logActivity('OPPS', `Adzuna query failed: ${e.message}`, 'red'); }
    }
  }

  // ── Update state ──
  seen.jobs = [...(seen.jobs || []), ...newJobs.map(j => ({ id: j.id }))];
  seen.last_run = new Date().toISOString();
  seen.total_seen = (seen.total_seen || 0) + newJobs.length;
  writeState('jobs-seen.json', seen);

  // ── Write new jobs to applications.json for dashboard display ──
  if (newJobs.length > 0) {
    const apps = readState('applications.json') || { pipeline: [], stats: { pending_review: 0, applied: 0, interviews: 0, rejected: 0 } };
    if (!apps.pipeline) apps.pipeline = [];
    if (!apps.stats) apps.stats = { pending_review: 0, applied: 0, interviews: 0, rejected: 0 };
    for (const job of newJobs) {
      apps.pipeline.push({ ...job, status: 'pending_review', score: null, added_at: new Date().toISOString() });
    }
    // Keep pipeline from growing unbounded — cap at 500 most recent
    if (apps.pipeline.length > 500) apps.pipeline = apps.pipeline.slice(-500);
    apps.stats.pending_review = apps.pipeline.filter(j => j.status === 'pending_review').length;
    apps.stats.applied        = apps.pipeline.filter(j => j.status === 'applied').length;
    apps.stats.interviews     = apps.pipeline.filter(j => j.status === 'interview').length;
    apps.stats.rejected       = apps.pipeline.filter(j => j.status === 'rejected' || j.status === 'skipped').length;
    writeState('applications.json', apps);
  }

  logActivity('OPPS', `job-intake complete — ${newJobs.length} new jobs found (${seen.total_seen} total seen)`, 'blue');
  updateDivisionState('opportunity', 'idle');
  grantDivisionXP('opportunity', 10, 'job-intake');
}

// ─────────────────────────────────────────────
// ── CONTROL QUEUE PROCESSOR ──
// ─────────────────────────────────────────────
// Checks control.json every 2 minutes inside server.js.
// No Claude spawn unless there's actually work in the queue.
// Previously this was a CronCreate cron firing every 10 minutes = 720 wasted Claude spawns/day.

let queueProcessing = false;

async function processControlQueue() {
  if (queueProcessing) return;
  const state = readState('control.json');
  if (!state || !state.queue) return;

  const queued = state.queue.filter(e => e.status === 'queued');
  if (queued.length === 0) return; // Nothing to do — no Claude spawn

  queueProcessing = true;
  try {
    for (const entry of queued) {
      entry.status = 'running';
    }
    writeState('control.json', state);

    const logDivMap = {
      'job-intake':       'OPPS',
      'hard-filter':      'OPPS',
      'funding-finder':   'OPPS',
      'trading-report':   'TRADING',
      'market-scan':      'TRADING',
      'health-logger':    'PERSONAL',
      'perf-correlation': 'PERSONAL',
      'burnout-monitor':  'PERSONAL',
      'personal-digest':  'PERSONAL',
      'repo-monitor':     'DEV',
      'debug-agent':      'DEV',
      'refactor-scan':    'DEV',
      'doc-update':       'DEV',
      'artifact-manager': 'DEV',
      'dev-digest':       'DEV',
      'device-posture':   'OP_SEC',
      'breach-check':     'OP_SEC',
      'threat-surface':   'OP_SEC',
      'cred-audit':       'OP_SEC',
      'privacy-scan':     'OP_SEC',
      'security-scan':    'OP_SEC',
      'opsec-digest':     'OP_SEC',
      'daily-briefing':   'SYS',
    };

    for (const entry of queued) {
      try {
        if (logDivMap[entry.skill] !== undefined) {
          const ok = await runSkillViaPython(entry.skill, logDivMap[entry.skill]);
          entry.status = ok ? 'completed' : 'failed';
        } else {
          logActivity('SYS', `Unknown skill in queue: ${entry.skill}`, 'red');
          entry.status = 'failed';
        }
      } catch(e) {
        entry.status = 'failed';
        logActivity('SYS', `Queue error for ${entry.skill}: ${e.message}`, 'red');
      }
    }

    state.last_processed = new Date().toISOString();
    writeState('control.json', state);
  } finally {
    queueProcessing = false;
  }
}

// Check queue every 2 minutes — zero cost when queue is empty
setInterval(processControlQueue, 2 * 60 * 1000);

// ─────────────────────────────────────────────
// ── FULL DIVISION SCHEDULE ──
// ─────────────────────────────────────────────
// All division tasks run here on their SOUL.md schedule.
// Mission Control is the primary interface — Discord webhook fires on escalations.
// Health check-in is handled via the dashboard widget (no Telegram needed).

const TZ = 'America/Halifax';

// ── Briefing compiler ──────────────────────────────────────────────────────
function compileBriefing(type) {
  try {
    const divs = ['opportunity', 'trading', 'personal', 'dev-automation', 'op-sec', 'sentinel'];
    const sections = [];
    const escalations = [];

    for (const div of divs) {
      const packetDir = path.join(ROOT, 'divisions', div, 'packets');
      if (!fs.existsSync(packetDir)) continue;
      const files = fs.readdirSync(packetDir).filter(f => f.endsWith('.json'));
      for (const f of files) {
        try {
          const pkt = JSON.parse(fs.readFileSync(path.join(packetDir, f), 'utf8'));
          if (pkt.escalate && pkt.escalation_reason) escalations.push(`[${div.toUpperCase()}] ${pkt.escalation_reason}`);
          if (pkt.summary) sections.push(`**${div.toUpperCase()} / ${pkt.skill}** (${pkt.status})\n${pkt.summary}`);
        } catch(e) {}
      }
    }

    const timestamp = new Date().toISOString();
    const header = type === 'morning'
      ? `# Morning Briefing — ${new Date().toLocaleDateString('en-CA', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' })}`
      : `# Daily Executive Briefing — ${new Date().toLocaleDateString('en-CA')}`;

    const escalationBlock = escalations.length
      ? `\n## ⚡ Escalations\n${escalations.map(e => `- ${e}`).join('\n')}\n`
      : '\n## Escalations\nNone.\n';

    const content = [header, escalationBlock, '## Division Summary', ...sections].join('\n\n');

    writeState('briefing.json', { content, type, last_generated: timestamp });
    logActivity('SYS', `${type} briefing compiled (${sections.length} division reports)`, 'blue');

    // Discord ping so Matthew knows it's ready
    const webhookUrl = process.env.DISCORD_WEBHOOK_URL;
    if (webhookUrl) {
      const msg = type === 'morning'
        ? `**J_Claw Morning Briefing** is ready — open Mission Control to review.`
        : `**J_Claw Daily Briefing** is ready — ${escalations.length} escalation(s). Open Mission Control.`;
      const body = JSON.stringify({ content: msg });
      const parsed = new url.URL(webhookUrl);
      const lib = parsed.protocol === 'https:' ? https : http;
      const req = lib.request({ hostname: parsed.hostname, path: parsed.pathname + parsed.search,
        method: 'POST', headers: { 'Content-Type': 'application/json', 'User-Agent': 'J_Claw/1.0', 'Content-Length': Buffer.byteLength(body) }
      }, () => {});
      req.on('error', () => {});
      req.write(body);
      req.end();
    }
  } catch(e) {
    logActivity('SYS', `briefing compile failed: ${e.message}`, 'red');
  }
}

// ── Opportunity Division ───────────────────────────────────────────────────
// Job intake + hard-filter every 3 hours
cron.schedule('7 */3 * * *', async () => {
  logActivity('OPPS', 'Scheduled job-intake starting...', 'blue');
  await runSkillViaPython('job-intake', 'OPPS');
}, { timezone: TZ });

// Funding finder daily at 2:00 PM
cron.schedule('0 14 * * *', async () => {
  await runSkillViaPython('funding-finder', 'OPPS');
}, { timezone: TZ });

// ── Trading Division ───────────────────────────────────────────────────────
// Market scan every 2 hours, 8am–10pm, all days (crypto is 24/7)
cron.schedule('0 8,10,12,14,16,18,20,22 * * *', async () => {
  await runSkillViaPython('market-scan', 'TRADING');
}, { timezone: TZ });

// Trading performance report daily at 6:00 PM
cron.schedule('0 18 * * *', async () => {
  await runSkillViaPython('trading-report', 'TRADING');
}, { timezone: TZ });

// Agent-network daily auto-trigger — weekdays 09:05 so cycle never goes stale
cron.schedule('5 9 * * 1-5', () => {
  const body = JSON.stringify({ cycle: 1, asset: 'SPX500', auto: true });
  const req = http.request({
    hostname: '127.0.0.1', port: 8000, path: '/run', method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) }
  }, () => {});
  req.on('error', () => logActivity('TRADING', 'Agent-network auto-trigger failed — Zenith offline?', 'yellow'));
  req.write(body);
  req.end();
  logActivity('TRADING', 'Agent-network daily cycle auto-triggered', 'teal');
}, { timezone: TZ });

// ── Personal Division ──────────────────────────────────────────────────────
// Health check-in prompt at 6:00 PM — write active prompt to state for dashboard widget
cron.schedule('0 18 * * *', () => {
  writeState('health-prompt.json', {
    active: true,
    prompted_at: new Date().toISOString(),
    message: "How are you feeling today? (sleep, energy, mood, any aches — whatever's relevant)",
  });
  logActivity('PERSONAL', 'Health check-in prompt active', 'purple');
}, { timezone: TZ });

// Performance correlation daily at 8:00 PM
cron.schedule('0 20 * * *', async () => {
  await runSkillViaPython('perf-correlation', 'PERSONAL');
}, { timezone: TZ });

// Burnout monitor daily at 9:00 PM
cron.schedule('0 21 * * *', async () => {
  await runSkillViaPython('burnout-monitor', 'PERSONAL');
}, { timezone: TZ });

// Personal digest daily at 9:30 PM
cron.schedule('30 21 * * *', async () => {
  await runSkillViaPython('personal-digest', 'PERSONAL');
}, { timezone: TZ });

// ── Dev Automation Division ────────────────────────────────────────────────
// Dev digest daily at 3:00 PM
cron.schedule('0 15 * * *', async () => {
  await runSkillViaPython('dev-digest', 'DEV');
}, { timezone: TZ });

// Dev weekly scans — TEMP: daily until verified, then restore Sunday-only
// refactor-scan: TEMP hourly for verification
cron.schedule('0 * * * *', async () => {
  await runSkillViaPython('refactor-scan', 'DEV');
}, { timezone: TZ });

cron.schedule('0 11 * * *', async () => {
  await runSkillViaPython('security-scan', 'DEV');
}, { timezone: TZ });

// doc-update at 13:00 — 2h after refactor-scan to ensure VRAM is clear
cron.schedule('0 13 * * *', async () => {
  await runSkillViaPython('doc-update', 'DEV');
}, { timezone: TZ });

// Artifact cleanup daily at 3:00 AM
cron.schedule('0 3 * * *', async () => {
  await runSkillViaPython('artifact-manager', 'DEV');
}, { timezone: TZ });

// ── OP-Sec Division ────────────────────────────────────────────────────────
// Mobile audit review nightly at 11:00 PM — reviews all mobile coding sessions
cron.schedule('0 23 * * *', async () => {
  await runSkillViaPython('mobile-audit-review', 'OP_SEC');
}, { timezone: TZ });

// Device posture daily at 8:00 AM (already runs via queue — ensure cron exists)
cron.schedule('0 8 * * *', async () => {
  await runSkillViaPython('device-posture', 'OP_SEC');
}, { timezone: TZ });

// Threat surface scan daily at 7:00 PM
cron.schedule('0 19 * * *', async () => {
  await runSkillViaPython('threat-surface', 'OP_SEC');
}, { timezone: TZ });

// OP-SEC deep scans — TEMP: daily until verified, then restore Sunday-only
// Staggered from 14:00 to avoid VRAM conflict with doc-update at 13:00
cron.schedule('0 14 * * *', async () => {
  await runSkillViaPython('breach-check', 'OP_SEC');
}, { timezone: TZ });

cron.schedule('0 15 * * *', async () => {
  await runSkillViaPython('cred-audit', 'OP_SEC');
}, { timezone: TZ });

cron.schedule('0 16 * * *', async () => {
  await runSkillViaPython('privacy-scan', 'OP_SEC');
}, { timezone: TZ });

// opsec-digest — daily at 16:30 (after breach/cred/privacy complete)
cron.schedule('30 16 * * *', async () => {
  await runSkillViaPython('opsec-digest', 'OP_SEC');
}, { timezone: TZ });

// ── Briefings ──────────────────────────────────────────────────────────────
// Morning briefing at 6:00 AM
cron.schedule('0 6 * * *', () => {
  compileBriefing('morning');
}, { timezone: TZ });

// Full daily executive briefing at 10:00 PM
cron.schedule('0 22 * * *', () => {
  compileBriefing('daily');
}, { timezone: TZ });

// ── Live context file — refreshed every 5 minutes ──────────────────────────
function writeLiveContext() {
  try {
    const context = buildContext();
    const timestamp = new Date().toISOString();
    const content = `# J_Claw Live Context\nGenerated: ${timestamp}\n\n${context}`;
    fs.writeFileSync(path.join(STATE_DIR, 'live-context.txt'), content, 'utf8');
  } catch(e) {}
}
writeLiveContext();
setInterval(writeLiveContext, 5 * 60 * 1000);

// ── Startup ──
// On startup, reset any queue items stuck as "running" (from a crashed/restarted server)
try {
  const ctrl = readState('control.json');
  if (ctrl && ctrl.queue) {
    let fixed = 0;
    ctrl.queue.forEach(e => { if (e.status === 'running') { e.status = 'queued'; fixed++; } });
    if (fixed > 0) { writeState('control.json', ctrl); console.log(`  [SYS] Reset ${fixed} stuck queue item(s) to queued`); }
  }
} catch(e) {}

server.listen(PORT, '0.0.0.0', () => {
  console.log('');
  console.log('  ==========================================');
  console.log('   OpenClaw // Mission Control');
  console.log('  ==========================================');
  console.log('');
  console.log('  Server    : http://localhost:' + PORT);
  console.log('  Dashboard : http://localhost:' + PORT + '/dashboard');
  console.log('');
  console.log('  Scheduler : node-cron active — full SOUL.md schedule (22 crons)');
  console.log('  Queue     : polling every 2 min (zero-cost when idle)');
  console.log('  Timezone  : America/Halifax');
  console.log('');
  console.log('  For persistence across reboots:');
  console.log('    npm i -g pm2 && pm2 start server.js --name openclaw');
  console.log('    pm2 startup  &&  pm2 save');
  console.log('');
});
