// Shared UI components — exported to window

const C = {
  header:    '#075E54',
  headerDk:  '#054d44',
  teal:      '#128C7E',
  green:     '#25D366',
  greenBg:   '#DCF8C6',
  greenSoft: '#d1fae5',
  amber:     '#f59e0b',
  amberBg:   '#fef3c7',
  red:       '#ef4444',
  redBg:     '#fee2e2',
  blue:      '#3b82f6',
  blueBg:    '#dbeafe',
  bg:        '#F0F2F5',
  surface:   '#ffffff',
  text:      '#111827',
  muted:     '#6b7280',
  border:    'rgba(0,0,0,0.08)',
};

const HEAT = {
  very_hot:  { label: 'Very Hot',  color: C.red,   bg: C.redBg,    dot: '#ef4444' },
  hot:       { label: 'Hot',       color: '#d97706', bg: C.amberBg, dot: '#f59e0b' },
  warm:      { label: 'Warm',      color: '#059669', bg: C.greenSoft,dot: '#10b981' },
  luke_warm: { label: 'Lukewarm', color: C.blue,   bg: C.blueBg,   dot: '#3b82f6' },
  cold:      { label: 'Cold',      color: C.muted,  bg: '#f3f4f6',  dot: '#9ca3af' },
};

const STATUS = {
  confirmed: { label: 'Booked',    color: '#059669', bg: C.greenSoft },
  pending:   { label: 'Pending',   color: '#d97706', bg: C.amberBg },
  cancelled: { label: 'Cancelled', color: C.red,     bg: C.redBg },
  delayed:   { label: 'Delayed',   color: C.blue,    bg: C.blueBg },
};

// ── Avatar ─────────────────────────────────────────────────────────────
function Avatar({ name = '?', size = 42, bg }) {
  const initials = name.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase();
  const colors = ['#128C7E','#075E54','#25D366','#0ea5e9','#8b5cf6','#f59e0b','#ef4444'];
  const color = bg || colors[name.charCodeAt(0) % colors.length];
  return (
    <div style={{
      width: size, height: size, borderRadius: '50%',
      background: color, color: '#fff', flexShrink: 0,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      fontSize: size * 0.36, fontWeight: 700, letterSpacing: '-0.5px',
    }}>
      {initials}
    </div>
  );
}

// ── StatusPill ────────────────────────────────────────────────────────
function StatusPill({ status, small = false }) {
  const s = STATUS[status] || STATUS.pending;
  return (
    <span style={{
      background: s.bg, color: s.color,
      fontSize: small ? 10 : 11, fontWeight: 700,
      padding: small ? '2px 6px' : '3px 8px',
      borderRadius: 20, whiteSpace: 'nowrap',
    }}>
      {s.label}
    </span>
  );
}

// ── HeatPill ──────────────────────────────────────────────────────────
function HeatPill({ heat, small = false }) {
  const h = HEAT[heat] || HEAT.cold;
  return (
    <span style={{
      background: h.bg, color: h.color,
      fontSize: small ? 10 : 11, fontWeight: 700,
      padding: small ? '2px 6px' : '3px 8px',
      borderRadius: 20, whiteSpace: 'nowrap',
    }}>
      {h.label}
    </span>
  );
}

// ── ScoreDot ──────────────────────────────────────────────────────────
function ScoreDot({ score }) {
  const color = score >= 80 ? C.red : score >= 60 ? C.amber : score >= 40 ? C.green : C.muted;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2 }}>
      <div style={{
        width: 36, height: 36, borderRadius: '50%',
        background: `conic-gradient(${color} ${score * 3.6}deg, #e5e7eb 0)`,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        position: 'relative',
      }}>
        <div style={{
          width: 28, height: 28, borderRadius: '50%',
          background: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 9, fontWeight: 700, color,
        }}>
          {score}
        </div>
      </div>
    </div>
  );
}

// ── TopBar ────────────────────────────────────────────────────────────
function TopBar({ title, subtitle, onBack, right, green = false }) {
  return (
    <div style={{
      background: green ? C.header : C.header,
      padding: '12px 16px', display: 'flex', alignItems: 'center', gap: 12,
      flexShrink: 0,
    }}>
      {onBack && (
        <button onClick={onBack} style={{
          background: 'none', border: 'none', color: 'rgba(255,255,255,0.9)',
          fontSize: 20, cursor: 'pointer', padding: '0 4px 0 0', lineHeight: 1,
        }}>←</button>
      )}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ color: '#fff', fontSize: 17, fontWeight: 600, lineHeight: 1.2 }}>{title}</div>
        {subtitle && <div style={{ color: 'rgba(255,255,255,0.7)', fontSize: 12, marginTop: 1 }}>{subtitle}</div>}
      </div>
      {right && <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>{right}</div>}
    </div>
  );
}

// ── SearchBar ─────────────────────────────────────────────────────────
function SearchBar({ value, onChange, placeholder = 'Search…' }) {
  return (
    <div style={{ padding: '8px 12px', background: C.header }}>
      <div style={{
        background: '#fff', borderRadius: 24,
        padding: '8px 14px', display: 'flex', alignItems: 'center', gap: 8,
      }}>
        <span style={{ color: C.muted, fontSize: 14 }}>🔍</span>
        <input
          value={value} onChange={e => onChange(e.target.value)}
          placeholder={placeholder}
          style={{
            border: 'none', outline: 'none', flex: 1,
            fontSize: 14, color: C.text, background: 'transparent',
          }}
        />
        {value && (
          <button onClick={() => onChange('')} style={{
            background: C.muted, border: 'none', borderRadius: '50%',
            width: 18, height: 18, color: '#fff', fontSize: 11,
            cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}>×</button>
        )}
      </div>
    </div>
  );
}

// ── BottomNav ─────────────────────────────────────────────────────────
function BottomNav({ active, onChange, badges = {} }) {
  const tabs = [
    { id: 'dashboard',    icon: '⊞', label: 'Home' },
    { id: 'appointments', icon: '📋', label: 'Appointments' },
    { id: 'leads',        icon: '⚡', label: 'Leads' },
    { id: 'followups',    icon: '🔔', label: 'Follow-ups' },
    { id: 'more',         icon: '···', label: 'More' },
  ];
  return (
    <nav style={{
      background: '#fff', borderTop: '1px solid #e5e7eb',
      display: 'flex', padding: '6px 0 8px', flexShrink: 0,
    }}>
      {tabs.map(t => (
        <button key={t.id} onClick={() => onChange(t.id)} style={{
          flex: 1, background: 'none', border: 'none', cursor: 'pointer',
          display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2,
          padding: '4px 0', position: 'relative',
        }}>
          <span style={{ fontSize: 18, lineHeight: 1, opacity: active === t.id ? 1 : 0.45 }}>{t.icon}</span>
          <span style={{
            fontSize: 10, fontWeight: active === t.id ? 700 : 400,
            color: active === t.id ? C.teal : C.muted,
          }}>{t.label}</span>
          {badges[t.id] > 0 && (
            <span style={{
              position: 'absolute', top: 2, right: '50%', transform: 'translateX(8px)',
              background: C.green, color: '#fff', fontSize: 9, fontWeight: 700,
              borderRadius: 10, padding: '1px 5px', minWidth: 16, textAlign: 'center',
            }}>{badges[t.id]}</span>
          )}
          {active === t.id && (
            <div style={{
              position: 'absolute', top: 0, left: '50%', transform: 'translateX(-50%)',
              width: 28, height: 3, borderRadius: '0 0 3px 3px', background: C.teal,
            }} />
          )}
        </button>
      ))}
    </nav>
  );
}

// ── SideNav (desktop) ─────────────────────────────────────────────────
function SideNav({ active, onChange, badges = {} }) {
  const tabs = [
    { id: 'dashboard',    icon: '⊞', label: 'Dashboard' },
    { id: 'appointments', icon: '📋', label: 'Appointments' },
    { id: 'leads',        icon: '⚡', label: 'Priority Leads' },
    { id: 'followups',    icon: '🔔', label: 'Follow-ups' },
    { id: 'jobs',         icon: '🔨', label: 'Job Appointments' },
    { id: 'templates',    icon: '📄', label: 'Templates' },
    { id: 'new_quote',    icon: '＋', label: 'New Quotation' },
  ];
  return (
    <nav style={{
      width: 220, background: C.header, display: 'flex', flexDirection: 'column',
      padding: '0 0 16px', flexShrink: 0,
    }}>
      <div style={{ padding: '20px 16px 16px', borderBottom: '1px solid rgba(255,255,255,0.1)' }}>
        <div style={{ color: '#fff', fontSize: 18, fontWeight: 700 }}>Plumbot</div>
        <div style={{ color: 'rgba(255,255,255,0.5)', fontSize: 12 }}>Admin Panel</div>
      </div>
      <div style={{ flex: 1, padding: '8px 8px', display: 'flex', flexDirection: 'column', gap: 2 }}>
        {tabs.map(t => (
          <button key={t.id} onClick={() => onChange(t.id)} style={{
            background: active === t.id ? 'rgba(255,255,255,0.15)' : 'none',
            border: 'none', borderRadius: 8, cursor: 'pointer',
            padding: '10px 12px', display: 'flex', alignItems: 'center', gap: 10,
            textAlign: 'left', position: 'relative',
          }}>
            <span style={{ fontSize: 16, opacity: active === t.id ? 1 : 0.6 }}>{t.icon}</span>
            <span style={{
              color: active === t.id ? '#fff' : 'rgba(255,255,255,0.65)',
              fontSize: 13, fontWeight: active === t.id ? 600 : 400, flex: 1,
            }}>{t.label}</span>
            {badges[t.id] > 0 && (
              <span style={{
                background: C.green, color: '#fff', fontSize: 10, fontWeight: 700,
                borderRadius: 10, padding: '1px 6px', minWidth: 18, textAlign: 'center',
              }}>{badges[t.id]}</span>
            )}
          </button>
        ))}
      </div>
    </nav>
  );
}

// ── Divider ───────────────────────────────────────────────────────────
function ListDivider() {
  return <div style={{ height: 1, background: '#f0f0f0', margin: '0 16px' }} />;
}

// ── EmptyState ────────────────────────────────────────────────────────
function EmptyState({ icon = '📭', title, sub }) {
  return (
    <div style={{ textAlign: 'center', padding: '48px 24px', color: C.muted }}>
      <div style={{ fontSize: 40, marginBottom: 12, opacity: 0.4 }}>{icon}</div>
      <div style={{ fontSize: 15, fontWeight: 600, color: '#374151', marginBottom: 4 }}>{title}</div>
      {sub && <div style={{ fontSize: 13 }}>{sub}</div>}
    </div>
  );
}

Object.assign(window, { C, HEAT, STATUS, Avatar, StatusPill, HeatPill, ScoreDot, TopBar, SearchBar, BottomNav, SideNav, ListDivider, EmptyState });
