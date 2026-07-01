import { useState, useEffect, useRef } from 'react';
import { Power, Activity, DollarSign, RotateCcw, Download } from 'lucide-react';

const API_URL = '/api';
const FETCH_TIMEOUT_MS = 4000;
const ACTION_TIMEOUT_MS = 20000;
const RESET_TIMEOUT_MS = 120000;

/** Always [[step amounts...]] — handles flat [1,3,9] from older API responses. */
function normalizeBudgetTiers(raw) {
  if (!Array.isArray(raw) || raw.length === 0) {
    return [[1, 3, 9, 25, 80, 180, 402]];
  }
  const nested = typeof raw[0] === 'number' ? [raw] : raw;
  return nested.map((tier) =>
    (Array.isArray(tier) ? tier : [tier]).map((n) => Math.max(1, Number(n) || 1))
  );
}

function formatApiErrorDetail(detail) {
  if (!detail) return 'Save failed';
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((e) => {
        if (typeof e === 'string') return e;
        const loc = Array.isArray(e.loc) ? e.loc.join('.') : '';
        return e.msg ? `${loc ? `${loc}: ` : ''}${e.msg}` : JSON.stringify(e);
      })
      .join('; ');
  }
  return JSON.stringify(detail);
}

async function apiFetch(path, options = {}, timeoutMs = FETCH_TIMEOUT_MS) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(`${API_URL}${path}`, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timeoutId);
  }
}

function formatLadder(tiers) {
  if (!Array.isArray(tiers)) return '';
  return tiers
    .map((tier, i) => `T${i + 1}: ${tier.map((n) => `$${n}`).join(' → ')}`)
    .join(' | ');
}

/** Match dropdown to the active IQ balance (required when multiple tournaments exist). */
function resolveActiveAccountId(status, accounts) {
  if (!accounts?.length) return '';
  if (status?.balance_id != null && status.balance_id !== '') {
    const byBalance = accounts.find((a) => String(a.id) === String(status.balance_id));
    if (byBalance) return String(byBalance.id);
  }
  if (status?.account_key?.startsWith('TOURNAMENT_')) {
    const tid = status.account_key.replace('TOURNAMENT_', '');
    const byKey = accounts.find((a) => String(a.id) === tid);
    if (byKey) return String(byKey.id);
  }
  const byType = accounts.find((a) => a.type === status?.account_type);
  return byType ? String(byType.id) : '';
}

function App() {
  const [status, setStatus] = useState(null);
  const [config, setConfig] = useState(null);
  const [isToggling, setIsToggling] = useState(false);
  const [accounts, setAccounts] = useState([]);
  const [actionError, setActionError] = useState('');
  const [showRealConfirm, setShowRealConfirm] = useState(false);
  const [showResetConfirm, setShowResetConfirm] = useState(false);
  const [isResetting, setIsResetting] = useState(false);
  const [clearLogOnReset, setClearLogOnReset] = useState(true);
  const [trades, setTrades] = useState([]);
  const [assetList, setAssetList] = useState([]);
  const [editTiers, setEditTiers] = useState(null);
  const [editAutoBracket, setEditAutoBracket] = useState(true);
  const [tierSaveMsg, setTierSaveMsg] = useState('');
  const [balanceTierBrackets, setBalanceTierBrackets] = useState([]);
  const [isRefreshingBalance, setIsRefreshingBalance] = useState(false);
  const [expandedTradeIndex, setExpandedTradeIndex] = useState(null);
  const prevRunningRef = useRef(null);


  const [setupStatus, setSetupStatus] = useState(null);
  const [showSetupWizard, setShowSetupWizard] = useState(false);
  const [wizardData, setWizardData] = useState({ iq_email: '', iq_password: '', iq_account_type: 'PRACTICE' });
  const [wizardLoading, setWizardLoading] = useState(false);
  const [wizardError, setWizardError] = useState('');
  const [wizardDone, setWizardDone] = useState(false);

  const loadAssetList = async () => {
    try {
      const res = await apiFetch('/assets');
      if (!res.ok) return;
      const data = await res.json();
      const list = data.open_assets || [];
      if (list.length) setAssetList(list);
    } catch (_) { /* ignore */ }
  };

  useEffect(() => {
    const fetchStatus = async () => {
      try {
        const res = await apiFetch('/status');
        if (res.ok) {
          const data = await res.json();
          setStatus(data);
        }
      } catch (err) {
        if (err.name !== 'AbortError') console.error('status:', err);
      }
    };
    fetchStatus();
    const interval = setInterval(fetchStatus, 1500);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    const checkSetup = async () => {
      try {
        const res = await apiFetch('/setup-status', {}, 5000);
        if (!res.ok) return;
        const data = await res.json();
        setSetupStatus(data);
        if (data.needs_setup) setShowSetupWizard(true);
      } catch (_) {}
    };
    checkSetup();
  }, []);

  useEffect(() => {
    const load = async () => {
      try {
        const [configRes, accountsRes, tradesRes] = await Promise.all([
          apiFetch('/config'),
          apiFetch('/accounts'),
          apiFetch('/trades?limit=15'),
        ]);
        const cfg = await configRes.json();
        setConfig(cfg);
        setBalanceTierBrackets(cfg.balance_tier_brackets || []);
        setAccounts((await accountsRes.json()).accounts || []);
        setTrades((await tradesRes.json()).trades || []);
        const assetsRes = await apiFetch('/assets');
        if (assetsRes.ok) {
          const data = await assetsRes.json();
          setAssetList(data.open_assets || []);
        }
      } catch (err) {
        console.error('init load:', err);
      }
    };
    load();
    const interval = setInterval(async () => {
      try {
        const res = await apiFetch('/trades?limit=15');
        if (res.ok) setTrades((await res.json()).trades || []);
      } catch (_) { /* ignore */ }
    }, 20000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    if (!status?.connected) return;
    const wasRunning = prevRunningRef.current;
    prevRunningRef.current = status.running;
    if (wasRunning && !status.running) {
      apiFetch('/accounts').then((r) => r.json()).then((d) => setAccounts(d.accounts || []));
    }
    loadAssetList();
  }, [status?.connected, status?.running]);

  useEffect(() => {
    if (!status?.connected) return;
    const refreshAccounts = async () => {
      try {
        const res = await apiFetch('/accounts');
        if (res.ok) setAccounts((await res.json()).accounts || []);
      } catch (_) {}
    };
    const interval = setInterval(refreshAccounts, 30000);
    return () => clearInterval(interval);
  }, [status?.connected]);

  const refreshStatus = async () => {
    try {
      const res = await apiFetch('/status');
      if (res.ok) setStatus(await res.json());
    } catch (_) { /* ignore */ }
  };

  const refreshBalance = async () => {
    if (!status?.connected) {
      setActionError('Connect to IQ Option first');
      return;
    }
    setIsRefreshingBalance(true);
    setActionError('');
    try {
      const res = await apiFetch('/balance/refresh', { method: 'POST' }, ACTION_TIMEOUT_MS);
      const data = await res.json();
      if (!res.ok) {
        setActionError(data.detail || 'Balance refresh failed');
        return;
      }
      setAccounts(data.accounts || []);
      setStatus((prev) =>
        prev ? { ...prev, balance: data.balance, balance_id: data.active_balance_id } : prev
      );
      await refreshStatus();
    } catch (err) {
      setActionError(
        err.name === 'AbortError' ? 'Balance refresh timed out' : 'Balance refresh failed'
      );
    } finally {
      setIsRefreshingBalance(false);
    }
  };


  const handleAccountChange = async (e) => {
    const selectedId = e.target.value;
    const acc = accounts.find((a) => String(a.id) === selectedId);
    if (!acc) return;
    if (status?.running) {
      setActionError('');
      try {
        const stopRes = await apiFetch('/stop', { method: 'POST' }, ACTION_TIMEOUT_MS);
        if (!stopRes.ok) {
          const data = await stopRes.json().catch(() => ({}));
          setActionError(data.detail || 'Stop the bot before switching accounts');
          await refreshStatus();
          return;
        }
        await refreshStatus();
      } catch (_) {
        setActionError('Could not stop bot before account switch');
        return;
      }
    }
    try {
      const res = await apiFetch('/account', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ account_type: acc.type, balance_id: acc.id }),
      }, ACTION_TIMEOUT_MS);
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setActionError(data.detail || 'Account switch failed');
        await refreshStatus();
        return;
      }
      await refreshStatus();
      const accountsRes = await apiFetch('/accounts');
      if (accountsRes.ok) {
        setAccounts((await accountsRes.json()).accounts || []);
      }
    } catch (err) {
      setActionError('Account switch failed');
      console.error('account switch:', err);
    }
  };

  const handleResetProgress = async () => {
    if (!status || isResetting) return;
    setIsResetting(true);
    setActionError('');
    try {
      const needsReal = status.account_type === 'REAL' || status.is_real_account;
      const res = await apiFetch('/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ clear_trade_log: clearLogOnReset, confirm: needsReal }),
      }, RESET_TIMEOUT_MS);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setActionError(data.detail || 'Reset failed');
        return;
      }
      setShowResetConfirm(false);
      await refreshStatus();
    } catch (err) {
      setActionError(err.name === 'AbortError' ? 'Reset timed out' : 'Cannot reach server');
    } finally {
      setIsResetting(false);
    }
  };

  const doStart = async (confirmReal = false) => {
    setIsToggling(true);
    setActionError('');
    setShowRealConfirm(false);
    try {
      const res = await apiFetch('/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm_real: confirmReal }),
      }, ACTION_TIMEOUT_MS);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setActionError(data.detail || 'Start failed');
        await refreshStatus();
        return;
      }
      await refreshStatus();
    } catch (err) {
      setActionError(err.name === 'AbortError' ? 'Start timed out — check status' : 'Cannot reach server');
      await refreshStatus();
    } finally {
      setIsToggling(false);
    }
  };

  const handleStartStop = async () => {
    if (!status || isToggling) return;
    if (status.running) {
      setIsToggling(true);
      setActionError('');
      try {
        const res = await apiFetch('/stop', { method: 'POST' }, ACTION_TIMEOUT_MS);
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          setActionError(data.detail || 'Stop failed');
        }
        await refreshStatus();
      } catch (err) {
        setActionError('Cannot reach server');
      } finally {
        setIsToggling(false);
      }
      return;
    }
    if (status.account_type === 'REAL' || status.is_real_account) {
      setShowRealConfirm(true);
      return;
    }
    await doStart(false);
  };

  const handlePauseResume = async () => {
    if (!status?.running || isToggling) return;
    setIsToggling(true);
    try {
      const endpoint = status.paused ? '/resume' : '/pause';
      const res = await apiFetch(endpoint, { method: 'POST' }, ACTION_TIMEOUT_MS);
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setActionError(data.detail || 'Pause/resume failed');
      }
      await refreshStatus();
    } finally {
      setIsToggling(false);
    }
  };

  const initTierEditor = () => {
    const current = config?.budget_tiers || status?.budget_tiers || [[1, 3, 9, 25, 80, 180, 402]];
    setEditTiers(normalizeBudgetTiers(current));
    setEditAutoBracket(config?.auto_bracket_enabled ?? true);
    setBalanceTierBrackets(config?.balance_tier_brackets || balanceTierBrackets);
    setTierSaveMsg('');
  };

  const defaultBracketPresets = balanceTierBrackets.length > 0
    ? balanceTierBrackets.map((b) => ({
        label: `${b.range_label} (Base $${b.base_amount})`,
        val: b.amounts,
      }))
    : [
        { label: '$1–$1,999 (Base $1)', val: [1, 3, 6, 16, 39, 98, 244, 610, 1526] },
        { label: '$2,000–$9,999 (Base $3)', val: [3, 8, 19, 47, 117, 293, 732, 1831, 4578] },
        { label: '$10,000–$19,999 (Base $9)', val: [9, 23, 56, 141, 352, 879, 2197, 5493, 13733] },
        { label: '$20,000–$49,999 (Base $20)', val: [20, 50, 125, 313, 781, 1953, 4883, 12207, 30518] },
        { label: '$50,000–$149,999 (Base $45)', val: [45, 113, 281, 703, 1758, 4395, 10986, 27466, 68665] },
        { label: '$150,000+ (Base $100)', val: [100, 250, 625, 1563, 3906, 9766, 24414, 61035, 152588] },
      ];

  const saveTiers = async () => {
    if (!editTiers) return;
    if (status?.running) {
      setTierSaveMsg('Stop the bot before saving bracket tiers.');
      return;
    }
    setTierSaveMsg('');
    const cleanedTiers = normalizeBudgetTiers(editTiers);
    try {
      const payload = {
        budget_tiers: cleanedTiers,
        auto_bracket_enabled: editAutoBracket,
      };
      const res = await apiFetch('/bracket-config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }, ACTION_TIMEOUT_MS);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        setTierSaveMsg(formatApiErrorDetail(err.detail));
        return;
      }
      const data = await res.json().catch(() => ({}));
      const savedTiers = normalizeBudgetTiers(data.budget_tiers || cleanedTiers);
      setTierSaveMsg('Tiers saved!');
      setEditTiers(savedTiers);
      setConfig({
        ...config,
        budget_tiers: savedTiers,
        auto_bracket_enabled: data.auto_bracket_enabled ?? editAutoBracket,
      });
      setTimeout(() => setTierSaveMsg(''), 3000);
    } catch (err) {
      setTierSaveMsg('Could not reach server');
    }
  };

  const exportTradeHistory = async (format = 'json') => {
    try {
      const res = await apiFetch(`/trades/export?format=${format}&limit=10000`, {}, 60000);
      if (!res.ok) return;
      const date = new Date().toISOString().slice(0, 10);
      if (format === 'csv') {
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `trade_history_${date}.csv`;
        a.click();
        URL.revokeObjectURL(url);
      } else {
        const data = await res.json();
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `trade_history_${date}.json`;
        a.click();
        URL.revokeObjectURL(url);
      }
    } catch (err) {
      console.error('export trade history:', err);
    }
  };

  const tradeEval = (t) => t.bot_evaluation || {};
  const fmtConf = (v) => (v != null && v !== '' ? `${Math.round(Number(v) * 100)}%` : '—');

  const handleWizardSubmit = async (e) => {
    e.preventDefault();
    setWizardLoading(true);
    setWizardError('');
    try {
      const res = await fetch(`${API_URL}/setup`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(wizardData),
      });
      const data = await res.json();
      if (!res.ok) { setWizardError(data.detail || 'Setup failed'); return; }
      if (data.mode === 'railway') {
        setWizardDone(true);
        return;
      }
      setShowSetupWizard(false);
      setWizardDone(false);
    } catch (err) {
      setWizardError('Could not reach server');
    } finally {
      setWizardLoading(false);
    }
  };


  const overlayStyle = {
    position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.85)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    zIndex: 9999, padding: '1rem',
  };
  const cardStyle = {
    background: '#0f172a', border: '1px solid rgba(99,102,241,0.3)',
    borderRadius: '16px', padding: '2.5rem', maxWidth: '480px', width: '100%',
    boxShadow: '0 25px 50px rgba(0,0,0,0.5)',
  };
  const inputStyle = {
    width: '100%', padding: '0.75rem 1rem', borderRadius: '8px',
    border: '1px solid rgba(255,255,255,0.15)', background: 'rgba(255,255,255,0.07)',
    color: '#e2e8f0', fontSize: '0.9rem', boxSizing: 'border-box', marginBottom: '0.75rem',
  };
  const btnPrimaryStyle = {
    width: '100%', padding: '0.875rem', borderRadius: '8px',
    background: 'linear-gradient(135deg, #6366f1, #8b5cf6)', color: 'white',
    fontWeight: 700, fontSize: '0.95rem', border: 'none', cursor: 'pointer',
  };

  if (showSetupWizard) {
    return (
      <div style={overlayStyle}>
        <div style={cardStyle}>
          <h1 style={{ fontSize: '1.75rem', fontWeight: 800, marginBottom: '0.75rem' }}>
            Welcome to BESTAbot 👋
          </h1>
          <p style={{ color: '#94a3b8', fontSize: '0.9rem', marginBottom: '1.5rem' }}>
            {setupStatus?.is_railway
              ? 'Running on Railway. Set your credentials via Railway Variables and redeploy.'
              : 'Connect your IQ Option account to get started.'}
          </p>
          {wizardDone ? (
            <div>
              <div style={{ background: 'rgba(52,211,153,0.1)', border: '1px solid rgba(52,211,153,0.3)', borderRadius: '8px', padding: '1rem', color: '#34d399', marginBottom: '1.5rem', fontSize: '0.9rem' }}>
                <strong>Running on Railway</strong><br/>
                Go to your Railway project → Variables tab → set <code>IQ_EMAIL</code>, <code>IQ_PASSWORD</code>, and <code>IQ_ACCOUNT_TYPE</code>, then click Redeploy.
              </div>
              <button style={btnPrimaryStyle} onClick={() => setShowSetupWizard(false)}>Close</button>
            </div>
          ) : (
            <form onSubmit={handleWizardSubmit}>
              <label style={{ color: '#94a3b8', fontSize: '0.8rem' }}>IQ Option Email</label>
              <input style={inputStyle} type="email" placeholder="your@email.com" value={wizardData.iq_email}
                onChange={e => setWizardData(d => ({ ...d, iq_email: e.target.value }))} required />
              <label style={{ color: '#94a3b8', fontSize: '0.8rem' }}>IQ Option Password</label>
              <input style={inputStyle} type="password" placeholder="••••••••" value={wizardData.iq_password}
                onChange={e => setWizardData(d => ({ ...d, iq_password: e.target.value }))} required />
              <label style={{ color: '#94a3b8', fontSize: '0.8rem' }}>Account Type</label>
              <select style={{ ...inputStyle, cursor: 'pointer' }} value={wizardData.iq_account_type}
                onChange={e => setWizardData(d => ({ ...d, iq_account_type: e.target.value }))}>
                <option value="PRACTICE">Practice (Recommended)</option>
                <option value="REAL">Real</option>
              </select>
              {wizardError && <p style={{ color: '#f87171', fontSize: '0.85rem', margin: '0 0 0.75rem' }}>{wizardError}</p>}
              <button style={{ ...btnPrimaryStyle, opacity: wizardLoading ? 0.7 : 1 }} type="submit" disabled={wizardLoading}>
                {wizardLoading ? 'Connecting…' : 'Connect & Start'}
              </button>
              <button type="button" onClick={() => setShowSetupWizard(false)}
                style={{ width: '100%', marginTop: '0.75rem', padding: '0.75rem', borderRadius: '8px', background: 'transparent', border: '1px solid rgba(255,255,255,0.1)', color: '#64748b', cursor: 'pointer', fontSize: '0.85rem' }}>
                Skip — I'll configure manually
              </button>
            </form>
          )}
        </div>
      </div>
    );
  }


  if (!status || !config) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh', color: 'white' }}>
        Connecting…
      </div>
    );
  }

  const tiers = normalizeBudgetTiers(config?.budget_tiers || status?.budget_tiers || []);
  const tierNum = (status.current_tier_index || 0) + 1;
  const assignedTier = status.assigned_tier || tierNum;
  const stepNum = status.current_step || (status.session_round_count || 0) + 1;
  const ladderSteps = status.ladder_steps || (tiers[status.current_tier_index]?.length) || 3;

  return (
    <div className="dashboard-container">
      {(status.is_real_account || status.account_type === 'REAL') && (
        <div style={{ background: '#7f1d1d', color: '#fecaca', textAlign: 'center', padding: '0.5rem', fontWeight: 700 }}>
          REAL MONEY — live orders
        </div>
      )}
      {status.simulation_mode && (
        <div style={{ background: '#1e3a5f', color: '#93c5fd', textAlign: 'center', padding: '0.5rem', fontWeight: 600 }}>
          SIMULATION — no real orders
        </div>
      )}

      {showRealConfirm && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.75)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}>
          <div className="glass-panel" style={{ maxWidth: 400, padding: '1.5rem' }}>
            <h3 style={{ color: '#fca5a5' }}>Start REAL trading?</h3>
            <p style={{ margin: '1rem 0' }}>Live balance will be used.</p>
            <div style={{ display: 'flex', gap: '0.75rem' }}>
              <button type="button" className="btn-save" onClick={() => doStart(true)} disabled={isToggling}>Confirm</button>
              <button type="button" onClick={() => setShowRealConfirm(false)}>Cancel</button>
            </div>
          </div>
        </div>
      )}

      <header className="header">
        <div className="header-title">
          <Activity size={20} color="var(--primary)" />
          BESTAbot
        </div>
        <div className="header-controls">
          <span style={{ fontSize: '0.78rem', color: status.connected ? 'var(--success)' : 'var(--warning)', fontWeight: 600 }}>
            {status.connecting ? 'Connecting…' : status.connected ? '● Connected' : '○ Disconnected'}
          </span>
          <select
            onChange={handleAccountChange}
            value={resolveActiveAccountId(status, accounts)}
          >
            {accounts.length === 0 ? (
              <option value="">Loading…</option>
            ) : (
              accounts.map((acc) => (
                <option key={acc.id} value={String(acc.id)}>
                  {acc.label} — ${acc.amount.toFixed(2)}
                </option>
              ))
            )}
          </select>
          <div className="balance-badge">
            <DollarSign size={14} style={{ display: 'inline', verticalAlign: 'text-bottom' }} />
            {status.balance.toFixed(2)}
          </div>
          <button
            type="button"
            className={`header-refresh-balance-btn${isRefreshingBalance ? ' spinning' : ''}`}
            onClick={refreshBalance}
            disabled={isRefreshingBalance || !status.connected}
            title="Refresh balance"
            aria-label="Refresh balance"
          >
            <RotateCcw size={14} />
            Refresh
          </button>
        </div>
      </header>

      <div className="main-grid">
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem', minWidth: 0 }}>
          <div className="glass-panel control-section">
            <div className={`status-badge ${status.running && !status.paused ? 'active' : 'idle'}`}>
              {status.running ? (status.paused ? 'PAUSED' : 'RUNNING') : status.connected ? 'STOPPED' : 'DISCONNECTED'}
            </div>
            <button
              className={`power-btn ${status.running ? 'stop' : 'start'}`}
              onClick={handleStartStop}
              disabled={isToggling || (!status.running && !status.connected)}
            >
              <Power size={40} />
            </button>
            {status.running && (
              <button type="button" onClick={handlePauseResume} disabled={isToggling} style={{ marginTop: '0.75rem' }}>
                {status.paused ? 'Resume' : 'Pause'}
              </button>
            )}
            


            {actionError && <p style={{ color: '#f87171', fontSize: '0.9rem', marginTop: '1rem' }}>{actionError}</p>}
            {!status.running && (status.cumulative_debt > 0 || stepNum > 1) && (
              <p style={{ color: '#93c5fd', fontSize: '0.85rem', marginTop: '0.5rem' }}>
                Resume: Tier {tierNum} step {stepNum}/{ladderSteps}
                {status.cumulative_debt > 0 ? ` · debt $${status.cumulative_debt.toFixed(2)}` : ''}
              </p>
            )}
          </div>

          <div className="glass-panel">
            <h2 className="panel-title">Status</h2>
            <div className="stats-grid">
              <div className="stat-card">
                <span className="stat-label">Pair</span>
                <span className="stat-value" style={{ fontSize: '0.95rem' }}>{status.asset}</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">Tier / step</span>
                <span className="stat-value">T{tierNum} · {stepNum}/{ladderSteps}</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">Next bet (per leg)</span>
                <span className="stat-value" style={{ color: '#60a5fa' }}>${status.current_bet?.toFixed(2) ?? '—'}</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">Recovery debt</span>
                <span className={`stat-value ${(status.cumulative_debt || 0) <= 0 ? 'profit' : 'loss'}`}>
                  ${(status.cumulative_debt || 0).toFixed(2)}
                </span>
              </div>
              <div className="stat-card" title="Round 1 = normal play (T0+T1). Round 2 = escalation (T2+T3). Round 3 = last resort (T4+T5). After any recovery the bot always returns to Round 1.">
                <span className="stat-label">Round</span>
                <span className="stat-value" style={{ color: status.active_round === 1 ? '#34d399' : status.active_round === 2 ? '#fbbf24' : '#f87171' }}>
                  {status.active_round ?? 1}
                  {status.active_round > 1 ? ' ⚠' : ''}
                </span>
              </div>
              {status.is_reserve_tier && (
                <div className="stat-card" title="Wins still needed on this reserve tier before returning to Round 1. Each step win counts more: S1=1, S2=2, S3=3.">
                  <span className="stat-label">Wins to recover</span>
                  <span className="stat-value" style={{ color: '#fbbf24' }}>
                    {status.reserve_wins_needed ?? 3} left
                  </span>
                </div>
              )}
              {status.is_mopup_phase && (
                <div className="stat-card" title={`T${status.mopup_tier} is recovering prior-round losses. Once this debt hits $0 the bot returns to Round 1 (T0).`} style={{ gridColumn: '1 / -1' }}>
                  <span className="stat-label" style={{ color: '#fb923c' }}>🔄 Mop-up Phase · T{status.mopup_tier}</span>
                  <div style={{ marginTop: '0.4rem', display: 'flex', alignItems: 'center', gap: '0.6rem' }}>
                    <div style={{ flex: 1, height: 8, borderRadius: 4, background: 'rgba(255,255,255,0.1)', overflow: 'hidden' }}>
                      <div style={{
                        height: '100%',
                        borderRadius: 4,
                        background: 'linear-gradient(90deg, #f97316, #fb923c)',
                        width: status.mopup_initial_debt > 0
                          ? `${Math.max(0, Math.min(100, ((status.mopup_initial_debt - status.cumulative_debt) / status.mopup_initial_debt) * 100))}%`
                          : '0%',
                        transition: 'width 0.5s ease',
                      }} />
                    </div>
                    <span className="stat-value" style={{ color: '#fb923c', fontSize: '0.9rem', whiteSpace: 'nowrap' }}>
                      ${(status.cumulative_debt || 0).toFixed(2)} left
                    </span>
                  </div>
                  <div style={{ marginTop: '0.35rem', fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                    Prior-round losses: ${(status.mopup_initial_debt || 0).toFixed(2)} · Recovered: ${Math.max(0, (status.mopup_initial_debt || 0) - (status.cumulative_debt || 0)).toFixed(2)}
                  </div>
                </div>
              )}
              {status.slope_flip_blocked && Object.keys(status.slope_flip_blocked).length > 0 && (
                <div className="stat-card" title="Assets temporarily blocked because the 3-bar short-term slope reversed against the 15-bar medium slope. Bot switches to next best asset or waits. Block expires in ~12 min." style={{ gridColumn: '1 / -1' }}>
                  <span className="stat-label" style={{ color: '#a78bfa' }}>⚡ Slope-flip Cooldowns</span>
                  <div style={{ marginTop: '0.4rem', display: 'flex', flexWrap: 'wrap', gap: '0.4rem' }}>
                    {Object.entries(status.slope_flip_blocked).map(([asset, secsLeft]) => {
                      const minsLeft = Math.ceil(secsLeft / 60);
                      const pct = Math.max(0, Math.min(100, (secsLeft / 720) * 100));
                      return (
                        <div key={asset} style={{
                          background: 'rgba(139,92,246,0.12)',
                          border: '1px solid rgba(139,92,246,0.30)',
                          borderRadius: 6,
                          padding: '0.3rem 0.55rem',
                          display: 'flex',
                          flexDirection: 'column',
                          gap: '0.25rem',
                          minWidth: 110,
                        }}>
                          <span style={{ fontSize: '0.78rem', fontWeight: 600, color: '#a78bfa' }}>{asset}</span>
                          <div style={{ height: 4, borderRadius: 2, background: 'rgba(255,255,255,0.08)', overflow: 'hidden' }}>
                            <div style={{ height: '100%', borderRadius: 2, background: '#a78bfa', width: `${pct}%`, transition: 'width 1s linear' }} />
                          </div>
                          <span style={{ fontSize: '0.70rem', color: 'var(--text-muted)' }}>{minsLeft}m left</span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
              <div className="stat-card">
                <span className="stat-label">Window P/L</span>
                <span className={`stat-value ${(status.window_profit || 0) >= 0 ? 'profit' : 'loss'}`}>
                  ${(status.window_profit || 0).toFixed(2)}
                </span>
              </div>
              {(status.tier_failure_streak > 0 || status.tier_exhaustion_cooldown_until) && (
                <div className="stat-card">
                  <span className="stat-label">Protection</span>
                  <span className="stat-value" style={{ fontSize: '0.85rem' }}>
                    {status.tier_exhaustion_cooldown_until ? 'Cooldown' : `Fail ${status.tier_failure_streak}`}
                  </span>
                </div>
              )}
              <div className="stat-card" title={`Pauses trading for 30 min after ${status.consec_ladder_loss_limit ?? 2} back-to-back full-ladder losses. Resets on any win.`}>
                <span className="stat-label">Full-ladder losses</span>
                <span
                  className="stat-value"
                  style={{
                    color:
                      (status.consecutive_full_ladder_losses ?? 0) === 0
                        ? '#34d399'
                        : (status.consecutive_full_ladder_losses ?? 0) >= (status.consec_ladder_loss_limit ?? 2)
                        ? '#f87171'
                        : '#fbbf24',
                  }}
                >
                  {status.consecutive_full_ladder_losses ?? 0} / {status.consec_ladder_loss_limit ?? 2}
                </span>
              </div>
              <div className="stat-card">
                <span className="stat-label">Session P/L</span>
                <span className={`stat-value ${(status.session_profit || 0) >= 0 ? 'profit' : 'loss'}`}>
                  ${(status.session_profit || 0).toFixed(2)}
                </span>
              </div>
              <div className="stat-card">
                <span className="stat-label">Total P/L</span>
                <span className={`stat-value ${(status.total_profit || 0) >= 0 ? 'profit' : 'loss'}`}>
                  ${(status.total_profit || 0).toFixed(2)}
                </span>
              </div>
              <div className="stat-card">
                <span className="stat-label">W / L</span>
                <span className="stat-value">{status.wins} / {status.losses}</span>
              </div>
            </div>
            {status.running && status.status_note && (
              <div
                style={{
                  marginTop: '1rem',
                  padding: '0.75rem 1rem',
                  borderRadius: '8px',
                  background: 'rgba(251, 191, 36, 0.10)',
                  border: '1px solid rgba(251, 191, 36, 0.35)',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '0.6rem',
                }}
              >
                <span style={{ fontSize: '1.05rem', lineHeight: 1 }}>🤖</span>
                <strong style={{ color: '#fbbf24', fontSize: '0.9rem', fontWeight: 700 }}>
                  {status.status_note}
                </strong>
              </div>
            )}
            {status.pair_quality && status.pair_quality.tradeable === false && (
              <div
                style={{
                  marginTop: '1.25rem',
                  padding: '1rem 1.25rem',
                  borderRadius: '12px',
                  background: 'linear-gradient(135deg, rgba(239,68,68,0.22) 0%, rgba(220,38,38,0.14) 100%)',
                  border: '2px solid rgba(239,68,68,0.7)',
                  boxShadow: '0 0 18px rgba(239,68,68,0.35), inset 0 1px 0 rgba(255,255,255,0.05)',
                  textAlign: 'center',
                }}
              >
                <div style={{ fontSize: '1.35rem', marginBottom: '0.3rem' }}>🚫</div>
                <div style={{ fontSize: '0.95rem', fontWeight: 800, color: '#ff6b6b', letterSpacing: '0.02em', textTransform: 'uppercase' }}>
                  Pair Quality Check Failed
                </div>
                <div style={{ margin: '0.45rem 0 0', fontSize: '0.85rem', color: '#fca5a5', lineHeight: 1.45 }}>
                  {status.pair_quality.reason || 'Market conditions failed quality gates.'}
                </div>
                {status.pair_quality.efficiency_ratio != null && (
                  <div style={{ marginTop: '0.55rem', fontSize: '0.78rem', color: '#fde68a', fontWeight: 600, letterSpacing: '0.01em' }}>
                    ER {status.pair_quality.efficiency_ratio} · slope {status.pair_quality.abs_slope}
                    <span style={{ color: '#fb923c', marginLeft: '0.35rem' }}>
                      {status.auto_select_asset ? '— auto-pick will try another pair' : '— enable auto-pick or change pair'}
                    </span>
                  </div>
                )}
              </div>
            )}
            {status.pair_quality && status.pair_quality.tradeable === true && (
              <div
                style={{
                  marginTop: '1.25rem',
                  padding: '0.75rem 1.25rem',
                  borderRadius: '12px',
                  background: 'linear-gradient(135deg, rgba(52,211,153,0.18) 0%, rgba(16,185,129,0.10) 100%)',
                  border: '2px solid rgba(52,211,153,0.55)',
                  boxShadow: '0 0 14px rgba(52,211,153,0.25)',
                  textAlign: 'center',
                }}
              >
                <div style={{ fontSize: '0.8rem', fontWeight: 700, color: '#34d399', letterSpacing: '0.04em', textTransform: 'uppercase' }}>
                  ✅ Pair OK
                </div>
                <div style={{ marginTop: '0.3rem', fontSize: '0.78rem', color: '#6ee7b7' }}>
                  ER {status.pair_quality.efficiency_ratio} · slope {status.pair_quality.abs_slope}
                </div>
              </div>
            )}
            {status.learned_pattern?.loaded && (
              <p style={{ fontSize: '0.8rem', color: '#a78bfa', marginTop: '0.75rem' }}>
                Learned gates (display only) ({status.learned_pattern.source_label || 'history'}):
                ER ≥ {status.learned_pattern.gates?.min_efficiency_ratio},
                slope ≥ {status.learned_pattern.gates?.min_directional_slope}
                {status.learned_pattern.focus_assets?.length > 0 && (
                  <> · focus {status.learned_pattern.focus_assets.join(', ')}</>
                )}
              </p>
            )}
            {Array.isArray(status.scheduled_ladder) && status.scheduled_ladder.length > 0 && (
              <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginTop: '0.75rem' }}>
                Current ladder: {status.scheduled_ladder.map((n) => `$${n}`).join(' → ')}
              </p>
            )}
            <button
              type="button"
              className="btn-secondary"
              style={{ marginTop: '1rem' }}
              disabled={isResetting}
              onClick={() => setShowResetConfirm(true)}
            >
              <RotateCcw size={16} style={{ display: 'inline', verticalAlign: 'middle', marginRight: 4 }} />
              Reset to Tier 1
            </button>
          </div>

          {showResetConfirm && (
            <div className="glass-panel">
              <p>Reset {status.account_key || status.account_type} to Tier 1, $0 debt?</p>
              <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginTop: '0.5rem' }}>
                Stops the bot if needed, then clears Tier/debt for all accounts, penalties, and trade history.
                Each new Railway deploy also starts fresh automatically.
              </p>
              <label style={{ display: 'flex', gap: '0.5rem', margin: '0.75rem 0', fontSize: '0.9rem' }}>
                <input type="checkbox" checked={clearLogOnReset} onChange={(e) => setClearLogOnReset(e.target.checked)} />
                Clear trade history and relearned pair rules (recommended)
              </label>
              {actionError && <p style={{ color: '#f87171', fontSize: '0.85rem', margin: '0.5rem 0' }}>{actionError}</p>}
              <button type="button" className="btn-danger" disabled={isResetting} onClick={handleResetProgress}>
                {isResetting ? 'Resetting…' : 'Reset'}
              </button>
              <button type="button" style={{ marginLeft: '0.5rem' }} onClick={() => { setShowResetConfirm(false); setActionError(''); }}>Cancel</button>
            </div>
          )}

          {/* ── Recent Trades ── */}
          <div className="glass-panel">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '0.5rem', marginBottom: '0.75rem' }}>
              <h2 className="panel-title" style={{ margin: 0 }}>Recent trades</h2>
              <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
                <button type="button" className="btn-secondary" onClick={() => exportTradeHistory('json')}>
                  <Download size={14} style={{ display: 'inline', verticalAlign: 'middle', marginRight: 4 }} />
                  Export JSON
                </button>
                <button type="button" className="btn-secondary" onClick={() => exportTradeHistory('csv')}>
                  <Download size={14} style={{ display: 'inline', verticalAlign: 'middle', marginRight: 4 }} />
                  Export CSV
                </button>
              </div>
            </div>
            <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginBottom: '0.75rem' }}>
              Each trade logs candle-follow direction, confidence, and entry snapshot metrics for later analysis.
            </p>
            <div style={{ overflowX: 'auto', WebkitOverflowScrolling: 'touch', fontSize: '0.8rem' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: '600px' }}>
                <thead>
                  <tr style={{ textAlign: 'left', color: 'var(--text-muted)' }}>
                    <th style={{ padding: '0.3rem 0.4rem' }}>Time</th>
                    <th style={{ padding: '0.3rem 0.4rem' }}>Pair</th>
                    <th style={{ padding: '0.3rem 0.4rem' }}>Dir</th>
                    <th style={{ padding: '0.3rem 0.4rem' }}>T/S</th>
                    <th style={{ padding: '0.3rem 0.4rem' }}>Bot%</th>
                    <th style={{ padding: '0.3rem 0.4rem' }}>ER</th>
                    <th style={{ padding: '0.3rem 0.4rem' }}>Slope</th>
                    <th style={{ padding: '0.3rem 0.4rem' }}>Score</th>
                    <th style={{ padding: '0.3rem 0.4rem' }}>Align</th>
                    <th style={{ padding: '0.3rem 0.4rem' }}>P/L</th>
                    <th style={{ padding: '0.3rem 0.4rem', color: 'var(--text-muted)' }}>▸</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.length === 0 ? (
                    <tr><td colSpan={11} style={{ padding: '0.5rem 0.4rem', color: 'var(--text-muted)' }}>No trades yet</td></tr>
                  ) : trades.map((t, i) => {
                    const ev = tradeEval(t);
                    const snap = t.entry_snapshot || {};
                    const dir = (ev.direction || t.bot_direction || '—').toUpperCase();
                    const aligned = ev.trend_aligned;
                    const profit = t.round_profit || 0;
                    const isWin = profit > 0;
                    const isExpanded = expandedTradeIndex === i;

                    const dirColor = dir === 'CALL' ? '#34d399' : dir === 'PUT' ? '#f87171' : '#94a3b8';
                    const rowStyle = {
                      borderTop: '1px solid rgba(255,255,255,0.06)',
                      cursor: 'pointer',
                      background: isExpanded ? 'rgba(99,102,241,0.08)' : 'transparent',
                      transition: 'background 0.15s',
                    };

                    const flipKind = ev.direction_flip_kind;
                    const ruleGate = ev.rule_gate_reason;
                    const aiApproved = ev.ai_approved;
                    const aiConf = ev.ai_confidence;
                    const aiSkipped = ev.ai_skipped;
                    const aiDisabled = ev.ai_disabled;

                    const fmtNum = (v, dp = 3) => (v != null && v !== '' && !isNaN(Number(v))) ? Number(v).toFixed(dp) : '—';

                    const guardLabel = ruleGate
                      ? ruleGate
                      : (ev.trend_aligned === false ? 'LT trend block' : null);

                    return [
                      <tr
                        key={`row-${i}`}
                        style={rowStyle}
                        onClick={() => setExpandedTradeIndex(isExpanded ? null : i)}
                      >
                        <td style={{ padding: '0.35rem 0.4rem' }}>{t.ts ? new Date(t.ts).toLocaleTimeString() : '—'}</td>
                        <td style={{ padding: '0.35rem 0.4rem' }}>{t.asset}</td>
                        <td style={{ padding: '0.35rem 0.4rem', color: dirColor, fontWeight: 600 }}>{dir}</td>
                        <td style={{ padding: '0.35rem 0.4rem' }}>T{t.tier} S{t.step}</td>
                        <td style={{ padding: '0.35rem 0.4rem' }}>{fmtConf(ev.bot_confidence ?? t.bot_confidence)}</td>
                        <td style={{ padding: '0.35rem 0.4rem' }}>{fmtNum(ev.entry_er ?? snap.efficiency_ratio, 3)}</td>
                        <td style={{ padding: '0.35rem 0.4rem' }}>{fmtNum(ev.entry_slope_signed ?? snap.slope_signed, 1)}</td>
                        <td style={{ padding: '0.35rem 0.4rem' }}>{fmtNum(ev.entry_straddle_score ?? snap.movement_score ?? snap.straddle_score, 1)}</td>
                        <td style={{ padding: '0.35rem 0.4rem', color: aligned === true ? '#34d399' : aligned === false ? '#f87171' : 'inherit' }}>
                          {aligned === true ? '✓' : aligned === false ? '✗' : '—'}
                        </td>
                        <td style={{ padding: '0.35rem 0.4rem', color: isWin ? '#34d399' : '#f87171', fontWeight: 600 }}>
                          {isWin ? '+' : ''}${profit.toFixed(2)}
                        </td>
                        <td style={{ padding: '0.35rem 0.4rem', color: 'var(--text-muted)', fontSize: '0.7rem' }}>
                          {isExpanded ? '▼' : '▸'}
                        </td>
                      </tr>,
                      isExpanded && (
                        <tr key={`replay-${i}`} style={{ background: 'rgba(15,23,42,0.6)' }}>
                          <td colSpan={11} style={{ padding: '0.75rem 1rem' }}>
                            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: '0.75rem' }}>

                              <div style={{ background: 'rgba(99,102,241,0.08)', borderRadius: '6px', padding: '0.6rem 0.75rem', border: '1px solid rgba(99,102,241,0.2)' }}>
                                <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', marginBottom: '0.3rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Direction Logic</div>
                                <div style={{ color: dirColor, fontWeight: 700, fontSize: '1rem' }}>{dir}</div>
                                {flipKind && <div style={{ fontSize: '0.7rem', color: '#fbbf24', marginTop: '0.2rem' }}>Flip: {flipKind}</div>}
                                {!flipKind && aligned === true && <div style={{ fontSize: '0.7rem', color: '#34d399', marginTop: '0.2rem' }}>Trend aligned</div>}
                                {!flipKind && aligned === false && <div style={{ fontSize: '0.7rem', color: '#f87171', marginTop: '0.2rem' }}>Counter-trend</div>}
                                {ev.slope_override_flip && <div style={{ fontSize: '0.7rem', color: '#a78bfa', marginTop: '0.2rem' }}>Slope override flip</div>}
                              </div>

                              <div style={{ background: 'rgba(15,23,42,0.4)', borderRadius: '6px', padding: '0.6rem 0.75rem', border: '1px solid rgba(255,255,255,0.06)' }}>
                                <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', marginBottom: '0.3rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Entry Metrics</div>
                                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.2rem', fontSize: '0.75rem' }}>
                                  <span><span style={{ color: 'var(--text-muted)' }}>Slope: </span><span style={{ color: (ev.entry_slope_signed ?? snap.slope_signed ?? 0) >= 0 ? '#34d399' : '#f87171' }}>{fmtNum(ev.entry_slope_signed ?? snap.slope_signed, 1)}</span></span>
                                  <span><span style={{ color: 'var(--text-muted)' }}>ER: </span><span style={{ color: '#e2e8f0' }}>{fmtNum(ev.entry_er ?? snap.efficiency_ratio, 3)}</span></span>
                                  <span><span style={{ color: 'var(--text-muted)' }}>Score: </span><span style={{ color: '#e2e8f0' }}>{fmtNum(ev.entry_straddle_score ?? snap.movement_score ?? snap.straddle_score, 1)}</span></span>
                                  <span><span style={{ color: 'var(--text-muted)' }}>Momentum: </span><span style={{ color: '#e2e8f0' }}>{fmtNum(snap.momentum_ratio, 3)}</span></span>
                                  <span><span style={{ color: 'var(--text-muted)' }}>Confidence: </span><span style={{ color: '#e2e8f0' }}>{fmtConf(ev.bot_confidence ?? t.bot_confidence)}</span></span>
                                </div>
                              </div>

                              <div style={{ background: guardLabel ? 'rgba(251,191,36,0.06)' : 'rgba(15,23,42,0.4)', borderRadius: '6px', padding: '0.6rem 0.75rem', border: `1px solid ${guardLabel ? 'rgba(251,191,36,0.25)' : 'rgba(255,255,255,0.06)'}` }}>
                                <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', marginBottom: '0.3rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Gates</div>
                                {guardLabel
                                  ? <div style={{ fontSize: '0.75rem', color: '#fbbf24', wordBreak: 'break-word' }}>{guardLabel}</div>
                                  : <div style={{ fontSize: '0.75rem', color: '#34d399' }}>All gates passed</div>
                                }
                                {ev.er_floor_used != null && (
                                  <div style={{ fontSize: '0.72rem', marginTop: '0.3rem', display: 'flex', alignItems: 'center', gap: '0.3rem', flexWrap: 'wrap' }}>
                                    <span style={{ color: 'var(--text-muted)' }}>ER floor:</span>
                                    <span style={{ color: '#e2e8f0', fontWeight: 600 }}>{ev.er_floor_used.toFixed(3)}</span>
                                    <span style={{ color: 'var(--text-muted)' }}>· actual:</span>
                                    <span style={{
                                      color: (ev.entry_er ?? 0) >= ev.er_floor_used ? '#34d399' : '#f87171',
                                      fontWeight: 600,
                                    }}>{fmtNum(ev.entry_er, 3)}</span>
                                    <span style={{ color: (ev.entry_er ?? 0) >= ev.er_floor_used ? '#34d399' : '#f87171' }}>
                                      {(ev.entry_er ?? 0) >= ev.er_floor_used ? '✓' : '✗'}
                                    </span>
                                  </div>
                                )}
                                {ev.pair_quality_reason && (
                                  <div style={{ fontSize: '0.7rem', color: '#f87171', marginTop: '0.2rem' }}>Pair: {ev.pair_quality_reason}</div>
                                )}
                                {ev.step_score_required != null && (
                                  <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginTop: '0.2rem' }}>Step score req: {ev.step_score_required}</div>
                                )}
                              </div>

                              {!aiDisabled && (
                                <div style={{ background: 'rgba(15,23,42,0.4)', borderRadius: '6px', padding: '0.6rem 0.75rem', border: '1px solid rgba(255,255,255,0.06)' }}>
                                  <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', marginBottom: '0.3rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>AI Gate</div>
                                  {aiSkipped
                                    ? <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Skipped (high-conf)</div>
                                    : aiApproved === true
                                      ? <div style={{ fontSize: '0.75rem', color: '#34d399' }}>✓ Approved {aiConf != null ? `(${Math.round(aiConf * 100)}%)` : ''}</div>
                                      : aiApproved === false
                                        ? <div style={{ fontSize: '0.75rem', color: '#f87171' }}>✗ Rejected {aiConf != null ? `(${Math.round(aiConf * 100)}%)` : ''}</div>
                                        : <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>—</div>
                                  }
                                </div>
                              )}

                              <div style={{ background: 'rgba(15,23,42,0.4)', borderRadius: '6px', padding: '0.6rem 0.75rem', border: '1px solid rgba(255,255,255,0.06)' }}>
                                <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', marginBottom: '0.3rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Result</div>
                                <div style={{ fontSize: '0.85rem', fontWeight: 700, color: isWin ? '#34d399' : '#f87171' }}>
                                  {isWin ? 'WIN' : 'LOSS'} {isWin ? '+' : ''}${profit.toFixed(2)}
                                </div>
                                <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginTop: '0.2rem' }}>
                                  Bet: ${t.bet} · T{t.tier} S{t.step}
                                </div>
                                {t.debt != null && <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>Debt after: ${Number(t.debt).toFixed(2)}</div>}
                              </div>

                            </div>
                          </td>
                        </tr>
                      )
                    ];
                  })}
                </tbody>
              </table>
            </div>
            <p style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginTop: '0.5rem' }}>
              Click any trade row to expand entry metrics and outcome details.
            </p>
          </div>

          {/* ── Tiers Configuration ── */}
          <div className="glass-panel">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75rem' }}>
              <h2 className="panel-title" style={{ margin: 0 }}>Martingale Ladder</h2>
              {!editTiers ? (
                <button type="button" className="btn-secondary" onClick={initTierEditor}>
                  ✏️ Edit
                </button>
              ) : (
                <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                  <button
                    type="button"
                    className="btn-save"
                    style={{ padding: '0.35rem 1rem', fontSize: '0.8rem', minHeight: 'unset' }}
                    onClick={(e) => { e.preventDefault(); saveTiers(); }}
                  >
                    Save
                  </button>
                  <button type="button" className="btn-secondary" style={{ fontSize: '0.8rem' }} onClick={() => setEditTiers(null)}>
                    Cancel
                  </button>
                  {tierSaveMsg && (
                    <span style={{ color: tierSaveMsg.includes('failed') || tierSaveMsg.includes('Could not') ? 'var(--danger)' : 'var(--success)', fontSize: '0.8rem', fontWeight: 600 }}>
                      {tierSaveMsg}
                    </span>
                  )}
                </div>
              )}
            </div>

            {editTiers ? (
              /* ── Edit Mode ── */
              <div style={{ overflowX: 'auto' }}>
                <div style={{ marginBottom: '1rem', padding: '0.75rem', background: 'rgba(0,0,0,0.2)', borderRadius: '6px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.75rem' }}>
                    <input 
                      type="checkbox" 
                      checked={editAutoBracket} 
                      onChange={(e) => setEditAutoBracket(e.target.checked)} 
                      id="auto-bracket-toggle"
                      style={{ cursor: 'pointer' }}
                    />
                    <label htmlFor="auto-bracket-toggle" style={{ fontSize: '0.85rem', color: 'var(--text-primary)', cursor: 'pointer', fontWeight: 600 }}>
                      Auto-Select Bracket via Balance
                    </label>
                  </div>
                  
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                    <label style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>Manual Bracket (Applies on next bot start):</label>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem' }}>
                      {defaultBracketPresets.map((bracket, idx) => {
                        const isSelected = JSON.stringify(editTiers?.[0]) === JSON.stringify(bracket.val);
                        return (
                          <button
                            key={idx}
                            type="button"
                            disabled={editAutoBracket}
                            onClick={() => setEditTiers(normalizeBudgetTiers(bracket.val))}
                            style={{
                              padding: '0.4rem 0.8rem',
                              borderRadius: '4px',
                              border: isSelected ? '1px solid var(--accent-blue)' : '1px solid var(--panel-border)',
                              background: isSelected ? 'rgba(56, 189, 248, 0.2)' : (editAutoBracket ? 'rgba(17,21,28,0.5)' : 'rgba(17,21,28,0.9)'),
                              color: editAutoBracket ? 'var(--text-muted)' : (isSelected ? 'var(--accent-blue)' : 'var(--text-primary)'),
                              fontSize: '0.8rem',
                              cursor: editAutoBracket ? 'not-allowed' : 'pointer',
                              transition: 'all 0.2s ease'
                            }}
                          >
                            {bracket.label}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                </div>
                {editTiers.map((tier, tIdx) => (
                  <div key={tIdx} style={{ marginBottom: tIdx < editTiers.length - 1 ? '0.75rem' : 0 }}>
                    <div style={{ fontSize: '0.72rem', color: 'var(--accent-blue)', fontWeight: 600, marginBottom: '0.4rem' }}>Tier {tIdx}</div>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.4rem', alignItems: 'flex-end' }}>
                      {tier.map((amount, sIdx) => (
                        <div key={sIdx} style={{ display: 'flex', flexDirection: 'column', gap: '0.15rem', width: '62px' }}>
                          <label style={{ fontSize: '0.6rem', color: 'var(--text-muted)', textAlign: 'center' }}>S{sIdx + 1}</label>
                          <input
                            type="number"
                            value={amount}
                            onChange={(e) => {
                              const raw = e.target.value;
                              const parsed = raw === '' ? '' : Number(raw);
                              const newTiers = editTiers.map(t => [...t]);
                              newTiers[tIdx][sIdx] = parsed;
                              setEditTiers(newTiers);
                            }}
                            style={{
                              width: '100%',
                              padding: '0.3rem',
                              borderRadius: '4px',
                              border: '1px solid var(--panel-border)',
                              background: 'rgba(17,21,28,0.8)',
                              color: 'var(--warning)',
                              fontSize: '0.82rem',
                              textAlign: 'center',
                              fontWeight: 600
                            }}
                            min="1"
                            step="1"
                          />
                        </div>
                      ))}
                      <button
                        type="button"
                        onClick={() => {
                          const newTiers = editTiers.map(t => [...t]);
                          newTiers[tIdx].push(1);
                          setEditTiers(newTiers);
                        }}
                        style={{
                          padding: '0.3rem 0.5rem',
                          borderRadius: '4px',
                          background: 'transparent',
                          border: '1px dashed rgba(42,50,62,0.8)',
                          color: 'var(--text-muted)',
                          cursor: 'pointer',
                          fontSize: '0.75rem',
                          height: 'fit-content'
                        }}
                      >
                        +
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
            /* ── Read-only Table ── */
              <div style={{ overflowX: 'auto' }}>
                {config?.auto_bracket_enabled !== false && balanceTierBrackets.length > 0 && (
                  <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginBottom: '0.5rem' }}>
                    Auto-bracket ranges: {balanceTierBrackets.map((b) => b.range_label).join(' · ')}
                  </p>
                )}
                {tiers.length === 0 ? (
                  <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)', textAlign: 'center' }}>No tiers configured</p>
                ) : (
                  tiers.map((tier, tIdx) => (
                    <div key={tIdx} style={{ marginBottom: tIdx < tiers.length - 1 ? '0.5rem' : 0 }}>
                      {tiers.length > 1 && (
                        <div style={{ fontSize: '0.68rem', color: 'var(--accent-blue)', fontWeight: 600, marginBottom: '0.3rem' }}>Tier {tIdx}</div>
                      )}
                      <table className="tier-table">
                        <thead>
                          <tr>
                            {tier.map((_, sIdx) => (
                              <th key={sIdx}>Step {sIdx + 1}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          <tr>
                            {tier.map((amount, sIdx) => (
                              <td key={sIdx} className="amount-cell">${amount}</td>
                            ))}
                          </tr>
                        </tbody>
                      </table>
                    </div>
                  ))
                )}
              </div>
            )}
          </div>








        </div>

      </div>
    </div>
  );
}

export default App;
