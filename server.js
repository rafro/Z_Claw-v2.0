// OpenClaw Mission Control — API + Static Server + Scheduler
// Port: 3000

const http   = require('http');
const https  = require('https');
const fs     = require('fs');
const path   = require('path');
const url    = require('url');
const { spawn } = require('child_process');
const cron   = require('node-cron');

const ROOT      = __dirname;
const STATE_DIR = path.join(ROOT, 'state');
const PORT      = 3000;

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
};

const DIV_XP_THRESHOLDS = [0, 51, 151, 301, 500];

// XP granted per skill completion (server-side, deterministic)
const SKILL_XP = {
  'job-intake':       { division: 'opportunity',    amount: 10 },
  'hard-filter':      { division: 'opportunity',    amount: 5  },
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

function xpForNextLevel(level) {
  return level * 100;
}

function applyXP(stats, amount) {
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
function grantDivisionXP(division, amount) {
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
  } catch(e) {}
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

  const { leveled, rankChanged } = applyXP(stats, amount);
  writeState('jclaw-stats.json', stats);

  logActivity('SYS', `⚔ Ruler bestowed ${amount} XP — ${reason}`, 'yellow');
  if (rankChanged) {
    logActivity('SYS', `⚔ RANK UP: ${oldRank} → ${stats.rank} (Lvl ${stats.level})`, 'purple');
  }

  jsonOk(res, {
    ok: true, amount, reason,
    new_level: stats.level, new_rank: stats.rank,
    base_xp: stats.base_xp, xp_to_next_level: stats.xp_to_next_level,
    rank_up: rankChanged, old_rank: oldRank,
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
    const trades = JSON.parse(fs.readFileSync(path.join(STATE_DIR, 'trade-log.json'), 'utf8'));
    const sessions = trades.sessions || trades.entries || [];
    lines.push('\n[ TRADING ]');
    if (sessions.length > 0) {
      const last = sessions[sessions.length - 1];
      lines.push(`  last session: ${last.date || last.time || 'unknown'}`);
      if (last.pnl !== undefined) lines.push(`  P&L: ${last.pnl}`);
      if (last.trades)            lines.push(`  trades: ${last.trades}`);
    } else {
      lines.push('  no sessions logged yet');
    }
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
  const systemPrompt = soul + '\n\n---\n\n' + context;

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

// ── Response helpers ──
function jsonOk(res, data) {
  res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
  res.end(JSON.stringify(data));
}

function jsonError(res, code, msg) {
  res.writeHead(code, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
  res.end(JSON.stringify({ error: msg }));
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
    res.writeHead(204, { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET,POST', 'Access-Control-Allow-Headers': 'Content-Type' });
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
  grantDivisionXP('opportunity', 10);
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
// Market scan every 2 hours during market hours Mon–Fri (9 AM – 5 PM)
cron.schedule('0 9,11,13,15,17 * * 1-5', async () => {
  await runSkillViaPython('market-scan', 'TRADING');
}, { timezone: TZ });

// Trading performance report daily at 6:00 PM
cron.schedule('0 18 * * *', async () => {
  await runSkillViaPython('trading-report', 'TRADING');
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

// Sunday scans
cron.schedule('0 10 * * 0', async () => {
  await runSkillViaPython('refactor-scan', 'DEV');
}, { timezone: TZ });

cron.schedule('0 11 * * 0', async () => {
  await runSkillViaPython('security-scan', 'DEV');
}, { timezone: TZ });

cron.schedule('0 12 * * 0', async () => {
  await runSkillViaPython('doc-update', 'DEV');
}, { timezone: TZ });

// Artifact cleanup daily at 3:00 AM
cron.schedule('0 3 * * *', async () => {
  await runSkillViaPython('artifact-manager', 'DEV');
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
  console.log('  Scheduler : node-cron active — full SOUL.md schedule (14 crons)');
  console.log('  Queue     : polling every 2 min (zero-cost when idle)');
  console.log('  Timezone  : America/Halifax');
  console.log('');
  console.log('  For persistence across reboots:');
  console.log('    npm i -g pm2 && pm2 start server.js --name openclaw');
  console.log('    pm2 startup  &&  pm2 save');
  console.log('');
});
