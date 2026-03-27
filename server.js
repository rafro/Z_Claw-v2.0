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

// ── Optional deps (ws, web-push) — loaded lazily so server starts without npm i ──
let WebSocketServer = null;
let webpush = null;
try { WebSocketServer = require('ws').WebSocketServer; } catch(e) { console.warn('  [ws] not installed — WebSocket disabled. Run: npm install ws'); }
try { webpush = require('web-push'); } catch(e) { console.warn('  [web-push] not installed — push disabled. Run: npm install web-push'); }

const ROOT      = __dirname;
const STATE_DIR = path.join(ROOT, 'state');
const GAME_EVENTS_FILE = path.join(STATE_DIR, 'game-events.jsonl');
const PORT      = 3000;

// ── Mobile SSE subscribers ──
const _mobileAlertSubscribers = new Set();

// ── Gamification SSE subscribers (PC + mobile) ──
const _gamifSubscribers = new Set();

// ── Pending coding approvals: sessionId → { preSessionHead, filesChanged, diffStat, timer } ──
const _pendingCodingApprovals = new Map();

// ── WebSocket clients ──
const _wsClients = new Set();

function broadcastWS(type, data) {
  if (!_wsClients.size) return;
  const msg = JSON.stringify({ type, ...data });
  for (const ws of _wsClients) {
    try { if (ws.readyState === 1) ws.send(msg); } catch(e) { _wsClients.delete(ws); }
  }
}

// ── Push subscription store ──
const _pushSubscriptions = new Set();
const PUSH_SUBS_FILE = path.join(__dirname, 'state', 'push-subscriptions.json');
function _loadPushSubs() {
  try { return new Set(JSON.parse(fs.readFileSync(PUSH_SUBS_FILE, 'utf8'))); } catch { return new Set(); }
}
function _savePushSubs() {
  try { fs.writeFileSync(PUSH_SUBS_FILE, JSON.stringify([..._pushSubscriptions], null, 2)); } catch(e) {}
}
// Load existing subs on startup
try { for (const s of _loadPushSubs()) _pushSubscriptions.add(s); } catch(e) {}

// VAPID keys — generate once and store; placeholder until npm i web-push
const VAPID_FILE = path.join(__dirname, 'state', 'vapid-keys.json');
let VAPID_KEYS = null;
function _ensureVapid() {
  if (!webpush) return;
  try {
    VAPID_KEYS = JSON.parse(fs.readFileSync(VAPID_FILE, 'utf8'));
  } catch(e) {
    VAPID_KEYS = webpush.generateVAPIDKeys();
    try { fs.writeFileSync(VAPID_FILE, JSON.stringify(VAPID_KEYS, null, 2)); } catch(_) {}
  }
  webpush.setVapidDetails('mailto:openclaw@realm.local', VAPID_KEYS.publicKey, VAPID_KEYS.privateKey);
}
_ensureVapid();

// ── Snooze store ──
const SNOOZED_ALERTS_FILE = path.join(__dirname, 'state', 'snoozed-alerts.json');
function _loadSnoozedAlerts() {
  try { return JSON.parse(fs.readFileSync(SNOOZED_ALERTS_FILE, 'utf8')); } catch { return {}; }
}
function _saveSnoozedAlerts(obj) {
  try { fs.writeFileSync(SNOOZED_ALERTS_FILE, JSON.stringify(obj, null, 2)); } catch(e) {}
}

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

function fileSizeSafe(filePath) {
  try {
    return fs.existsSync(filePath) ? fs.statSync(filePath).size : 0;
  } catch(_) {
    return 0;
  }
}

function readGameEventsSince(offset = 0) {
  try {
    if (!fs.existsSync(GAME_EVENTS_FILE)) return [];
    const size = fs.statSync(GAME_EVENTS_FILE).size;
    if (size <= offset) return [];

    const fd = fs.openSync(GAME_EVENTS_FILE, 'r');
    try {
      const len = size - offset;
      const buf = Buffer.alloc(len);
      fs.readSync(fd, buf, 0, len, offset);
      return buf.toString('utf8')
        .split('\n')
        .filter(Boolean)
        .map(line => {
          try { return JSON.parse(line); } catch(_) { return null; }
        })
        .filter(Boolean);
    } finally {
      fs.closeSync(fd);
    }
  } catch(_) {
    return [];
  }
}

function parseJsonOutput(raw) {
  try {
    return JSON.parse((raw || '').trim() || '{}');
  } catch(_) {
    return {};
  }
}

function runRealmKeeperTask(task, args = []) {
  return new Promise((resolve, reject) => {
    const runId = simpleId();
    const gameEventOffset = fileSizeSafe(GAME_EVENTS_FILE);
    const runDivisionPath = path.join(ROOT, 'run_division.py');
    const proc = spawn(PYTHON_EXE, [runDivisionPath, 'realm-keeper', task, ...args.map(v => String(v))], {
      env: { ...process.env, JCLAW_RUN_ID: runId },
      windowsHide: true,
      cwd: ROOT,
    });

    let stdout = '';
    let stderr = '';
    proc.stdout.on('data', d => { stdout += d.toString(); });
    proc.stderr.on('data', d => { stderr += d.toString(); });

    proc.on('close', code => {
      if (code !== 0) {
        const errLine = stderr.split('\n').filter(Boolean).pop() || `exit ${code}`;
        return reject(new Error(errLine.trim()));
      }
      const canonicalEvents = readGameEventsSince(gameEventOffset).filter(evt => evt.run_id === runId);
      canonicalEvents.forEach(_consumeCanonicalGameEvent);
      resolve({ result: parseJsonOutput(stdout), canonicalEvents, stdout, stderr });
    });
  });
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

// ── Realm world data (canonical — mirrors runtime/realm/config.py) ────────────
// To change XP values or rank names, update runtime/realm/config.py first,
// then keep this in sync. Python is the sole writer of jclaw-stats.json.

const DIV_RANKS = {
  opportunity:    ['Scout of the Dawnhunt', 'Vanguard Pathfinder', 'Grand Hunter', 'Sovereign Tracker', 'Spear of the Hunt'],
  trading:        ['Signal Initiate', 'Veil Adept', 'Pattern Seer', 'Voice of the Oracle', 'Grand Oracle of Markets'],
  dev_automation: ['Codex Initiate', 'Iron Smith', 'Forge Warden', 'Codex Architect', 'Master of the Iron Codex'],
  personal:       ['Covenant Initiate', 'Flame Tender', 'Guardian of Vitality', 'Covenant Warden', 'Eternal Keeper'],
  op_sec:         ['Circle Watchman', 'Veil Scout', 'Shadow Warden', 'Grand Sentinel', 'Sovereign of the Null'],
  production:     ['Apprentice of the Forge', 'Craftwright Adept', 'Lykeon Architect', 'Master of the Forge', 'Lyke, Architect of the Lykeon Forge'],
};

const DIV_COMMANDERS = {
  opportunity:    { name: 'VAEL',   order: 'The Dawnhunt Order' },
  trading:        { name: 'SEREN',  order: 'The Auric Veil'     },
  dev_automation: { name: 'KAELEN', order: 'The Iron Codex'     },
  personal:       { name: 'LYRIN',  order: 'The Ember Covenant' },
  op_sec:         { name: 'ZETH',   order: 'The Nullward Circle' },
  production:     { name: 'LYKE',   order: 'The Lykeon Forge'   },
};

const DIV_XP_THRESHOLDS = [0, 51, 151, 301, 500];

// Base XP auto-granted when a division crosses a rank tier (mirrors config.py)
const RANK_UP_BASE_XP = { 1: 15, 2: 25, 3: 40, 4: 60 };

// XP per skill — canonical values match runtime/realm/config.py
const SKILL_XP = {
  // Opportunity — The Dawnhunt Order
  'job-intake':         { division: 'opportunity',    amount: 10 },
  'hard-filter':        { division: 'opportunity',    amount:  5 },
  'funding-finder':     { division: 'opportunity',    amount:  5 },
  // Trading — The Auric Veil
  'trading-report':     { division: 'trading',        amount: 15 },
  'market-scan':        { division: 'trading',        amount:  5 },
  'virtual-trader':     { division: 'trading',        amount:  8 },
  'backtester':         { division: 'trading',        amount:  5 },
  // Dev Automation — The Iron Codex
  'repo-monitor':       { division: 'dev_automation', amount: 10 },
  'refactor-scan':      { division: 'dev_automation', amount:  5 },
  'doc-update':         { division: 'dev_automation', amount:  5 },
  'debug-agent':        { division: 'dev_automation', amount:  8 },
  'artifact-manager':   { division: 'dev_automation', amount:  3 },
  'dev-digest':         { division: 'dev_automation', amount:  5 },
  'dev-pipeline':       { division: 'dev_automation', amount: 10 },
  // Personal — The Ember Covenant
  'health-logger':      { division: 'personal',       amount: 15 },
  'perf-correlation':   { division: 'personal',       amount: 10 },
  'burnout-monitor':    { division: 'personal',       amount:  5 },
  'personal-digest':    { division: 'personal',       amount:  5 },
  // Op-Sec — The Nullward Circle
  'device-posture':     { division: 'op_sec',         amount: 10 },
  'breach-check':       { division: 'op_sec',         amount: 10 },
  'threat-surface':     { division: 'op_sec',         amount:  8 },
  'cred-audit':         { division: 'op_sec',         amount:  8 },
  'privacy-scan':       { division: 'op_sec',         amount:  5 },
  'opsec-digest':       { division: 'op_sec',         amount:  5 },
  'mobile-audit-review':{ division: 'op_sec',         amount:  5 },
  'sentinel-health':    { division: 'op_sec',         amount:  5 },
  'security-scan':      { division: 'op_sec',         amount: 10 },
  // Production — The Lykeon Forge
  'image-generate':     { division: 'production',     amount: 15 },
  'sprite-generate':    { division: 'production',     amount: 20 },
  'video-generate':     { division: 'production',     amount: 20 },
  'graphic-design':     { division: 'production',     amount: 15 },
  'prompt-craft':       { division: 'production',     amount:  5 },
  'style-check':        { division: 'production',     amount:  8 },
  'image-review':       { division: 'production',     amount:  8 },
  'audio-test':         { division: 'production',     amount:  8 },
  'video-review':       { division: 'production',     amount: 10 },
  'asset-catalog':      { division: 'production',     amount:  5 },
  'storyboard-compose': { division: 'production',     amount: 10 },
  'continuity-check':   { division: 'production',     amount:  8 },
  'asset-deliver':      { division: 'production',     amount:  5 },
  'production-digest':  { division: 'production',     amount: 10 },
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
  'backtester':       { divState: 'trading',        division: 'trading',        task: 'backtester'       },
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
  // Production Division — The Lykeon Forge
  'image-generate':     { divState: 'production', division: 'production', task: 'image-generate'     },
  'sprite-generate':    { divState: 'production', division: 'production', task: 'sprite-generate'    },
  'video-generate':     { divState: 'production', division: 'production', task: 'video-generate'     },
  'graphic-design':     { divState: 'production', division: 'production', task: 'graphic-design'     },
  'prompt-craft':       { divState: 'production', division: 'production', task: 'prompt-craft'       },
  'style-check':        { divState: 'production', division: 'production', task: 'style-check'        },
  'image-review':       { divState: 'production', division: 'production', task: 'image-review'       },
  'audio-test':         { divState: 'production', division: 'production', task: 'audio-test'         },
  'video-review':       { divState: 'production', division: 'production', task: 'video-review'       },
  'asset-catalog':      { divState: 'production', division: 'production', task: 'asset-catalog'      },
  'storyboard-compose': { divState: 'production', division: 'production', task: 'storyboard-compose' },
  'continuity-check':   { divState: 'production', division: 'production', task: 'continuity-check'   },
  'asset-deliver':      { divState: 'production', division: 'production', task: 'asset-deliver'      },
  'production-digest':  { divState: 'production', division: 'production', task: 'production-digest'  },
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

function deriveBaseProgress(totalBaseXP = 0) {
  let level = 1;
  let xpIntoLevel = Math.max(0, Math.floor(totalBaseXP || 0));
  let xpForLevel = xpForNextLevel(level);
  while (xpIntoLevel >= xpForLevel) {
    xpIntoLevel -= xpForLevel;
    level++;
    xpForLevel = xpForNextLevel(level);
  }
  return {
    level,
    rank: rankForLevel(level),
    xp_into_level: xpIntoLevel,
    xp_for_next_level: xpForLevel,
    xp_to_next_level: Math.max(0, xpForLevel - xpIntoLevel),
  };
}

function syncBaseProgress(stats) {
  if (!stats || typeof stats !== 'object') return stats;
  stats.base_xp = Math.max(0, Math.floor(stats.base_xp || 0));
  if (!stats.total_xp_earned) stats.total_xp_earned = 0;
  const progress = deriveBaseProgress(stats.base_xp);
  stats.level = progress.level;
  stats.rank = progress.rank;
  stats.xp_into_level = progress.xp_into_level;
  stats.xp_for_next_level = progress.xp_for_next_level;
  stats.xp_to_next_level = progress.xp_to_next_level;
  return stats;
}

function applyXP(stats, amount) {
  syncBaseProgress(stats);
  const oldLevel = stats.level || 1;
  const oldRank  = stats.rank || rankForLevel(oldLevel);
  stats.base_xp += amount;
  stats.total_xp_earned += amount;
  syncBaseProgress(stats);
  stats.last_updated = new Date().toISOString();
  return { leveled: stats.level > oldLevel, rankChanged: stats.rank !== oldRank };
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

    // Streak XP multiplier: +10% per 7-day milestone, stacks to +50% at 35 days
    _ensureStreaks(stats);
    const streakDays   = ((stats.streaks || {})[division] || {}).current || 0;
    const streakMult   = Math.min(1.5, 1.0 + Math.floor(streakDays / 7) * 0.1);
    // Prestige multiplier: permanent +5% per prestige (stacks)
    const prestigeMult = stats.prestige_multiplier || 1.0;
    const actualAmount = Math.round(amount * streakMult * prestigeMult);

    const oldDivXP = stats.divisions[division].xp;
    const oldRank  = stats.divisions[division].rank;
    stats.divisions[division].xp += actualAmount;
    const newDivXP = stats.divisions[division].xp;

    // Update division rank based on thresholds
    const divRanks = DIV_RANKS[division] || [];
    const rankIdx = DIV_XP_THRESHOLDS.filter(t => newDivXP >= t).length - 1;
    stats.divisions[division].rank = divRanks[Math.min(rankIdx, divRanks.length - 1)] || stats.divisions[division].rank;
    const newRank = stats.divisions[division].rank;

    // Rank-up: auto-grant base XP for each tier crossed, broadcast event
    const oldRankIdx = DIV_XP_THRESHOLDS.filter(t => oldDivXP >= t).length - 1;
    if (rankIdx > oldRankIdx) {
      logActivity('SYS', `⚔ ${division} rank up: ${newRank}`, 'purple');
      _broadcastGamifEvent({ event: 'rank_up', division, old_rank: oldRank, new_rank: newRank });
      // Auto-grant base XP for each tier crossed (real activity → global level)
      let baseBonus = 0;
      for (let t = oldRankIdx + 1; t <= rankIdx; t++) {
        baseBonus += (RANK_UP_BASE_XP[t] || 0);
      }
      if (baseBonus > 0) {
        const { leveled, rankChanged } = applyXP(stats, baseBonus);
        logActivity('SYS', `✦ Realm advancement: +${baseBonus} base XP (${DIV_COMMANDERS[division]?.order || division} tier ${rankIdx})`, 'blue');
        if (leveled || rankChanged) {
          _broadcastGamifEvent({ event: 'rank_up', division: 'base', new_rank: stats.rank, level: stats.level });
        }
        _appendXpHistory({ event: 'realm_advancement', div: division, tier: rankIdx, base_xp: baseBonus, reason: `${division} rank-up tier ${rankIdx}` });
      }
    }

    stats.last_updated = new Date().toISOString();
    writeState('jclaw-stats.json', stats);

    // Broadcast streak multiplier when active
    if (streakMult > 1.0) {
      _broadcastGamifEvent({ event: 'streak_multiplier_applied', division, multiplier: streakMult, streak_days: streakDays });
    }

    // Auto-prestige check
    _checkAutoPrestige();

    const resolved = skillName || Object.keys(SKILL_XP).find(k => SKILL_XP[k].division === division) || 'unknown';
    setImmediate(() => handleGamifCheck(resolved, division, actualAmount, streakMult));
  } catch(e) {}
}

// Auto-prestige: triggers when all 6 divisions hit rank 5 (>= 500 XP each)
function _checkAutoPrestige() {
  try {
    const stats = readState('jclaw-stats.json');
    if (!stats) return;
    const CORE = ['opportunity', 'trading', 'dev_automation', 'personal', 'op_sec', 'production'];
    if (!CORE.every(d => ((stats.divisions || {})[d] || {}).xp >= 500)) return;
    for (const d of CORE) {
      stats.divisions[d].xp   = 0;
      stats.divisions[d].rank = (DIV_RANKS[d] || ['Unknown'])[0];
    }
    stats.prestige            = (stats.prestige || 0) + 1;
    stats.prestige_multiplier = Math.round((1.0 + stats.prestige * 0.05) * 1000) / 1000;
    stats.last_updated        = new Date().toISOString();
    writeState('jclaw-stats.json', stats);
    logActivity('SYS', `⭐ AUTO-PRESTIGE ${stats.prestige} — ×${stats.prestige_multiplier} XP`, 'purple');
    _broadcastGamifEvent({ event: 'prestige', prestige: stats.prestige, multiplier: stats.prestige_multiplier, auto: true });
    _appendXpHistory({ event: 'prestige', prestige: stats.prestige, multiplier: stats.prestige_multiplier, auto: true });
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

const ALL_DIVISION_KEYS = ['opportunity', 'trading', 'dev_automation', 'personal', 'op_sec', 'production'];

function _ensureStreaks(stats) {
  if (!stats.streaks) stats.streaks = {};
  for (const d of ALL_DIVISION_KEYS) {
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
    { id: 'first_hunt',       cond: () => divXP('opportunity') > 0 },
    { id: 'market_watcher',   cond: () => divXP('trading') > 0 },
    { id: 'code_warden',      cond: () => divXP('dev_automation') > 0 },
    { id: 'covenant_keeper',  cond: () => divXP('personal') > 0 },
    { id: 'veil_opened',      cond: () => divXP('op_sec') > 0 },
    { id: 'loyal_flame',      cond: () => Object.values(stats.streaks || {}).some(s => (s.longest || 0) >= 7) },
    { id: 'division_master',  cond: () => ALL_DIVISION_KEYS.some(d => divIdx(d) >= 3) },
    { id: 'five_orders',      cond: () => ALL_DIVISION_KEYS.every(d => divXP(d) > 0) },
    { id: 'realm_commander',  cond: () => (stats.level || 1) >= 10 },
    { id: 'eternal',          cond: () => (stats.level || 1) >= 50 },
  ];

  for (const { id, cond } of checks) {
    if (!earned.has(id) && cond()) {
      if (!stats.achievements) stats.achievements = [];
      stats.achievements.push(id);
      earned.add(id);
      unlocked.push(id);
    }
  }
  return unlocked;
}

function _broadcastGamifEvent(event) {
  const payload = JSON.stringify({ type: 'gamif', ...event });
  for (const res of _gamifSubscribers) {
    try { res.write(`data: ${payload}\n\n`); } catch(e) { _gamifSubscribers.delete(res); }
  }
  // Also push over WebSocket
  broadcastWS(event.event || 'xp_gained', event);
}

function _appendXpHistory(entry) {
  try {
    const line = JSON.stringify({ ts: new Date().toISOString(), ...entry }) + '\n';
    fs.appendFileSync(path.join(STATE_DIR, 'xp-history.jsonl'), line);
  } catch(e) {}
}

function _consumeCanonicalGameEvent(evt) {
  if (!evt || !evt.event) return;
  _broadcastGamifEvent(evt);

  if (evt.event === 'rank_up' && evt.division && evt.division !== 'base') {
    logActivity('SYS', `⚔ ${evt.division} rank up: ${evt.new_rank || 'new rank'}`, 'purple');
  } else if (evt.event === 'streak_milestone') {
    logActivity('SYS', `🔥 ${evt.division} streak: ${evt.streak} days`, 'yellow');
  } else if (evt.event === 'achievement_unlock') {
    logActivity('SYS', `🏆 Achievement unlocked: ${evt.achievement}`, 'yellow');
  } else if (evt.event === 'prestige') {
    logActivity('SYS', `⭐ PRESTIGE ${evt.prestige} — ×${evt.multiplier} XP`, 'purple');
  } else if (evt.event === 'xp_grant' && evt.source === 'ruler') {
    logActivity('SYS', `⚔ Ruler bestowed ${evt.amount} XP — ${evt.reason || 'Ruler decree'}`, 'yellow');
  }
}

// Called after every skill completion. Updates streak, checks achievements,
// broadcasts SSE, appends telemetry. Does NOT modify division XP.
// actualXp / streakMult are forwarded from grantDivisionXP (Node path).
// For Python-run skills they default to the base SKILL_XP amount / 1.0.
function handleGamifCheck(skillName, divisionKey, actualXp = null, streakMult = null) {
  try {
    const stats = readState('jclaw-stats.json');
    if (!stats) return;

    _ensureStreaks(stats);
    const streakMilestone = _updateStreak(stats, divisionKey);
    const newAchievements = _checkAchievements(stats);

    stats.last_updated = new Date().toISOString();
    writeState('jclaw-stats.json', stats);

    const divStats  = (stats.divisions || {})[divisionKey] || {};
    const baseXp    = (SKILL_XP[skillName] || {}).amount || 0;
    const xpGranted = actualXp !== null ? actualXp : baseXp;
    const mult      = streakMult !== null ? streakMult : 1.0;
    _broadcastGamifEvent({
      event: 'skill_complete', skill: skillName, division: divisionKey,
      xp_granted: xpGranted, multiplier: mult,
      division_xp: divStats.xp, division_rank: divStats.rank,
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

    _appendXpHistory({ event: 'skill_complete', skill: skillName, div: divisionKey,
      xp: xpGranted, multiplier: mult,
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
    const stats = syncBaseProgress(readState('jclaw-stats.json'));
    if (stats) res.write(`data: ${JSON.stringify({ type: 'gamif', event: 'init', stats })}\n\n`);
  } catch(e) {}
  const hb = setInterval(() => { try { res.write(': ping\n\n'); } catch(e) {} }, 25000);
  _gamifSubscribers.add(res);
  req.on('close', () => { clearInterval(hb); _gamifSubscribers.delete(res); });
}

// ── API handlers ──

// GET /api/stats/summary  (also /mobile/api/stats/summary)
function handleStatsSummary(res) {
  try {
    const stats = syncBaseProgress(readState('jclaw-stats.json') || {});
    let xpPerDay7d = 0;
    try {
      const histFile = path.join(STATE_DIR, 'xp-history.jsonl');
      const cutoff   = new Date(); cutoff.setDate(cutoff.getDate() - 7);
      const lines    = fs.readFileSync(histFile, 'utf8').split('\n').filter(l => l.trim());
      const recent   = lines.reduce((sum, line) => {
        try { const e = JSON.parse(line); if (e.xp && e.ts && new Date(e.ts) >= cutoff) return sum + (e.xp || 0); } catch(e) {}
        return sum;
      }, 0);
      xpPerDay7d = Math.round((recent / 7) * 10) / 10;
    } catch(e) {}

    const longestStreaks = {};
    for (const [div, s] of Object.entries(stats.streaks || {})) {
      longestStreaks[div] = s.longest || 0;
    }

    jsonOk(res, {
      total_xp_earned:     stats.total_xp_earned    || 0,
      level:               stats.level               || 1,
      rank:                stats.rank                || 'Apprentice of the Realm',
      prestige:            stats.prestige            || 0,
      prestige_multiplier: stats.prestige_multiplier || 1.0,
      longest_streaks:     longestStreaks,
      achievements_earned: (stats.achievements || []).length,
      achievements_total:  11,
      xp_per_day_7d:       xpPerDay7d,
    });
  } catch(e) { jsonError(res, 500, 'stats summary error'); }
}

// POST /api/prestige  — PC only, Tyler confirms
// Condition: all 6 divisions >= 500 XP. Resets division XP, grants +5% permanent multiplier.
function handlePrestige(res) {
  const stats = readState('jclaw-stats.json');
  if (!stats) return jsonError(res, 500, 'stats not found');

  const DIVISIONS = ['opportunity', 'trading', 'dev_automation', 'personal', 'op_sec', 'production'];
  const notReady  = DIVISIONS.filter(d => ((stats.divisions || {})[d] || {}).xp < 500);
  if (notReady.length > 0) {
    return jsonError(res, 400, `Not eligible — divisions below 500 XP: ${notReady.join(', ')}`);
  }

  // Reset all division XP to 0, reset ranks to entry tier
  for (const d of DIVISIONS) {
    stats.divisions[d].xp   = 0;
    stats.divisions[d].rank = (DIV_RANKS[d] || ['Unknown'])[0];
  }

  stats.prestige            = (stats.prestige || 0) + 1;
  stats.prestige_multiplier = Math.round((1.0 + stats.prestige * 0.05) * 1000) / 1000;
  stats.last_updated        = new Date().toISOString();
  writeState('jclaw-stats.json', stats);

  logActivity('SYS', `⭐ PRESTIGE ${stats.prestige} — permanent XP multiplier: ×${stats.prestige_multiplier}`, 'purple');
  _broadcastGamifEvent({ event: 'prestige', prestige: stats.prestige, multiplier: stats.prestige_multiplier });
  _appendXpHistory({ event: 'prestige', prestige: stats.prestige, multiplier: stats.prestige_multiplier });

  // Push prestige animation event to the theater queue
  try {
    const qp = path.join(STATE_DIR, 'anim-queue.json');
    const queue = fs.existsSync(qp) ? JSON.parse(fs.readFileSync(qp, 'utf8') || '[]') : [];
    queue.push({
      id:         Math.random().toString(36).slice(2, 10),
      type:       'prestige',
      prestige:   stats.prestige,
      multiplier: stats.prestige_multiplier,
      color:      '#a855f7',
      ts:         new Date().toISOString(),
    });
    fs.writeFileSync(qp, JSON.stringify(queue, null, 2));
  } catch(_e) { /* non-fatal */ }

  jsonOk(res, {
    ok: true,
    prestige:            stats.prestige,
    prestige_multiplier: stats.prestige_multiplier,
    message:             `Prestige ${stats.prestige} achieved — permanent ×${stats.prestige_multiplier} XP multiplier`,
  });
}

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
  if (rankChanged) {
    _broadcastGamifEvent({ event: 'rank_up', source: 'base', old_rank: oldRank, new_rank: stats.rank });
  }
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

async function handlePrestigeViaPython(res) {
  try {
    const { result } = await runRealmKeeperTask('force-prestige');
    return jsonOk(res, {
      ok: true,
      prestige: result.prestige,
      prestige_multiplier: result.prestige_multiplier,
      message: result.message || `Prestige ${result.prestige} achieved`,
    });
  } catch(e) {
    const msg = e.message || 'prestige failed';
    if (msg.includes('Not eligible')) return jsonError(res, 400, msg);
    return jsonError(res, 500, msg);
  }
}

async function handleBestowViaPython(body, res) {
  const amount = parseInt(body.amount) || 50;
  const reason = body.reason || 'Ruler\'s decree';
  if (amount <= 0 || amount > 10000) return jsonError(res, 400, 'invalid amount');

  try {
    const { result } = await runRealmKeeperTask('grant-base', [amount, reason]);
    const oldRank = result.rank_up_msg ? result.rank_up_msg.split(' -> ')[0] : '';
    return jsonOk(res, {
      ok: true,
      amount,
      reason,
      new_level: result.level,
      new_rank: result.rank,
      base_xp: result.base_xp,
      xp_into_level: result.xp_into_level,
      xp_for_next_level: result.xp_for_next_level,
      xp_to_next_level: result.xp_to_next_level,
      rank_up: result.rank_up,
      old_rank: oldRank,
      achievements_unlocked: result.new_achievements || [],
    });
  } catch(e) {
    return jsonError(res, 500, e.message || 'bestow failed');
  }
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
  const divisions = ['trading', 'opportunity', 'dev-automation', 'personal', 'op-sec', 'production', 'sentinel'];
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

    // Helper to normalise a raw strategy object (active or history entry)
    function normStrat(h) {
      return {
        name:             h.strategy_name || 'None',
        sharpe:           h.sharpe,
        sortino:          h.sortino,
        win_rate:         h.win_rate != null ? Math.round(h.win_rate * 100) : null,
        avg_r:            h.avg_r,
        avg_win_r:        h.avg_win_r,
        avg_loss_r:       h.avg_loss_r,
        rr_ratio:         h.rr_ratio,
        rr_display:       h.rr_display,
        profit_factor:    h.profit_factor,
        max_drawdown_pct: h.max_drawdown_pct != null ? h.max_drawdown_pct : (h.max_drawdown != null ? Math.round(h.max_drawdown * 1000) / 10 : null),
        trade_count:      h.trade_count,
        oos_sharpe:       h.oos_sharpe,
        oos_win_rate:     h.oos_win_rate != null ? Math.round(h.oos_win_rate * 100) : null,
        oos_trade_count:  h.oos_trade_count,
        oos_growth_pct:   h.oos_growth_pct != null ? Math.round(h.oos_growth_pct * 1000) / 10 : null,
        theoretical_ev_r: h.theoretical_ev_r,
        empirical_ev_r:   h.empirical_ev_r,
        confidence_rating: h.confidence_rating,
        backtest_years:   (h.recent_window && h.recent_window.years) ? Math.round(h.recent_window.years * 10) / 10 : null,
        total_pnl_pct:    h.total_pnl_pct != null ? Math.round(h.total_pnl_pct * 1000) / 10 : null,
        total_pnl_usd:    h.total_pnl_usd != null ? Math.round(h.total_pnl_usd * 100) / 100 : null,
        annualised_return_pct: h.annualised_return_pct != null ? Math.round(h.annualised_return_pct * 100) / 100 : null,
        projected_monthly_pnl_pct: h.projected_monthly_pnl_pct != null ? Math.round(h.projected_monthly_pnl_pct * 10000) / 100 : null,
        return_projections: h.return_projections || null,
        mc_p95_dd:        h.mc_p95_dd != null ? Math.round(h.mc_p95_dd * 1000) / 10 : null,
        best_risk_pct:    h.best_risk_pct,
      };
    }

    // Read virtual account state
    let virtualAccount = null;
    try {
      virtualAccount = JSON.parse(fs2.readFileSync(path2.join(agentNetworkState, 'virtual_account.json'), 'utf8'));
    } catch(e) {}

    // Build performance_history (last 10 cycle winners, newest first, equity_curve stripped for size)
    const rawHistory = (cycleData.performance_history || []).slice(-10).reverse();
    const perfHistory = rawHistory.map((h, i) => ({
      ...normStrat(h),
      label: i === 0 ? 'Current' : `−${i}`,
    }));

    jsonOk(res, {
      available: true,
      cycle_number:     cycleData.cycle_number,
      risk_multiplier:  cycleData.risk_multiplier,
      stale,
      hours_since_update: Math.round(hoursSince * 10) / 10,
      last_modified: new Date(mtimeMs).toISOString(),
      active_strategy: normStrat(strat),
      equity_curve:    strat.equity_curve || null,
      oos_validation:  strat.oos_sharpe != null ? {
        sharpe:      strat.oos_sharpe,
        win_rate:    strat.oos_win_rate != null ? Math.round(strat.oos_win_rate * 100) : null,
        trade_count: strat.oos_trade_count,
        growth_pct:  strat.oos_growth_pct != null ? Math.round(strat.oos_growth_pct * 1000) / 10 : null,
        note:        strat.confidence_rating ? `Confidence: ${strat.confidence_rating}` : null,
      } : null,
      account: virtualAccount ? {
        balance:         virtualAccount.account_balance,
        initial_balance: virtualAccount.initial_balance,
        growth_usd:      Math.round((virtualAccount.account_balance - virtualAccount.initial_balance) * 100) / 100,
        open_positions:  virtualAccount.open_positions || [],
      } : null,
      performance_history: perfHistory,
      agents: {
        strategy_builder: { role: 'Generates 100 strategies/cycle',      last_output: `Cycle ${cycleData.cycle_number}` },
        backtester:       { role: 'Evaluates & selects top 3 strategies', last_output: strat.strategy_name || '—' },
        trader:           { role: 'Executes trades with risk controls',   last_output: `${todayTrades.length} trade(s) today` },
        trading_coach:    { role: 'Reviews performance, adjusts risk',    last_output: lastWeekly.health_tier ? `Health: ${lastWeekly.health_tier}` : 'No review yet' },
      },
      recent_trades: recentTrades.map(t => ({
        symbol:      t.symbol,
        pnl:         t.pnl,
        pnl_dollar:  t.pnl_dollar != null ? t.pnl_dollar : (t.pnl != null && t.risk_usd != null ? Math.round(t.pnl * t.risk_usd * 100) / 100 : null),
        r_multiple:  t.r_multiple,
        result:      t.result,
        reason:      t.reason,
        entry_price: t.entry_price,
        exit_price:  t.exit_price,
        risk_usd:    t.risk_usd,
        date: t.timestamp ? new Date(t.timestamp).toISOString().slice(0,10) : null,
      })),
      latest_weekly_review: lastWeekly && lastWeekly.health_tier ? {
        health_tier:   lastWeekly.health_tier,
        summary:       lastWeekly.summary || lastWeekly.coaching_notes || null,
        date:          lastWeekly.date || lastWeekly.week_end || null,
        win_rate:      lastWeekly.win_rate != null ? Math.round(lastWeekly.win_rate * 100) : null,
        trades:        lastWeekly.total_trades || lastWeekly.trade_count || null,
        pnl_r:         lastWeekly.total_pnl_r || lastWeekly.pnl_r || null,
      } : null,
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

// GET /api/trading/accounts — returns all known trading account states (virtual, alpaca paper, etc.)
function handleGetTradingAccounts(res) {
  const AGENT_STATE = 'C:/Users/Tyler/agent-network/state';
  const accounts    = {};
  const today       = new Date().toISOString().slice(0, 10);

  // Virtual Account (J_Claw paper trader)
  try {
    const va   = JSON.parse(fs.readFileSync(path.join(AGENT_STATE, 'virtual_account.json'), 'utf8'));
    const bal  = va.account_balance  || 10000;
    const init = va.initial_balance  || 10000;
    const pnl  = Math.round((bal - init) * 100) / 100;
    const pnlPct = init > 0 ? Math.round(((bal - init) / init) * 10000) / 100 : 0;
    const log  = va.trade_log || [];
    const todayExits = log.filter(t => t.type === 'exit' && (t.timestamp || '').startsWith(today));
    const todayPnl   = Math.round(todayExits.reduce((s, t) => s + (t.pnl || 0), 0) * 100) / 100;
    accounts.virtual = {
      key:             'virtual',
      label:           'Virtual',
      type:            'paper',
      balance:         bal,
      initial_balance: init,
      pnl_total:       pnl,
      pnl_pct:         pnlPct,
      pnl_today:       todayPnl,
      total_trades:    log.filter(t => t.type === 'exit').length,
      open_positions:  (va.open_positions || []).map(p => ({
        symbol:      p.symbol,
        side:        p.side,
        entry_price: p.entry_price,
        qty:         p.qty,
        risk_usd:    p.risk_usd,
        stop_loss:   p.stop_loss,
        opened_at:   p.opened_at,
      })),
      updated_at: va.updated_at || null,
    };
  } catch(e) {}

  // Alpaca Paper (optional — only included if file exists)
  try {
    const ap   = JSON.parse(fs.readFileSync(path.join(AGENT_STATE, 'alpaca_paper_state.json'), 'utf8'));
    const bal  = ap.account_balance || ap.portfolio_value || 10000;
    const init = ap.initial_balance || 10000;
    const pnl  = Math.round((bal - init) * 100) / 100;
    const pnlPct = init > 0 ? Math.round(((bal - init) / init) * 10000) / 100 : 0;
    const log  = ap.trade_log || [];
    const todayExits = log.filter(t => t.type === 'exit' && (t.timestamp || '').startsWith(today));
    const todayPnl   = Math.round(todayExits.reduce((s, t) => s + (t.pnl || 0), 0) * 100) / 100;
    accounts.alpaca_paper = {
      key:             'alpaca_paper',
      label:           'Alpaca Paper',
      type:            'paper',
      balance:         bal,
      initial_balance: init,
      pnl_total:       pnl,
      pnl_pct:         pnlPct,
      pnl_today:       todayPnl,
      total_trades:    log.filter(t => t.type === 'exit').length,
      open_positions:  (ap.open_positions || []).map(p => ({
        symbol:      p.symbol,
        side:        p.side,
        entry_price: p.entry_price,
        qty:         p.qty,
        risk_usd:    p.risk_usd,
        stop_loss:   p.stop_loss,
      })),
      updated_at: ap.updated_at || null,
    };
  } catch(e) {}

  jsonOk(res, { accounts, account_keys: Object.keys(accounts) });
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
    lines.push(`  XP: ${stats.xp_into_level || 0} / ${stats.xp_for_next_level || stats.xp_to_next_level} | Total base XP: ${stats.base_xp} | Total earned: ${stats.total_xp_earned}`);
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
        if (evt.type === 'stream_event' && evt.event?.type === 'content_block_start' && evt.event.content_block?.type === 'tool_use') {
          res.write(`data: ${JSON.stringify({ type: 'thinking', tool: evt.event.content_block.name })}\n\n`);
        }
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

// ── CORS helper — allow localhost and Tailscale CGNAT range (100.64.0.0/10) only ──
function corsOrigin(req) {
  const origin = (req && req.headers && req.headers.origin) || '';
  if (!origin) return 'http://localhost:3000';
  if (/^https?:\/\/localhost(:\d+)?$/.test(origin)) return origin;
  if (/^https?:\/\/100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+(:\d+)?$/.test(origin)) return origin;
  return 'http://localhost:3000'; // unknown origin — browser will reject the mismatch
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
  'launch_comfyui',      // start ComfyUI via run_amd_gpu.bat if not already running
  'start_agent_network', // start agent-network via PM2 if not already running on :8000
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
      case 'launch_comfyui':
        result = await mobileLaunchComfyUI(); break;
      case 'start_agent_network':
        result = await mobileStartAgentNetwork(); break;
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

async function mobileLaunchComfyUI() {
  // Check if ComfyUI is already running first
  try {
    const http = require('http');
    await new Promise((resolve, reject) => {
      const req = http.get('http://127.0.0.1:8188', { timeout: 2000 }, res => resolve(res));
      req.on('error', reject);
      req.on('timeout', () => { req.destroy(); reject(new Error('timeout')); });
    });
    return { ok: true, message: 'ComfyUI is already running', already_running: true };
  } catch(_) { /* not running — proceed to launch */ }

  // Read comfyui_path from production config
  let batPath = 'C:\\ComfyUI\\run_amd_gpu.bat';
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'divisions/production/config.json'), 'utf8'));
    if (cfg.comfyui_path) batPath = cfg.comfyui_path;
  } catch(_) {}

  if (!fs.existsSync(batPath)) {
    return { ok: false, message: `ComfyUI launcher not found at: ${batPath} — update divisions/production/config.json → comfyui_path` };
  }

  try {
    const batDir = path.dirname(batPath);
    const proc = spawn(
      'cmd.exe',
      ['/c', 'start', '""', batPath],
      { detached: true, stdio: 'ignore', windowsHide: false, cwd: batDir }
    );
    proc.unref();
    logActivity('SYS', '[MOBILE] ComfyUI launch triggered via mobile dashboard', 'yellow');
    mobileAuditLog({ action: 'launch_comfyui', actor: 'mobile-operator', result: 'succeeded', detail: batPath });
    return { ok: true, message: 'ComfyUI is starting — this takes 20-40 seconds. A window opened on the desktop.' };
  } catch(e) {
    return { ok: false, message: 'Failed to launch ComfyUI: ' + e.message };
  }
}

async function mobileStartAgentNetwork() {
  // Check if agent-network is already running on port 8000
  try {
    const http = require('http');
    await new Promise((resolve, reject) => {
      const req = http.get('http://127.0.0.1:8000', { timeout: 2000 }, res => resolve(res));
      req.on('error', reject);
      req.on('timeout', () => { req.destroy(); reject(new Error('timeout')); });
    });
    return { ok: true, message: 'Agent-network already running', already_running: true };
  } catch(_) { /* not running — proceed to start */ }

  try {
    const proc = spawn(
      'powershell.exe',
      ['-NoProfile', '-Command', 'pm2 start C:/Users/Tyler/agent-network/pm2.config.js --no-daemon'],
      { detached: true, stdio: 'ignore' }
    );
    proc.unref();
    logActivity('SYS', '[MOBILE] Agent-network start initiated via mobile dashboard', 'yellow');
    mobileAuditLog({ action: 'start_agent_network', actor: 'mobile-operator', result: 'succeeded' });
    return { ok: true, message: 'Agent-network start initiated' };
  } catch(e) {
    return { ok: false, message: 'Failed to start agent-network: ' + e.message };
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
  try {
    const alerts = _collectMobileAlerts();
    const payload = JSON.stringify({ type: 'update', alerts });
    for (const res of _mobileAlertSubscribers) {
      try { res.write(`data: ${payload}\n\n`); } catch(e) { _mobileAlertSubscribers.delete(res); }
    }
    broadcastWS('alert_fired', { alerts });
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
  const packetDirs = ['op-sec', 'trading', 'opportunity', 'dev-automation', 'personal', 'production', 'sentinel'];
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

  // Production — The Lykeon Forge
  try {
    const cat = JSON.parse(fs.readFileSync(path.join(ROOT, 'divisions', 'production', 'packets', 'asset-catalog.json'), 'utf8'));
    m.production = {
      total_assets:   cat.metrics?.total     || 0,
      pending_review: cat.metrics?.pending   || 0,
      approved:       cat.metrics?.approved  || 0,
      delivered:      cat.metrics?.delivered || 0,
    };
  } catch(e) {
    m.production = { total_assets: 0, pending_review: 0, approved: 0, delivered: 0 };
  }

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

function handleMobileBattlesToday(res) {
  try {
    const fs2 = require('fs');
    const histPath = path.join(STATE_DIR, 'xp-history.jsonl');
    const todayStr = new Date().toISOString().slice(0, 10);
    // Soldier names + order-language labels. Mirrors runtime/realm/config.py skills.
    const SKILL_META = {
      // Dawnhunt Order (opportunity)
      'job-intake':          { label: 'Mark the Quarry',      soldier: 'The Tracker',           icon: '⟶', anim: 'arrow'   },
      'hard-filter':         { label: "The Arbiter's Cut",    soldier: 'The Arbiter',           icon: '⟁', anim: 'slash'   },
      'funding-finder':      { label: 'Strike the Vein',      soldier: 'The Prospector',        icon: '◈', anim: 'scan'    },
      // Auric Veil (trading)
      'trading-report':      { label: "The Oracle's Edict",   soldier: "The Seer's Voice",      icon: '◉', anim: 'slash'   },
      'market-scan':         { label: 'Read the Runes',       soldier: 'The Signal Keeper',     icon: '◈', anim: 'scan'    },
      'virtual-trader':      { label: 'Shadow Run',           soldier: 'The Shadow Runner',     icon: '⟁', anim: 'slash'   },
      'backtester':          { label: 'Pattern Lock',         soldier: 'The Pattern Keeper',    icon: '◫', anim: 'circuit' },
      // Iron Codex (dev_automation)
      'repo-monitor':        { label: 'Watch the Forge',      soldier: 'The Warden',            icon: '⬡', anim: 'circuit' },
      'refactor-scan':       { label: 'Reforge',              soldier: 'The Reforger',          icon: '⟁', anim: 'circuit' },
      'doc-update':          { label: 'Inscribe',             soldier: 'The Scribe',            icon: '◫', anim: 'circuit' },
      'debug-agent':         { label: 'Debug the Construct',  soldier: 'The Debugger',          icon: '◈', anim: 'circuit' },
      'artifact-manager':    { label: 'Temper the Pipeline',  soldier: 'The Relic Keeper',      icon: '⬡', anim: 'circuit' },
      'dev-digest':          { label: 'Codex Report',         soldier: 'The Chronicler',        icon: '◉', anim: 'circuit' },
      'dev-pipeline':        { label: 'Lay the Foundation',   soldier: 'The Architect',         icon: '⬢', anim: 'circuit' },
      // Nullward Circle (op_sec)
      'device-posture':      { label: 'Inspect the Veil',     soldier: 'The Posture Guard',     icon: '⬡', anim: 'shield'  },
      'breach-check':        { label: 'Breach Watch',         soldier: 'The Breach Scout',      icon: '⟁', anim: 'shield'  },
      'threat-surface':      { label: 'Map the Dark',         soldier: 'The Surface Warden',    icon: '◈', anim: 'shield'  },
      'cred-audit':          { label: 'Credential Sweep',     soldier: 'The Credential Keeper', icon: '◫', anim: 'shield'  },
      'privacy-scan':        { label: 'Privacy Ward',         soldier: 'The Privacy Warden',    icon: '⬡', anim: 'shield'  },
      'opsec-digest':        { label: 'Null Report',          soldier: 'The Brief',             icon: '◉', anim: 'shield'  },
      'mobile-audit-review': { label: 'Audit the Mobile Veil',soldier: 'The Mobile Warden',     icon: '◈', anim: 'shield'  },
      'sentinel-health':     { label: 'Sentinel Watch',       soldier: 'The Sentinel',          icon: '⬢', anim: 'shield'  },
      'security-scan':       { label: 'Audit the Veil',       soldier: 'The Code Sentinel',     icon: '⬡', anim: 'shield'  },
      // Ember Covenant (personal)
      'health-logger':       { label: 'Tend the Flame',       soldier: 'The Tender',            icon: '◉', anim: 'sparkle' },
      'perf-correlation':    { label: 'Inner Sight',          soldier: 'The Lens',              icon: '◈', anim: 'sparkle' },
      'burnout-monitor':     { label: 'Read the Ashes',       soldier: 'The Watchfire',         icon: '⟁', anim: 'sparkle' },
      'personal-digest':     { label: "The Covenant's Voice", soldier: 'The Voice',             icon: '◫', anim: 'sparkle' },
      // Sentinel (non-division utility)
      'provider-health':     { label: 'Provider Watch',       soldier: 'The Provider Scout',    icon: '◈', anim: 'scan'    },
      'queue-monitor':       { label: 'Queue Watch',          soldier: 'The Queue Warden',      icon: '◈', anim: 'scan'    },
    };
    const DIV_NAMES = {
      opportunity:    'Dawnhunt',
      trading:        'Auric Veil',
      dev_automation: 'Iron Codex',
      personal:       'Ember Covenant',
      op_sec:         'Nullward',
      production:     'Lykeon Forge',
      sentinel:       'Sentinel',
    };
    const DIV_COMMANDERS_META = {
      opportunity: 'VAEL', trading: 'SEREN', dev_automation: 'KAELEN',
      personal: 'LYRIN', op_sec: 'ZETH', production: 'LYKE',
    };
    let battles = [];
    try {
      const lines = fs2.readFileSync(histPath, 'utf8').split('\n').filter(Boolean);
      battles = lines
        .map(l => { try { return JSON.parse(l); } catch(e) { return null; } })
        .filter(e => e && e.event === 'skill_complete' && e.ts && e.ts.slice(0, 10) === todayStr)
        .map(e => {
          const meta = SKILL_META[e.skill] || { label: e.skill, soldier: '', icon: '⚔', anim: 'slash' };
          return {
            skill:      e.skill,
            label:      meta.label,
            soldier:    meta.soldier || '',
            icon:       meta.icon,
            anim:       meta.anim,
            division:   e.div,
            div_name:   DIV_NAMES[e.div] || e.div,
            commander:  DIV_COMMANDERS_META[e.div] || '',
            xp:         e.xp || 0,
            multiplier: e.multiplier || 1,
            streak:     e.streak || 0,
            time:     e.ts,
          };
        })
        .reverse(); // most recent first
    } catch(e) {}
    jsonOk(res, { available: true, battles, date: todayStr });
  } catch(e) {
    jsonOk(res, { available: false, error: e.message });
  }
}

function handleMobileBattlesWeek(res) {
  try {
    const fs2 = require('fs');
    const histPath = path.join(STATE_DIR, 'xp-history.jsonl');
    const DIV_NAMES = { opportunity: 'Dawnhunt', trading: 'Auric Veil', dev_automation: 'Iron Codex', personal: 'Ember Covenant', op_sec: 'Nullward', production: 'Lykeon Forge' };
    const cutoffDate = new Date(); cutoffDate.setDate(cutoffDate.getDate() - 6);
    const cutoffStr  = cutoffDate.toISOString().slice(0, 10);

    let totalXp = 0, totalBattles = 0;
    const byDiv  = {};
    const byDay  = {};

    try {
      const lines = fs2.readFileSync(histPath, 'utf8').split('\n').filter(Boolean);
      for (const line of lines) {
        try {
          const e = JSON.parse(line);
          if (!e || e.event !== 'skill_complete' || !e.ts) continue;
          const day = e.ts.slice(0, 10);
          if (day < cutoffStr) continue;
          const xp  = e.xp || 0;
          const div = e.div || 'unknown';
          totalXp      += xp;
          totalBattles += 1;
          if (!byDiv[div]) byDiv[div] = { xp: 0, count: 0, name: DIV_NAMES[div] || div };
          byDiv[div].xp    += xp;
          byDiv[div].count += 1;
          if (!byDay[day]) byDay[day] = { xp: 0, count: 0 };
          byDay[day].xp    += xp;
          byDay[day].count += 1;
        } catch(e) {}
      }
    } catch(e) {}

    // Build last 7 days in order
    const days = [];
    for (let i = 6; i >= 0; i--) {
      const d = new Date(); d.setDate(d.getDate() - i);
      const key = d.toISOString().slice(0, 10);
      days.push({ date: key, xp: (byDay[key] || {}).xp || 0, count: (byDay[key] || {}).count || 0 });
    }

    jsonOk(res, { total_xp: totalXp, total_battles: totalBattles, by_division: byDiv, by_day: days });
  } catch(e) {
    jsonOk(res, { total_xp: 0, total_battles: 0, by_division: {}, by_day: [] });
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

// ── New mobile API handlers ───────────────────────────────────────────────────

// GET /mobile/api/briefing
function handleMobileBriefing(res) {
  const f = path.join(STATE_DIR, 'briefing.json');
  if (!fs.existsSync(f)) return jsonOk(res, { content: null, type: null, last_generated: null });
  try { jsonOk(res, JSON.parse(fs.readFileSync(f, 'utf8'))); } catch(e) { jsonOk(res, { content: null }); }
}

// POST /mobile/api/health-checkin
function handleHealthCheckin(req, res, body) {
  try {
    const logFile = path.join(STATE_DIR, 'health-log.json');
    let log = [];
    if (fs.existsSync(logFile)) { try { log = JSON.parse(fs.readFileSync(logFile, 'utf8')); } catch(e) {} }
    const entry = { date: new Date().toISOString().split('T')[0], ts: new Date().toISOString(), ...body, source: 'mobile' };
    log.push(entry);
    fs.writeFileSync(logFile, JSON.stringify(log, null, 2));
    jsonOk(res, { ok: true, entry });
  } catch(e) { jsonError(res, 400, e.message); }
}

// GET /mobile/api/jobs
function handleMobileJobs(req, res) {
  const f = path.join(STATE_DIR, 'applications.json');
  if (!fs.existsSync(f)) return jsonOk(res, { jobs: [], total: 0 });
  try {
    const parsedUrl = new url.URL('http://x' + req.url);
    const apps = JSON.parse(fs.readFileSync(f, 'utf8'));
    const status = parsedUrl.searchParams.get('status') || 'pending_review';
    const limit = parseInt(parsedUrl.searchParams.get('limit')) || 50;
    const filtered = Array.isArray(apps) ? apps.filter(j => (j.status || 'pending_review') === status).slice(0, limit) : [];
    jsonOk(res, { jobs: filtered, total: filtered.length, status });
  } catch(e) { jsonOk(res, { jobs: [], total: 0, error: e.message }); }
}

// POST /mobile/api/jobs/:id/action
function handleJobAction(req, res, jobId, body) {
  try {
    const { action } = body;
    const f = path.join(STATE_DIR, 'applications.json');
    if (!fs.existsSync(f)) return jsonError(res, 404, 'No applications file');
    let apps = JSON.parse(fs.readFileSync(f, 'utf8'));
    const idx = apps.findIndex(j => (j.id || j.job_id || j.url) === jobId);
    if (idx === -1) return jsonError(res, 404, 'Job not found');
    if (action === 'apply') apps[idx].status = 'applied';
    else if (action === 'archive') apps[idx].status = 'archived';
    else if (action === 'snooze') { apps[idx].status = 'snoozed'; apps[idx].snooze_until = new Date(Date.now() + 7*24*60*60*1000).toISOString(); }
    apps[idx].action_ts = new Date().toISOString();
    fs.writeFileSync(f, JSON.stringify(apps, null, 2));
    jsonOk(res, { ok: true, job: apps[idx] });
  } catch(e) { jsonError(res, 400, e.message); }
}

// GET /mobile/api/coding-history
function handleCodingHistory(res) {
  const f = path.join(STATE_DIR, 'coding-history.json');
  if (!fs.existsSync(f)) return jsonOk(res, { sessions: [] });
  try {
    const data = JSON.parse(fs.readFileSync(f, 'utf8'));
    const sessions = Array.isArray(data) ? data.slice(-30).reverse() : [];
    jsonOk(res, { sessions });
  } catch(e) { jsonOk(res, { sessions: [] }); }
}

// GET /mobile/api/trading/signals
function handleTradingSignals(res) {
  const marketDir = path.join(ROOT, 'divisions', 'trading', 'hot');
  let signals = [];
  let latestSummary = '';
  try {
    if (fs.existsSync(marketDir)) {
      const files = fs.readdirSync(marketDir).filter(f => f.startsWith('market-') && f.endsWith('.json')).sort().reverse();
      for (const file of files.slice(0, 5)) {
        try {
          const d = JSON.parse(fs.readFileSync(path.join(marketDir, file), 'utf8'));
          const fileTs = d.generated_at || '';
          if (!latestSummary && d.summary) latestSummary = d.summary;
          if (d.signals) {
            // Normalise signal fields for the UI
            const enriched = d.signals.map(s => ({
              ...s,
              symbol: s.symbol || s.ticker || s.instrument || '?',
              generated_at: s.generated_at || fileTs,
              note: s.note || s.detail || '',
            }));
            signals.push(...enriched);
          }
        } catch(e) {}
      }
    }
  } catch(e) {}
  jsonOk(res, { signals: signals.slice(0, 20), total: signals.length, summary: latestSummary });
}

// GET /mobile/api/achievements
function handleAchievements(res) {
  const f = path.join(STATE_DIR, 'jclaw-stats.json');
  let earned = [];
  if (fs.existsSync(f)) { try { const s = JSON.parse(fs.readFileSync(f, 'utf8')); earned = s.achievements || []; } catch(e) {} }
  const ALL_ACHIEVEMENTS = [
    { id: 'first_blood', name: 'First Blood', icon: '⚔️', desc: 'Complete your first skill run', lore: 'The blade was drawn. The hunt began.' },
    { id: 'five_orders', name: 'Six Orders', icon: '🏰', desc: 'Have all six divisions active', lore: 'The realm stands complete. All orders march.' },
    { id: 'market_watcher', name: 'Market Watcher', icon: '📈', desc: 'Run market-scan 10 times', lore: 'The runes have been read. Patterns emerge from chaos.' },
    { id: 'first_hunt', name: 'First Hunt', icon: '🎯', desc: 'Find your first job opportunity', lore: 'VANCE drew the map. The first target was marked.' },
    { id: 'covenant_keeper', name: 'Covenant Keeper', icon: '🔥', desc: 'Maintain a 7-day streak', lore: 'The flame was kept alive. The pact holds.' },
    { id: 'code_warden', name: 'Code Warden', icon: '🛡️', desc: 'Run a security scan', lore: 'FORGE swept the fortress. No breach went unseen.' },
    { id: 'division_master', name: 'Division Master', icon: '👑', desc: 'Reach rank 3 in any division', lore: 'A commander ascended. The order grows mighty.' },
    { id: 'veil_opened', name: 'Veil Opened', icon: '👁️', desc: 'Complete an op-sec scan', lore: 'WRAITH lifted the shroud. What was hidden is now known.' },
    { id: 'rulers_blessing', name: "Ruler's Blessing", icon: '✨', desc: 'Receive a bestow XP event', lore: 'The sovereign smiled upon the realm.' },
    { id: 'ember_lit', name: 'Ember Lit', icon: '🌿', desc: 'Log health data for 3 consecutive days', lore: 'EMBER tended the flame. Life sustained life.' },
    { id: 'oracle_speaks', name: 'Oracle Speaks', icon: '🔮', desc: 'Generate a trading strategy', lore: 'ORACLE spoke truth. The market had no secrets left.' },
    { id: 'iron_forged', name: 'Iron Forged', icon: '⚙️', desc: 'Complete 5 dev automation tasks', lore: 'The forge ran hot. Code became stronger.' },
    { id: 'sovereign_path', name: 'Sovereign Path', icon: '🌟', desc: 'Reach level 5 overall', lore: 'J_Claw ascended. The realm trembled with power.' },
  ];
  const result = ALL_ACHIEVEMENTS.map(a => ({ ...a, unlocked: earned.includes(a.id), unlock_date: null }));
  jsonOk(res, { achievements: result, earned_count: earned.length, total: result.length });
}

// GET /mobile/api/division/:id/report
function handleDivisionReport(req, res, divId) {
  const packetPaths = [
    path.join(ROOT, 'divisions', divId, 'hot', 'packet.json'),
    path.join(ROOT, 'divisions', divId.replace('-', '_'), 'hot', 'packet.json'),
    path.join(STATE_DIR, `${divId}-last-packet.json`),
    path.join(STATE_DIR, `${divId.replace('-','_')}-last-packet.json`),
  ];
  for (const p of packetPaths) {
    if (fs.existsSync(p)) {
      try { return jsonOk(res, { ok: true, report: JSON.parse(fs.readFileSync(p, 'utf8')), path: p }); } catch(e) {}
    }
  }
  const actFile = path.join(STATE_DIR, 'activity-log.json');
  if (fs.existsSync(actFile)) {
    try {
      const log = JSON.parse(fs.readFileSync(actFile, 'utf8'));
      const entries = log.entries || log;
      const divEntries = (Array.isArray(entries) ? entries : []).filter(e => (e.division || '').toLowerCase().includes(divId.replace('-','_').split('_')[0])).slice(-5);
      if (divEntries.length) return jsonOk(res, { ok: true, report: null, recent_activity: divEntries });
    } catch(e) {}
  }
  jsonError(res, 404, 'No report found');
}

// ── Realm Layer endpoints ─────────────────────────────────────────────────────

// GET /mobile/api/realm/config
// Serves the full world config to the frontend. Replaces hardcoded PARTY object.
function handleRealmConfig(res) {
  try {
    const divs     = {};
    for (const [key, div] of Object.entries(DIV_COMMANDERS)) {
      const ranks = DIV_RANKS[key] || [];
      divs[key] = {
        key,
        commander: div.name,
        order:     div.order,
        ranks:     ranks.map((title, i) => ({ xp: DIV_XP_THRESHOLDS[i], title })),
      };
    }
    jsonOk(res, {
      divisions:    divs,
      thresholds:   DIV_XP_THRESHOLDS,
      rank_up_base_xp: RANK_UP_BASE_XP,
    });
  } catch(e) { jsonError(res, 500, 'realm config error'); }
}

// GET /mobile/api/realm/directive
// Computes the highest-leverage next action for Matthew.
// Priority: streak_at_risk > dormant_order > near_rank_up > lagging_order
function handleRealmDirective(res) {
  try {
    const stats  = readState('jclaw-stats.json') || {};
    const divs   = stats.divisions || {};
    const streaks = stats.streaks  || {};
    const today  = new Date().toISOString().slice(0, 10);
    const directives = [];

    for (const key of ALL_DIVISION_KEYS) {
      const div      = divs[key]    || { xp: 0 };
      const streak   = streaks[key] || {};
      const cmdMeta  = DIV_COMMANDERS[key] || {};
      const divRanks = DIV_RANKS[key]      || [];
      const tierIdx  = DIV_XP_THRESHOLDS.filter(t => (div.xp || 0) >= t).length - 1;
      const nextThr  = DIV_XP_THRESHOLDS[tierIdx + 1];
      const xpAway   = nextThr ? nextThr - (div.xp || 0) : null;

      // 1. Streak at risk (has streak, hasn't run today)
      if (streak.current > 0 && streak.last_date && streak.last_date !== today) {
        const mult = Math.min(1.5, 1.0 + Math.floor(streak.current / 7) * 0.1);
        directives.push({
          type:      'streak_at_risk',
          urgency:   'critical',
          division:  key,
          commander: cmdMeta.name,
          order:     cmdMeta.order,
          title:     `${cmdMeta.name}'s streak is at risk`,
          message:   `${cmdMeta.order} has a ${streak.current}-day streak — unconfirmed today. Break now and lose the ×${mult.toFixed(1)} multiplier.`,
          action:    `Run a ${cmdMeta.order} skill to hold the streak.`,
          xp_at_stake: Math.round(20 * mult), // rough daily XP potential
          streak_days: streak.current,
          score: 1000 + streak.current * 10,
        });
      }
      // 2. Dormant order (never ran or no streak at all)
      if ((div.xp || 0) === 0 || streak.current === 0) {
        const isDormant = (div.xp || 0) === 0;
        directives.push({
          type:      isDormant ? 'dormant_order' : 'no_streak',
          urgency:   isDormant ? 'high' : 'normal',
          division:  key,
          commander: cmdMeta.name,
          order:     cmdMeta.order,
          title:     isDormant ? `${cmdMeta.name} has not answered the call` : `${cmdMeta.order} has no active streak`,
          message:   isDormant
            ? `${cmdMeta.order} has not yet opened its ledger. No XP, no rank, no multiplier.`
            : `${cmdMeta.order} is active but has no streak. A 7-day streak unlocks the ×1.1 multiplier.`,
          action:    `Run any ${cmdMeta.order} skill.`,
          xp_at_stake: 15,
          score: isDormant ? 600 : 150,
        });
      }
      // 3. Near rank-up (within 25 XP of next tier)
      if (xpAway !== null && xpAway > 0 && xpAway <= 25) {
        const nextRank = divRanks[tierIdx + 1] || 'next rank';
        directives.push({
          type:      'near_rank_up',
          urgency:   'high',
          division:  key,
          commander: cmdMeta.name,
          order:     cmdMeta.order,
          title:     `${cmdMeta.order} is ${xpAway} XP from ascension`,
          message:   `${cmdMeta.name} is ${xpAway} XP away from ${nextRank}. A single skill run may close the gap.`,
          action:    `Run a ${cmdMeta.order} skill — ${xpAway} XP needed.`,
          xp_at_stake: xpAway,
          next_rank: nextRank,
          score: 500 + (26 - xpAway) * 5,
        });
      }
    }

    // Sort by score descending
    directives.sort((a, b) => b.score - a.score);

    // No directives = realm is healthy
    if (directives.length === 0) {
      const topDiv = ALL_DIVISION_KEYS.reduce((best, k) => {
        const s = (streaks[k] || {}).current || 0;
        return s > ((streaks[best] || {}).current || 0) ? k : best;
      }, ALL_DIVISION_KEYS[0]);
      const topStreak = (streaks[topDiv] || {}).current || 0;
      const topCmd = DIV_COMMANDERS[topDiv] || {};
      directives.push({
        type: 'realm_healthy', urgency: 'none',
        division: topDiv, commander: topCmd.name, order: topCmd.order,
        title: 'Realm is in good order',
        message: topStreak > 0
          ? `${topCmd.order} holds a ${topStreak}-day battle rhythm. All orders are active.`
          : 'All orders are active. No urgent actions.',
        action: 'Continue current pace.',
        xp_at_stake: 0, score: 0,
      });
    }

    jsonOk(res, {
      primary:   directives[0]   || null,
      secondary: directives.slice(1, 3),
      generated_at: new Date().toISOString(),
    });
  } catch(e) { jsonError(res, 500, 'directive error: ' + e.message); }
}

// GET /mobile/api/realm/chronicle
// Returns recent chronicle entries (newest first).
function handleRealmChronicle(res) {
  try {
    const chroniclePath = path.join(STATE_DIR, 'realm-chronicle.jsonl');
    const limitStr = '25';
    const limit = parseInt(limitStr, 10) || 25;
    if (!fs.existsSync(chroniclePath)) return jsonOk(res, { entries: [], total: 0 });
    const lines = fs.readFileSync(chroniclePath, 'utf8').split('\n').filter(Boolean);
    const entries = [];
    for (const line of lines) {
      try { entries.push(JSON.parse(line)); } catch(e) {}
    }
    entries.reverse();
    jsonOk(res, { entries: entries.slice(0, limit), total: entries.length });
  } catch(e) { jsonError(res, 500, 'chronicle error'); }
}

// ── Animation queue ──────────────────────────────────────────────────────────

// GET /mobile/api/anim/queue  — returns pending animation events
function handleAnimQueue(res) {
  try {
    const qp = path.join(STATE_DIR, 'anim-queue.json');
    if (!fs.existsSync(qp)) return jsonOk(res, { queue: [], count: 0 });
    const queue = JSON.parse(fs.readFileSync(qp, 'utf8') || '[]');
    jsonOk(res, { queue, count: queue.length });
  } catch(e) { jsonError(res, 500, 'anim queue read error'); }
}

// POST /mobile/api/anim/queue/clear  — empties the queue after viewing
function handleAnimQueueClear(res) {
  try {
    const qp = path.join(STATE_DIR, 'anim-queue.json');
    fs.writeFileSync(qp, '[]');
    jsonOk(res, { ok: true });
  } catch(e) { jsonError(res, 500, 'anim queue clear error'); }
}

// ── WebAuthn credential storage ──────────────────────────────────────────────
const WEBAUTHN_FILE = path.join(STATE_DIR, 'webauthn-credentials.json');
const _webauthnChallenges = new Map(); // in-memory: token → { challenge, expires }

function _waLoadCredentials() {
  try {
    if (fs.existsSync(WEBAUTHN_FILE)) return JSON.parse(fs.readFileSync(WEBAUTHN_FILE, 'utf8'));
  } catch(e) {}
  return { credentials: [] };
}

function _waSaveCredentials(data) {
  fs.writeFileSync(WEBAUTHN_FILE, JSON.stringify(data, null, 2));
}

function defaultStoryState() {
  return {
    chapter: 0,
    chapter_key: 'prologue',
    chapter_label: 'Prologue',
    chapter_title: 'The Awakening',
    chapter_summary: 'The realm stirs. The commanders are watching to learn what kind of sovereign J_Claw will become.',
    active_arc: {
      id: 'balanced',
      label: 'Balanced Doctrine',
      name: 'The Measured Ascent',
      summary: 'The sovereign is still unproven. The realm has not committed to a doctrine yet.',
    },
    relationships: {},
    recent_scenes: [],
    choices: [],
    pending_choice: null,
  };
}

// GET /mobile/api/webauthn/register/options
function handleWebAuthnRegisterOptions(res) {
  const challenge = require('crypto').randomBytes(32).toString('base64url');
  const token     = require('crypto').randomBytes(16).toString('hex');
  _webauthnChallenges.set(token, { challenge, expires: Date.now() + 120_000 });
  jsonOk(res, {
    token,
    challenge,
    rp:   { id: 'localhost', name: 'J_Claw Mission Control' },
    user: { id: 'amNsYXctb3duZXI', name: 'owner', displayName: 'J_Claw Owner' },
    pubKeyCredParams: [
      { type: 'public-key', alg: -7  },  // ES256
      { type: 'public-key', alg: -257 }, // RS256
    ],
    authenticatorSelection: {
      authenticatorAttachment: 'platform',
      userVerification:        'required',
      residentKey:             'preferred',
    },
    timeout: 60000,
    attestation: 'none',
  });
}

// POST /mobile/api/webauthn/register/complete
async function handleWebAuthnRegisterComplete(req, res) {
  try {
    const body  = await parseBody(req);
    const entry = _webauthnChallenges.get(body.token);
    if (!entry || Date.now() > entry.expires) return jsonError(res, 400, 'Challenge expired — try again');
    _webauthnChallenges.delete(body.token);
    if (!body.credential_id) return jsonError(res, 400, 'Missing credential_id');

    const data = _waLoadCredentials();
    // Replace any existing credential (one per device is enough)
    data.credentials = data.credentials.filter(c => c.id !== body.credential_id);
    data.credentials.push({
      id:         body.credential_id,
      label:      body.label || 'Mobile Biometric',
      registered: new Date().toISOString(),
    });
    _waSaveCredentials(data);
    jsonOk(res, { ok: true, message: 'Biometric registered', credential_count: data.credentials.length });
  } catch(e) { jsonError(res, 500, 'Registration error: ' + e.message); }
}

// GET /mobile/api/webauthn/auth/options  — returns challenge + allowCredentials
function handleWebAuthnAuthOptions(res) {
  const challenge = require('crypto').randomBytes(32).toString('base64url');
  const token     = require('crypto').randomBytes(16).toString('hex');
  _webauthnChallenges.set(token, { challenge, expires: Date.now() + 90_000 });
  const data = _waLoadCredentials();
  jsonOk(res, {
    token,
    challenge,
    allowCredentials: data.credentials.map(c => ({ type: 'public-key', id: c.id })),
    userVerification: 'required',
    timeout:          15000,
  });
}

// DELETE /mobile/api/webauthn/credentials  — clear all registered credentials
function handleWebAuthnClearCredentials(res) {
  try {
    _waSaveCredentials({ credentials: [] });
    jsonOk(res, { ok: true, message: 'All biometric credentials cleared' });
  } catch(e) { jsonError(res, 500, 'Clear error: ' + e.message); }
}

// GET /mobile/api/webauthn/credentials  — list registered credentials
function handleWebAuthnListCredentials(res) {
  const data = _waLoadCredentials();
  jsonOk(res, { credentials: data.credentials, count: data.credentials.length });
}

// GET /mobile/api/story/state  — current story state + choices made
function handleStoryState(res) {
  try {
    const sp = path.join(STATE_DIR, 'story-state.json');
    if (!fs.existsSync(sp)) return jsonOk(res, defaultStoryState());
    jsonOk(res, JSON.parse(fs.readFileSync(sp, 'utf8') || '{}'));
  } catch(e) { jsonError(res, 500, 'story state error'); }
}

// POST /mobile/api/story/choice  — record a player story choice
async function handleStoryChoice(req, res) {
  try {
    const body = await parseBody(req);
    const division = body.division || '';
    const choiceId = body.choice_id || '';
    const choiceText = body.choice_text || '';
    if (!division || !choiceId) return jsonError(res, 400, 'division and choice_id required');
    const { result } = await runRealmKeeperTask('story-choice', [division, choiceId, choiceText]);
    jsonOk(res, { ok: true, state: result });
  } catch(e) { jsonError(res, 500, 'story choice error'); }
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
    res.writeHead(204, { 'Access-Control-Allow-Origin': corsOrigin(req), 'Access-Control-Allow-Methods': 'GET,POST', 'Access-Control-Allow-Headers': 'Content-Type,Authorization' });
    res.end(); return;
  }

  if (reqPath.startsWith('/api/')) {
    try {
      if (method === 'POST' && reqPath === '/api/bestow') {
        const body = await parseBody(req); return await handleBestowViaPython(body, res);
      }
      if (method === 'POST' && reqPath === '/api/control') {
        const body = await parseBody(req); return handleControl(body, res);
      }
      if (method === 'GET' && reqPath === '/api/gamif/stream') { return handleGamifStream(req, res); }
      if (method === 'GET' && reqPath === '/api/stats')         { return jsonOk(res, syncBaseProgress(readState('jclaw-stats.json') || {})); }
      if (method === 'GET' && reqPath === '/api/stats/summary') { return handleStatsSummary(res); }
      if (method === 'POST' && reqPath === '/api/prestige') { return await handlePrestigeViaPython(res); }
      if (method === 'GET' && reqPath === '/api/jobs') { return handleGetJobs(res); }
      if (method === 'GET' && reqPath === '/api/grants') { return handleGetGrants(res); }
      if (method === 'GET' && reqPath === '/api/packets') { return handleGetPackets(res); }
      if (method === 'GET' && reqPath === '/api/trading/cycle') { return handleGetTradingCycle(res); }
      if (method === 'GET' && reqPath === '/api/trading/accounts') { return handleGetTradingAccounts(res); }
      if (method === 'GET' && reqPath === '/api/trading/cycle/status') { return proxyZenith('GET', '/status', null, res); }
      if (method === 'GET' && reqPath === '/api/trading/agent-network/start') {
        try {
          const result = await mobileStartAgentNetwork();
          return jsonOk(res, result);
        } catch(e) {
          return jsonError(res, 500, e.message);
        }
      }
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
  if (reqPath === '/mobile/sw.js') {
    return serveStatic('/mobile/sw.js', res);
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
          'Access-Control-Allow-Origin':  corsOrigin(req),
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
        const s = syncBaseProgress(readState('jclaw-stats.json') || {});
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
      if (method === 'GET' && reqPath === '/mobile/api/stats/summary') { return handleStatsSummary(res); }
      if (method === 'GET' && reqPath === '/mobile/api/realm/config')    { return handleRealmConfig(res); }
      if (method === 'GET' && reqPath === '/mobile/api/realm/directive') { return handleRealmDirective(res); }
      if (method === 'GET' && reqPath === '/mobile/api/realm/chronicle') { return handleRealmChronicle(res); }
      if (method === 'GET'  && reqPath === '/mobile/api/anim/queue')       { return handleAnimQueue(res); }
      if (method === 'POST' && reqPath === '/mobile/api/anim/queue/clear') { return handleAnimQueueClear(res); }
      if (method === 'GET'  && reqPath === '/mobile/api/story/state')      { return handleStoryState(res); }
      if (method === 'POST' && reqPath === '/mobile/api/story/choice')     { return handleStoryChoice(req, res); }
      if (method === 'GET'  && reqPath === '/mobile/api/webauthn/register/options')   { return handleWebAuthnRegisterOptions(res); }
      if (method === 'POST' && reqPath === '/mobile/api/webauthn/register/complete')  { return handleWebAuthnRegisterComplete(req, res); }
      if (method === 'GET'  && reqPath === '/mobile/api/webauthn/auth/options')       { return handleWebAuthnAuthOptions(res); }
      if (method === 'GET'  && reqPath === '/mobile/api/webauthn/credentials')        { return handleWebAuthnListCredentials(res); }
      if (method === 'DELETE' && reqPath === '/mobile/api/webauthn/credentials')      { return handleWebAuthnClearCredentials(res); }
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
      if (method === 'GET' && reqPath === '/mobile/api/battles/today') {
        return handleMobileBattlesToday(res);
      }
      if (method === 'GET' && reqPath === '/mobile/api/battles/week') {
        return handleMobileBattlesWeek(res);
      }
      if (method === 'GET' && reqPath === '/mobile/api/briefing') {
        return handleMobileBriefing(res);
      }
      if (method === 'GET' && reqPath === '/mobile/api/jobs') {
        return handleMobileJobs(req, res);
      }
      if (method === 'POST' && reqPath === '/mobile/api/health-checkin') {
        const body = await parseBody(req);
        return handleHealthCheckin(req, res, body);
      }
      if (method === 'GET' && reqPath === '/mobile/api/coding-history') {
        return handleCodingHistory(res);
      }
      if (method === 'GET' && reqPath === '/mobile/api/trading/signals') {
        return handleTradingSignals(res);
      }
      if (method === 'GET' && reqPath === '/mobile/api/achievements') {
        return handleAchievements(res);
      }
      {
        let _m;
        if (method === 'POST' && (_m = reqPath.match(/^\/mobile\/api\/jobs\/(.+)\/action$/))) {
          const body = await parseBody(req);
          return handleJobAction(req, res, decodeURIComponent(_m[1]), body);
        }
        if (method === 'GET' && (_m = reqPath.match(/^\/mobile\/api\/division\/(.+)\/report$/))) {
          return handleDivisionReport(req, res, decodeURIComponent(_m[1]));
        }
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
        const a = Buffer.from(stored.hash), b = Buffer.from(hash.padEnd(stored.hash.length, '\0').slice(0, stored.hash.length));
        const match = a.length === b.length && crypto.timingSafeEqual(a, b) && stored.hash === hash;
        if (match) return jsonOk(res, { ok: true });
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

      // ── Activity log feed ────────────────────────────────────────────────────
      if (method === 'GET' && reqPath === '/mobile/api/activity') {
        try {
          const log = readState('activity-log.json') || { entries: [] };
          const entries = (log.entries || []).slice(-100).reverse();
          return jsonOk(res, { entries });
        } catch(e) {
          return jsonOk(res, { entries: [] });
        }
      }

      // ── ComfyUI: status check ─────────────────────────────────────────────────
      if (method === 'GET' && reqPath === '/mobile/api/comfyui/status') {
        try {
          const http = require('http');
          await new Promise((resolve, reject) => {
            const req = http.get('http://127.0.0.1:8188', { timeout: 2500 }, res => resolve(res));
            req.on('error', reject);
            req.on('timeout', () => { req.destroy(); reject(new Error('timeout')); });
          });
          return jsonOk(res, { online: true, url: 'http://127.0.0.1:8188' });
        } catch(_) {
          let batPath = 'C:\\ComfyUI\\run_amd_gpu.bat';
          try {
            const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'divisions/production/config.json'), 'utf8'));
            if (cfg.comfyui_path) batPath = cfg.comfyui_path;
          } catch(_) {}
          return jsonOk(res, { online: false, bat_exists: fs.existsSync(batPath), bat_path: batPath });
        }
      }

      // ── Skills: trigger a skill from mobile ──────────────────────────────────
      if (method === 'POST' && reqPath === '/mobile/api/skills/trigger') {
        const body = await parseBody(req);
        const skill = (body.skill || '').trim();
        if (!skill) return jsonError(res, 400, 'skill required');
        if (!SKILL_TASK_MAP[skill]) return jsonError(res, 400, `Unknown skill: ${skill}`);
        // Support optional params (e.g. prompt for image-generate)
        // image-generate / sprite-generate expect positional args: asset_type, commander, subject
        let extraArgs = [];
        if (body.params?.prompt && ['image-generate', 'sprite-generate'].includes(skill)) {
          extraArgs = ['portrait_bust', 'generic', body.params.prompt];
        } else if (body.params) {
          extraArgs = [JSON.stringify(body.params)];
        }
        if (body.params?.prompt) {
          logActivity('MOBILE', `Skill triggered via mobile: ${skill} (with prompt)`, 'blue');
        } else {
          logActivity('MOBILE', `Skill triggered via mobile: ${skill}`, 'blue');
        }
        // Fire-and-forget — don't await
        runSkillViaPython(skill, body.division || 'MOBILE', extraArgs).then(ok => {
          broadcastWS('task_completed', { skill, ok });
        });
        return jsonOk(res, { ok: true, message: `${skill} queued` });
      }

      // ── Skills: run a combo (sequential multi-skill workflow) ─────────────────
      if (method === 'POST' && reqPath === '/mobile/api/skills/combo') {
        const body   = await parseBody(req);
        const skills     = (body.skills || []).filter(s => typeof s === 'string' && s.trim());
        const name       = (body.name || 'Combo').trim();
        const prompt     = body.prompt || null;
        const assetType  = body.assetType || 'portrait_bust';
        if (!skills.length) return jsonError(res, 400, 'skills required');
        for (const s of skills) {
          if (!SKILL_TASK_MAP[s]) return jsonError(res, 400, `Unknown skill: ${s}`);
        }
        logActivity('MOBILE', `Combo triggered via mobile: ${name} (${skills.join(' → ')})`, 'blue');
        // Run sequentially in background
        (async () => {
          for (const skill of skills) {
            broadcastWS('task_started', { skill, combo: name });
            const extraArgs = (prompt && ['image-generate','sprite-generate'].includes(skill))
              ? [assetType, 'generic', prompt] : [];
            const ok = await runSkillViaPython(skill, 'MOBILE', extraArgs);
            broadcastWS('task_completed', { skill, ok, combo: name });
            if (!ok) {
              logActivity('MOBILE', `Combo ${name} failed at ${skill}`, 'red');
              broadcastWS('combo_completed', { name, skills, success: false, failed_at: skill });
              return;
            }
          }
          logActivity('MOBILE', `Combo ${name} complete`, 'green');
          broadcastWS('combo_completed', { name, skills, success: true });
        })();
        return jsonOk(res, { ok: true, message: `${name} started` });
      }

      // ── Alerts: action (dismiss/snooze/escalate) ──────────────────────────────
      {
        let _m;
        if (method === 'POST' && (_m = reqPath.match(/^\/mobile\/api\/alerts\/(.+)\/action$/))) {
          const alertId = decodeURIComponent(_m[1]);
          const body    = await parseBody(req);
          const action  = (body.action || '').toLowerCase();

          if (action === 'dismiss') {
            const dismissed = _loadDismissedAlerts();
            dismissed.add(alertId);
            _saveDismissedAlerts(dismissed);
            _broadcastAlertUpdate();
            broadcastWS('division_status_changed', { alert_id: alertId, action: 'dismissed' });
            return jsonOk(res, { ok: true, message: 'Alert dismissed' });
          }

          if (action === 'snooze') {
            const snoozed = _loadSnoozedAlerts();
            snoozed[alertId] = new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString();
            _saveSnoozedAlerts(snoozed);
            broadcastWS('division_status_changed', { alert_id: alertId, action: 'snoozed' });
            return jsonOk(res, { ok: true, message: 'Alert snoozed 24h' });
          }

          if (action === 'escalate') {
            // Write escalation note to state
            const escalations = readState('escalated-alerts.json') || [];
            escalations.push({ id: alertId, escalated_at: new Date().toISOString() });
            writeState('escalated-alerts.json', escalations);
            logActivity('MOBILE', `Alert escalated: ${alertId}`, 'red');
            broadcastWS('division_status_changed', { alert_id: alertId, action: 'escalated', severity: 'HIGH' });
            return jsonOk(res, { ok: true, message: 'Alert escalated to HIGH' });
          }

          return jsonError(res, 400, 'Invalid action — use dismiss, snooze, or escalate');
        }
      }

      // ── Characters: use-shield ────────────────────────────────────────────────
      {
        let _m;
        if (method === 'POST' && (_m = reqPath.match(/^\/mobile\/api\/characters\/(.+)\/use-shield$/))) {
          const divKey = decodeURIComponent(_m[1]);
          try {
            const stats = readState('jclaw-stats.json');
            if (!stats) return jsonError(res, 500, 'stats not loaded');
            _ensureStreaks(stats);
            const streak = stats.streaks[divKey];
            if (!streak) return jsonError(res, 400, `Unknown division: ${divKey}`);
            const sa = stats.streak_shield_available || 0;
            if (sa <= 0) return jsonError(res, 400, 'No streak shields available');
            if (stats.streak_shield_used) return jsonError(res, 400, 'Shield already used this week');
            streak.shield_this_week = true;
            streak.last_date = new Date().toISOString().slice(0, 10); // counts today
            stats.streak_shield_available = Math.max(0, sa - 1);
            stats.streak_shield_used = true;
            writeState('jclaw-stats.json', stats);
            logActivity(divKey, `Streak shield used for ${divKey}`, 'blue');
            broadcastWS('xp_gained', { division: divKey, shield_used: true });
            return jsonOk(res, { ok: true, message: `Streak shield activated for ${divKey}` });
          } catch(e) { return jsonError(res, 500, e.message); }
        }

        // ── Characters: bestow-xp ──────────────────────────────────────────────
        if (method === 'POST' && (_m = reqPath.match(/^\/mobile\/api\/characters\/(.+)\/bestow-xp$/))) {
          const divKey = decodeURIComponent(_m[1]);
          const body   = await parseBody(req);
          const amount = parseInt(body.amount) || 0;
          const flavor = (body.flavor || '').slice(0, 200);
          if (amount <= 0 || amount > 200) return jsonError(res, 400, 'Amount must be 1–200');
          const validDivs = new Set(['opportunity', 'trading', 'dev_automation', 'personal', 'op_sec', 'production']);
          if (!validDivs.has(divKey)) return jsonError(res, 400, `Unknown division: ${divKey}`);
          try {
            const { result } = await runRealmKeeperTask('grant-division', [divKey, amount, 'mobile-bestow', flavor]);
            logActivity(divKey, `Mobile bestow: +${amount} XP ${flavor ? '- ' + flavor : ''}`, 'purple');
            return jsonOk(res, {
              ok: true,
              message: `+${result.xp_granted || amount} XP bestowed on ${divKey}`,
              division_xp: result.division_xp,
              division_rank: result.division_rank,
              achievements_unlocked: result.new_achievements || [],
            });
          } catch(e) { return jsonError(res, 500, e.message); }
        }
      }

      // ── Push subscription ─────────────────────────────────────────────────────
      if (method === 'GET' && reqPath === '/mobile/api/push/vapid-key') {
        if (!VAPID_KEYS) return jsonError(res, 503, 'Push not configured — run: npm install web-push');
        return jsonOk(res, { publicKey: VAPID_KEYS.publicKey });
      }
      if (method === 'POST' && reqPath === '/mobile/api/push-subscribe') {
        if (!webpush || !VAPID_KEYS) return jsonError(res, 503, 'Push not configured');
        const body = await parseBody(req);
        const sub  = body.subscription;
        if (!sub || !sub.endpoint) return jsonError(res, 400, 'Invalid subscription');
        _pushSubscriptions.add(JSON.stringify(sub));
        _savePushSubs();
        return jsonOk(res, { ok: true, message: 'Push subscription registered' });
      }
      if (method === 'POST' && reqPath === '/mobile/api/push-test') {
        if (!webpush || !VAPID_KEYS) return jsonError(res, 503, 'Push not configured');
        const payload = JSON.stringify({ title: 'Mission Control', body: 'Push test successful — realm connection active' });
        let sent = 0;
        for (const subStr of _pushSubscriptions) {
          try {
            await webpush.sendNotification(JSON.parse(subStr), payload);
            sent++;
          } catch(e) { _pushSubscriptions.delete(subStr); _savePushSubs(); }
        }
        return jsonOk(res, { ok: true, sent });
      }

      // ── Generated Images: list recent images from generated assets dir ──────
      if (method === 'GET' && reqPath === '/mobile/api/generated-images') {
        const generatedRoot = path.join(ROOT, 'mobile', 'assets', 'generated');
        const results = [];
        try {
          // Walk commander dirs -> asset_type dirs -> files
          const commanders = fs.readdirSync(generatedRoot).filter(d => {
            try { return fs.statSync(path.join(generatedRoot, d)).isDirectory(); } catch(_) { return false; }
          });
          for (const cmd of commanders) {
            const cmdDir = path.join(generatedRoot, cmd);
            const assetTypes = fs.readdirSync(cmdDir).filter(d => {
              try { return fs.statSync(path.join(cmdDir, d)).isDirectory(); } catch(_) { return false; }
            });
            for (const at of assetTypes) {
              const atDir = path.join(cmdDir, at);
              const files = fs.readdirSync(atDir).filter(f => /\.(png|jpg|webp)$/i.test(f));
              for (const f of files) {
                const stat = fs.statSync(path.join(atDir, f));
                results.push({
                  url: `/mobile/assets/generated/${cmd}/${at}/${f}`,
                  filename: f, commander: cmd, assetType: at,
                  size_kb: Math.round(stat.size / 1024),
                  created_at: stat.birthtimeMs || stat.ctimeMs,
                });
              }
            }
          }
        } catch(_e) { /* dir may not exist yet */ }
        // Sort newest first
        results.sort((a, b) => b.created_at - a.created_at);
        return jsonOk(res, { ok: true, images: results.slice(0, 50) });
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
    const runId = simpleId();
    const gameEventOffset = fileSizeSafe(GAME_EVENTS_FILE);

    const runDivisionPath = path.join(ROOT, 'run_division.py');
    const proc = spawn(PYTHON_EXE, [runDivisionPath, mapping.division, mapping.task, ...extraArgs], {
      env: { ...process.env, JCLAW_RUN_ID: runId },
      windowsHide: true,
      cwd: ROOT,
    });

    let stderr = '';
    let stdout = '';
    proc.stderr.on('data', d => { stderr += d.toString(); });
    proc.stdout.on('data', d => { stdout += d.toString(); });

    proc.on('close', code => {
      updateDivisionState(mapping.divState, 'idle');
      if (code === 0) {
        logActivity(logDiv || 'SYS', `${skillName} complete`, 'green');
        const canonicalEvents = readGameEventsSince(gameEventOffset).filter(evt => evt.run_id === runId);
        if (canonicalEvents.length) {
          canonicalEvents.forEach(_consumeCanonicalGameEvent);
        } else {
          handleGamifCheck(skillName, mapping.divState);
        }
        // For image/sprite generation, read the packet and attach image URLs to the WS event
        let generatedImages = [];
        if (['image-generate', 'sprite-generate'].includes(skillName)) {
          try {
            const packetPath = path.join(ROOT, 'divisions', 'production', 'packets', `${skillName}.json`);
            const packet = JSON.parse(fs.readFileSync(packetPath, 'utf8'));
            const filenames = packet?.metrics?.filenames || [];
            const assetType = packet?.metrics?.asset_type || 'portrait_bust';
            const commander = packet?.metrics?.commander || 'generic';
            generatedImages = filenames.map(f => `/mobile/assets/generated/${commander}/${assetType}/${f}`);
          } catch(_e) { /* packet may not be written yet */ }
        }
        broadcastWS('task_completed', { skill: skillName, division: mapping.divState, ok: true, images: generatedImages });
        broadcastWS('division_status_changed', { division: mapping.divState, status: 'idle' });
        resolve(true);
      } else {
        const errLine = stderr.split('\n').filter(l => l.includes('ERROR') || l.includes('FAILED')).pop()
          || `exit ${code}`;
        logActivity(logDiv || 'SYS', `${skillName} failed — ${errLine.trim()}`, 'red');
        broadcastWS('division_status_changed', { division: mapping.divState, status: 'error' });
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
    const divs = ['opportunity', 'trading', 'personal', 'dev-automation', 'op-sec', 'production', 'sentinel'];
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

// Virtual trader daily at 6:00 PM — must run first (produces trade data for backtester/report)
cron.schedule('0 18 * * *', async () => {
  await runSkillViaPython('virtual-trader', 'TRADING');
}, { timezone: TZ });

// Backtester daily at 6:05 PM — runs after virtual-trader produces trade data
cron.schedule('5 18 * * *', async () => {
  await runSkillViaPython('backtester', 'TRADING');
}, { timezone: TZ });

// Trading performance report daily at 6:10 PM — runs last, summarises both above
cron.schedule('10 18 * * *', async () => {
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
  await runSkillViaPython('security-scan', 'OP_SEC');
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

// ── Realm chronicle retroactive migration (runs once on first boot) ───────────
setImmediate(() => {
  try {
    const chroniclePath = path.join(STATE_DIR, 'realm-chronicle.jsonl');
    if (!fs.existsSync(chroniclePath) || fs.statSync(chroniclePath).size === 0) {
      const histPath  = path.join(STATE_DIR, 'xp-history.jsonl');
      if (!fs.existsSync(histPath)) return;
      const lines     = fs.readFileSync(histPath, 'utf8').split('\n').filter(Boolean);
      const divXp     = {};
      const divTier   = {};
      let written     = 0;
      const entries   = [];

      for (const line of lines) {
        try {
          const e = JSON.parse(line);
          if (e.event === 'skill_complete' && e.div && e.xp) {
            const div    = e.div;
            const oldXp  = divXp[div] || 0;
            const newXp  = oldXp + e.xp;
            const oldT   = divTier[div] || 0;
            const newT   = DIV_XP_THRESHOLDS.filter(t => newXp >= t).length - 1;
            if (newT > oldT) {
              const ranks  = DIV_RANKS[div] || [];
              const cmd    = DIV_COMMANDERS[div] || {};
              const order  = cmd.order || div;
              const rank   = ranks[newT] || 'unknown';
              const bonus  = RANK_UP_BASE_XP[newT] || 0;
              entries.push(JSON.stringify({
                ts: e.ts || new Date().toISOString(),
                event_class: newT === 4 ? 'major' : 'micro',
                category:    'rank_up', division: div,
                commander:   cmd.name || div, order,
                tier:        newT, title: `${order} — ${rank}`,
                lore:        `${cmd.name || div} and the ${order} crossed the ${DIV_XP_THRESHOLDS[newT]} XP threshold.`,
                operational: `${order} advanced to ${rank}`,
                impact:      bonus > 0 ? `+${bonus} base XP granted to J_Claw.` : '',
                retroactive: true,
              }));
              written++;
              divTier[div] = newT;
            }
            divXp[div] = newXp;
          } else if (e.event === 'ruler_bestow' && e.amount) {
            entries.push(JSON.stringify({
              ts: e.ts || new Date().toISOString(),
              event_class: 'micro', category: 'ruler_reward',
              amount: e.amount, reason: e.reason || '',
              title: `Sovereign's Decree — ${e.amount} XP`,
              lore: `Matthew granted ${e.amount} base XP to J_Claw. ${e.reason || ''}`,
              operational: `+${e.amount} base XP`, impact: '', retroactive: true,
            }));
            written++;
          }
        } catch(_) {}
      }

      if (entries.length > 0) {
        fs.writeFileSync(chroniclePath, entries.join('\n') + '\n', 'utf8');
        console.log(`  Realm Chronicle: ${written} retroactive events written.`);
      }
    }
  } catch(e) { console.error('  Chronicle migration error:', e.message); }
});

server.listen(PORT, '0.0.0.0', () => {
  console.log('');
  console.log('  ==========================================');
  console.log('   OpenClaw // Mission Control');
  console.log('  ==========================================');
  console.log('');
  console.log('  Server    : http://localhost:' + PORT);
  console.log('  Dashboard : http://localhost:' + PORT + '/dashboard');
  console.log('');
  console.log('  Scheduler : node-cron active — full SOUL.md schedule (25 crons)');
  console.log('  Queue     : polling every 2 min (zero-cost when idle)');
  console.log('  Timezone  : America/Halifax');
  console.log('');
  console.log('  For persistence across reboots:');
  console.log('    npm i -g pm2 && pm2 start server.js --name openclaw');
  console.log('    pm2 startup  &&  pm2 save');
  console.log('');
});

// ── WebSocket server (attaches to same HTTP server, same port) ──
if (WebSocketServer) {
  const wss = new WebSocketServer({ server, path: '/mobile/ws' });
  wss.on('connection', (ws, req) => {
    const mobileToken = process.env.MOBILE_TOKEN || '';
    // Auth: check token param
    try {
      const params = new url.URL('http://x' + req.url).searchParams;
      const provided = params.get('token') || '';
      const clientIP = req.socket.remoteAddress || '';
      const isLocalhost = clientIP === '::1' || clientIP === '127.0.0.1' || clientIP === '::ffff:127.0.0.1';
      if (!isLocalhost) {
        if (!mobileToken) {
          ws.close(4003, 'Mobile access not configured');
          return;
        }
        if (!provided || !crypto.timingSafeEqual(
          Buffer.from(provided.padEnd(64, '\0')),
          Buffer.from(mobileToken.padEnd(64, '\0'))
        )) {
          ws.close(4001, 'Unauthorized');
          return;
        }
      }
    } catch(e) { ws.close(4001, 'Bad request'); return; }

    _wsClients.add(ws);
    ws.on('close', () => _wsClients.delete(ws));
    ws.on('error', () => _wsClients.delete(ws));
    // Send hello
    try { ws.send(JSON.stringify({ type: 'connected', ts: new Date().toISOString() })); } catch(e) {}
  });
  console.log('  WebSocket : ws://localhost:' + PORT + '/mobile/ws');
} else {
  console.log('  WebSocket : DISABLED (run npm install)');
}
