// Screen components — exported to window

// ── Dashboard ─────────────────────────────────────────────────────────
function ScheduleSection({ title, items, onSelect, emptyMsg, accent }) {
  const [collapsed, setCollapsed] = React.useState(false);
  return (
    <div style={{ background:'#fff', marginBottom:8 }}>
      <button
        onClick={() => setCollapsed(c => !c)}
        style={{ width:'100%', padding:'12px 16px 6px', display:'flex', alignItems:'center', justifyContent:'space-between', background:'none', border:'none', cursor:'pointer' }}
      >
        <div style={{ display:'flex', alignItems:'center', gap:8 }}>
          {accent && <div style={{ width:3, height:16, borderRadius:2, background:accent }} />}
          <div style={{ fontSize:13, fontWeight:700, color:'#374151', textTransform:'uppercase', letterSpacing:.6 }}>{title}</div>
          <span style={{ fontSize:11, fontWeight:700, background: accent+'22', color:accent, borderRadius:10, padding:'1px 7px' }}>{items.length}</span>
        </div>
        <span style={{ color:C.muted, fontSize:14, transform: collapsed?'rotate(-90deg)':'rotate(90deg)', transition:'transform .2s' }}>›</span>
      </button>
      {!collapsed && (
        items.length === 0
          ? <div style={{ padding:'12px 16px', fontSize:13, color:C.muted }}>{emptyMsg}</div>
          : items.map((appt, i) => (
            <div key={appt.id}>
              <div
                onClick={() => onSelect(appt)}
                style={{ padding:'10px 16px', display:'flex', alignItems:'center', gap:12, cursor:'pointer' }}
                onMouseEnter={e=>e.currentTarget.style.background='#f9fafb'}
                onMouseLeave={e=>e.currentTarget.style.background='#fff'}
              >
                <div style={{
                  width:46, height:46, borderRadius:10,
                  background: appt.status==='confirmed' ? C.greenSoft : C.amberBg,
                  display:'flex', flexDirection:'column', alignItems:'center', justifyContent:'center', flexShrink:0,
                }}>
                  <div style={{ fontSize:11, fontWeight:800, color: appt.status==='confirmed'?'#059669':'#d97706', lineHeight:1.1, textAlign:'center' }}>
                    {appt.scheduled ? new Date(appt.scheduled).toTimeString().slice(0,5) : '--:--'}
                  </div>
                </div>
                <Avatar name={appt.customer_name} size={42} />
                <div style={{ flex:1, minWidth:0 }}>
                  <div style={{ fontSize:14, fontWeight:600, color:C.text, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
                    {appt.customer_name}
                  </div>
                  <div style={{ fontSize:12, color:C.muted }}>{appt.service} · {appt.area}</div>
                </div>
                <div style={{ display:'flex', flexDirection:'column', alignItems:'flex-end', gap:4 }}>
                  <StatusPill status={appt.status} small />
                  {'score' in appt && <ScoreDot score={appt.score} />}
                </div>
              </div>
              {i < items.length-1 && <ListDivider />}
            </div>
          ))
      )}
    </div>
  );
}

function DashboardScreen({ onNavigate, onSelectAppt }) {
  const { appointments, upcoming = [], jobs = [], stats, followups } = MOCK_DATA;
  const today = appointments.filter(a => a.scheduled && a.status === 'confirmed');
  const tomorrow = upcoming.filter(a => new Date(a.scheduled).toDateString() === new Date('2026-04-24').toDateString());
  const thisWeek = upcoming.filter(a => new Date(a.scheduled) > new Date('2026-04-24T23:59'));
  const weekJobs = jobs.filter(j => j.status === 'scheduled' || j.status === 'in_progress');
  const hotAlerts = appointments.filter(a => ['very_hot','hot'].includes(a.lead_status) && a.status !== 'cancelled');

  return (
    <div style={{ display:'flex', flexDirection:'column', height:'100%', overflow:'hidden' }}>
      <TopBar
        title="Plumbot"
        subtitle={new Date().toLocaleDateString('en-MY', { weekday:'long', day:'numeric', month:'long' })}
        right={
          <div style={{ width:34, height:34, borderRadius:'50%', background:'rgba(255,255,255,0.2)',
            display:'flex', alignItems:'center', justifyContent:'center', color:'#fff', fontSize:15 }}>
            👤
          </div>
        }
      />

      <div style={{ flex:1, overflowY:'auto', background:C.bg }}>
        {/* Alert banner */}
        {hotAlerts.length > 0 && (
          <div
            onClick={() => onNavigate('leads')}
            style={{
              background:'#fff', borderBottom:`3px solid ${C.red}`,
              padding:'10px 16px', display:'flex', alignItems:'center', gap:10, cursor:'pointer',
            }}>
            <span style={{ fontSize:18 }}>🔥</span>
            <div style={{ flex:1 }}>
              <div style={{ fontSize:13, fontWeight:700, color:'#111' }}>
                {hotAlerts.length} priority leads need attention
              </div>
              <div style={{ fontSize:12, color:C.muted }}>Tap to view priority leads</div>
            </div>
            <span style={{ color:C.muted, fontSize:18 }}>›</span>
          </div>
        )}

        {/* Stats row */}
        <div style={{ display:'grid', gridTemplateColumns:'repeat(5,1fr)', gap:0, background:'#fff', marginBottom:8 }}>
          {[
            { n: today.length,            label: "Today's Appts", color: C.teal,  bg:'#e6f7f5' },
            { n: stats.today_jobs,        label: "Today's Jobs",  color:'#0ea5e9',bg:'#e0f2fe' },
            { n: stats.hot_leads,         label: 'Hot Leads',     color: C.red,   bg:'#fff0f0' },
            { n: stats.pending_followups, label: 'Follow-ups',    color:'#d97706',bg:'#fffbeb' },
            { n: stats.total_this_week,   label: 'This Week',     color: C.muted, bg:'#f9fafb' },
          ].map((s,i) => (
            <div key={i} style={{ padding:'14px 8px', textAlign:'center', background:s.bg, borderRight: i<4 ? '1px solid #f0f0f0':'none' }}>
              <div style={{ fontSize:22, fontWeight:800, color:s.color, lineHeight:1 }}>{s.n}</div>
              <div style={{ fontSize:10, color:C.muted, marginTop:3, lineHeight:1.2 }}>{s.label}</div>
            </div>
          ))}
        </div>

        {/* Today's schedule */}
        <ScheduleSection
          title="Today's Appointments"
          items={today}
          onSelect={onSelectAppt}
          emptyMsg="No appointments today"
          accent={C.teal}
        />

        {/* Tomorrow */}
        <ScheduleSection
          title="Tomorrow"
          items={tomorrow}
          onSelect={onSelectAppt}
          emptyMsg="No appointments tomorrow"
          accent="#8b5cf6"
        />

        {/* This week */}
        <ScheduleSection
          title="Later This Week"
          items={thisWeek}
          onSelect={onSelectAppt}
          emptyMsg="Nothing else this week"
          accent={C.amber}
        />

        {/* Jobs this week */}
        {weekJobs.length > 0 && (
          <ScheduleSection
            title="Jobs This Week"
            items={weekJobs.map(j => ({ ...j, status: j.status === 'in_progress' ? 'confirmed' : 'pending', score: undefined }))}
            onSelect={() => {}}
            emptyMsg=""
            accent={C.red}
          />
        )}

        {/* Pending follow-ups */}
        <div style={{ background:'#fff', marginBottom:8 }}>
          <div style={{ padding:'12px 16px 6px', display:'flex', alignItems:'center', justifyContent:'space-between' }}>
            <div style={{ fontSize:13, fontWeight:700, color:'#374151', textTransform:'uppercase', letterSpacing:.6 }}>Pending Follow-ups</div>
            <button onClick={() => onNavigate('followups')} style={{
              background:'none', border:'none', color:C.teal, fontSize:13, fontWeight:600, cursor:'pointer'
            }}>See all ›</button>
          </div>
          {followups.filter(f=>f.urgent).slice(0,3).map((f,i) => (
            <div key={f.id}>
              <div
                onClick={() => { const a = appointments.find(a=>a.id===f.appt_id); if(a) onSelectAppt(a); }}
                style={{ padding:'10px 16px', display:'flex', alignItems:'center', gap:12, cursor:'pointer' }}>
                <Avatar name={f.name} size={40} />
                <div style={{ flex:1 }}>
                  <div style={{ fontSize:14, fontWeight:600, color:C.text }}>{f.name}</div>
                  <div style={{ fontSize:12, color:C.muted }}>{f.note}</div>
                </div>
                <div style={{ fontSize:11, color:f.urgent ? C.red : C.muted, fontWeight:600 }}>{f.time}</div>
              </div>
              {i < 2 && <ListDivider />}
            </div>
          ))}
        </div>

        {/* Quick actions */}
        <div style={{ background:'#fff', marginBottom:8, padding:'12px 16px' }}>
          <div style={{ fontSize:13, fontWeight:700, color:'#374151', textTransform:'uppercase', letterSpacing:.6, marginBottom:10 }}>Quick Actions</div>
          <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:8 }}>
            {[
              { label:'New Quotation', icon:'📝', color:C.teal },
              { label:'Priority Leads', icon:'⚡', color:C.red, action:()=>onNavigate('leads') },
              { label:'All Appointments', icon:'📋', color:'#8b5cf6', action:()=>onNavigate('appointments') },
              { label:'Follow-ups', icon:'🔔', color:'#d97706', action:()=>onNavigate('followups') },
            ].map((a,i) => (
              <button key={i} onClick={a.action} style={{
                background:'#f9fafb', border:`1px solid #e5e7eb`, borderRadius:12,
                padding:'12px 14px', display:'flex', alignItems:'center', gap:10,
                cursor:'pointer', textAlign:'left',
              }}>
                <span style={{ fontSize:20 }}>{a.icon}</span>
                <span style={{ fontSize:13, fontWeight:600, color:C.text }}>{a.label}</span>
              </button>
            ))}
          </div>
        </div>

        <div style={{ height:16 }} />
      </div>
    </div>
  );
}

// ── Appointments List ─────────────────────────────────────────────────
function AppointmentsScreen({ onSelectAppt }) {
  const [search, setSearch] = React.useState('');
  const [filter, setFilter] = React.useState('all');
  const { appointments } = MOCK_DATA;

  const counts = {
    all: appointments.length,
    confirmed: appointments.filter(a=>a.status==='confirmed').length,
    pending: appointments.filter(a=>a.status==='pending').length,
    cancelled: appointments.filter(a=>a.status==='cancelled').length,
  };

  const filtered = appointments.filter(a => {
    const matchSearch = !search || a.customer_name.toLowerCase().includes(search.toLowerCase())
      || a.phone.includes(search) || a.service.toLowerCase().includes(search.toLowerCase());
    const matchFilter = filter === 'all' || a.status === filter;
    return matchSearch && matchFilter;
  });

  const tabs = [
    { id:'all',       label:'All',       count: counts.all },
    { id:'confirmed', label:'Booked',    count: counts.confirmed, color: '#059669' },
    { id:'pending',   label:'Pending',   count: counts.pending,   color: '#d97706' },
    { id:'cancelled', label:'Cancelled', count: counts.cancelled, color: C.red },
  ];

  return (
    <div style={{ display:'flex', flexDirection:'column', height:'100%', overflow:'hidden' }}>
      <TopBar title="Appointments" subtitle={`${counts.all} total`} />
      <SearchBar value={search} onChange={setSearch} placeholder="Search name, phone, service…" />

      {/* Filter tabs */}
      <div style={{
        background:'#fff', display:'flex', borderBottom:'1px solid #f0f0f0',
        overflowX:'auto', scrollbarWidth:'none',
      }}>
        {tabs.map(t => (
          <button key={t.id} onClick={() => setFilter(t.id)} style={{
            flex:1, minWidth:60, padding:'10px 4px', border:'none', background:'none',
            cursor:'pointer', position:'relative', whiteSpace:'nowrap',
          }}>
            <span style={{ fontSize:13, fontWeight: filter===t.id ? 700 : 500, color: filter===t.id ? (t.color||C.teal) : C.muted }}>
              {t.label}
            </span>
            <span style={{
              display:'inline-block', marginLeft:4,
              background: filter===t.id ? (t.color||C.teal) : '#e5e7eb',
              color: filter===t.id ? '#fff' : C.muted,
              borderRadius:10, fontSize:10, fontWeight:700,
              padding:'1px 5px',
            }}>{t.count}</span>
            {filter===t.id && (
              <div style={{ position:'absolute', bottom:0, left:0, right:0, height:2, background:t.color||C.teal }} />
            )}
          </button>
        ))}
      </div>

      {/* List */}
      <div style={{ flex:1, overflowY:'auto', background:C.bg }}>
        <div style={{ background:'#fff' }}>
          {filtered.length === 0
            ? <EmptyState icon="📋" title="No appointments found" sub="Try adjusting your search or filter" />
            : filtered.map((appt, i) => (
              <div key={appt.id}>
                <ApptListRow appt={appt} onClick={() => onSelectAppt(appt)} />
                {i < filtered.length-1 && <ListDivider />}
              </div>
            ))
          }
        </div>
      </div>
    </div>
  );
}

function ApptListRow({ appt, onClick }) {
  const lastMsg = appt.conversation[appt.conversation.length - 1];
  const heat = HEAT[appt.lead_status] || HEAT.cold;

  return (
    <div onClick={onClick} style={{
      padding:'12px 16px', display:'flex', alignItems:'center', gap:12,
      cursor:'pointer', background:'#fff', transition:'background .15s',
    }}
    onMouseEnter={e => e.currentTarget.style.background='#f9fafb'}
    onMouseLeave={e => e.currentTarget.style.background='#fff'}
    >
      <Avatar name={appt.customer_name} size={50} />
      <div style={{ flex:1, minWidth:0 }}>
        <div style={{ display:'flex', alignItems:'baseline', justifyContent:'space-between', gap:8, marginBottom:2 }}>
          <div style={{ fontSize:15, fontWeight:600, color:C.text, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap', flex:1 }}>
            {appt.customer_name}
          </div>
          <div style={{ fontSize:11, color:C.muted, flexShrink:0 }}>
            {appt.scheduled ? new Date(appt.scheduled).toTimeString().slice(0,5) : '—'}
          </div>
        </div>
        <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', gap:8 }}>
          <div style={{ fontSize:12, color:C.muted, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap', flex:1 }}>
            {appt.service} · {appt.area}
          </div>
          <div style={{ display:'flex', gap:4, flexShrink:0, alignItems:'center' }}>
            <div style={{
              width:10, height:10, borderRadius:'50%', background:heat.dot, flexShrink:0,
            }} />
            <StatusPill status={appt.status} small />
          </div>
        </div>
        {lastMsg && (
          <div style={{ fontSize:12, color:'#9ca3af', marginTop:2, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
            {lastMsg.role === 'user' ? '👤 ' : '🤖 '}{lastMsg.content.slice(0,55)}…
          </div>
        )}
      </div>
    </div>
  );
}

// ── Appointment Detail ────────────────────────────────────────────────
function AppointmentDetailScreen({ appt, onBack, isDesktop }) {
  const [tab, setTab] = React.useState('chat');
  const [message, setMessage] = React.useState('');
  const [msgs, setMsgs] = React.useState(appt.conversation);
  const chatEndRef = React.useRef(null);

  React.useEffect(() => {
    setMsgs(appt.conversation);
    setTab('chat');
  }, [appt.id]);

  React.useEffect(() => {
    if (chatEndRef.current) chatEndRef.current.scrollIntoView({ behavior:'smooth' });
  }, [msgs]);

  const heat = HEAT[appt.lead_status] || HEAT.cold;

  function sendMessage() {
    if (!message.trim()) return;
    setMsgs(m => [...m, { role:'assistant', content: message, time: new Date().toTimeString().slice(0,5) }]);
    setMessage('');
  }

  return (
    <div style={{ display:'flex', flexDirection:'column', height:'100%', overflow:'hidden', background:C.bg }}>
      {/* Header */}
      <div style={{ background:C.header, padding:'10px 16px', display:'flex', alignItems:'center', gap:12, flexShrink:0 }}>
        {!isDesktop && (
          <button onClick={onBack} style={{ background:'none', border:'none', color:'#fff', fontSize:20, cursor:'pointer', padding:0 }}>←</button>
        )}
        <Avatar name={appt.customer_name} size={40} bg="rgba(255,255,255,0.25)" />
        <div style={{ flex:1, minWidth:0 }}>
          <div style={{ color:'#fff', fontSize:15, fontWeight:600, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
            {appt.customer_name}
          </div>
          <div style={{ color:'rgba(255,255,255,0.7)', fontSize:11 }}>
            {appt.phone} · {appt.area}
          </div>
        </div>
        <div style={{ display:'flex', gap:8 }}>
          <a href={`tel:${appt.phone.replace(/\s/g,'')}`} style={{
            background:'rgba(255,255,255,0.2)', borderRadius:20,
            padding:'5px 12px', color:'#fff', fontSize:12, fontWeight:600, textDecoration:'none',
          }}>📞 Call</a>
          <a href={`https://wa.me/${appt.phone.replace(/[\s\-+]/g,'')}`} target="_blank" rel="noreferrer" style={{
            background:C.green, borderRadius:20,
            padding:'5px 12px', color:'#fff', fontSize:12, fontWeight:600, textDecoration:'none',
          }}>WhatsApp</a>
        </div>
      </div>

      {/* Status bar */}
      <div style={{ background:'#fff', padding:'8px 16px', display:'flex', gap:8, alignItems:'center', flexWrap:'wrap', borderBottom:'1px solid #f0f0f0', flexShrink:0 }}>
        <StatusPill status={appt.status} />
        <HeatPill heat={appt.lead_status} />
        <ScoreDot score={appt.score} />
        <span style={{ fontSize:12, color:C.muted }}>Score {appt.score}/100</span>
        {appt.chatbot_paused && (
          <span style={{ fontSize:11, background:'#fef3c7', color:'#92400e', padding:'2px 8px', borderRadius:20, fontWeight:700 }}>
            ⏸ Bot paused
          </span>
        )}
      </div>

      {/* Tabs */}
      <div style={{ background:'#fff', display:'flex', borderBottom:'1px solid #f0f0f0', flexShrink:0 }}>
        {['chat','info','quotations'].map(t => (
          <button key={t} onClick={() => setTab(t)} style={{
            flex:1, padding:'10px', border:'none', background:'none', cursor:'pointer',
            fontSize:13, fontWeight: tab===t ? 700 : 500,
            color: tab===t ? C.teal : C.muted, textTransform:'capitalize',
            borderBottom: tab===t ? `2px solid ${C.teal}` : '2px solid transparent',
          }}>
            {t === 'chat' ? '💬 Chat' : t === 'info' ? '📋 Details' : '📄 Quotes'}
          </button>
        ))}
      </div>

      {/* Tab content */}
      {tab === 'chat' && (
        <>
          {/* Chat messages */}
          <div style={{
            flex:1, overflowY:'auto', padding:'12px 16px',
            backgroundImage:'url("data:image/svg+xml,%3Csvg width=\'20\' height=\'20\' xmlns=\'http://www.w3.org/2000/svg\'%3E%3Ccircle cx=\'2\' cy=\'2\' r=\'1\' fill=\'%23c5b99a22\'/%3E%3C/svg%3E")',
            backgroundColor:'#E5DDD5',
          }}>
            {msgs.map((msg, i) => (
              <div key={i} style={{
                display:'flex', justifyContent: msg.role==='user' ? 'flex-start' : 'flex-end',
                marginBottom:6,
              }}>
                <div style={{
                  maxWidth:'75%', background: msg.role==='user' ? '#fff' : C.greenBg,
                  borderRadius: msg.role==='user' ? '0 10px 10px 10px' : '10px 0 10px 10px',
                  padding:'8px 12px', boxShadow:'0 1px 2px rgba(0,0,0,0.1)',
                }}>
                  <div style={{ fontSize:11, fontWeight:700, color: msg.role==='user' ? C.teal : '#555', marginBottom:3 }}>
                    {msg.role==='user' ? '👤 Customer' : '🤖 Plumbot'}
                  </div>
                  <div style={{ fontSize:13, color:C.text, lineHeight:1.45 }}>{msg.content}</div>
                  <div style={{ fontSize:10, color:'#9ca3af', textAlign:'right', marginTop:4 }}>{msg.time}</div>
                </div>
              </div>
            ))}
            <div ref={chatEndRef} />
          </div>

          {/* Message input */}
          <div style={{ background:'#f0f0f0', padding:'8px 12px', display:'flex', gap:8, alignItems:'flex-end', flexShrink:0 }}>
            <div style={{
              flex:1, background:'#fff', borderRadius:24, padding:'8px 14px',
              display:'flex', alignItems:'center', gap:6,
            }}>
              <textarea
                value={message}
                onChange={e => setMessage(e.target.value)}
                onKeyDown={e => { if (e.key==='Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } }}
                placeholder="Type a message…"
                rows={1}
                style={{
                  border:'none', outline:'none', flex:1, fontSize:14,
                  resize:'none', background:'transparent', color:C.text,
                  fontFamily:'inherit', lineHeight:1.4,
                }}
              />
            </div>
            <button onClick={sendMessage} style={{
              width:44, height:44, borderRadius:'50%', background: message.trim() ? C.teal : '#ccc',
              border:'none', cursor: message.trim() ? 'pointer' : 'default',
              display:'flex', alignItems:'center', justifyContent:'center', fontSize:18, flexShrink:0,
              transition:'background .2s',
            }}>➤</button>
          </div>
        </>
      )}

      {tab === 'info' && (
        <div style={{ flex:1, overflowY:'auto', padding:16 }}>
          <div style={{ background:'#fff', borderRadius:12, padding:16, marginBottom:12 }}>
            <div style={{ fontSize:13, fontWeight:700, color:'#374151', marginBottom:12, textTransform:'uppercase', letterSpacing:.6 }}>Customer Details</div>
            {[
              { label:'Name', value: appt.customer_name },
              { label:'Phone', value: appt.phone },
              { label:'Service', value: appt.service },
              { label:'Area', value: appt.area },
              { label:'Scheduled', value: appt.scheduled ? new Date(appt.scheduled).toLocaleString('en-MY') : 'Not scheduled' },
              { label:'Description', value: appt.description },
            ].map((f,i) => (
              <div key={i} style={{ marginBottom:10 }}>
                <div style={{ fontSize:11, color:C.muted, marginBottom:3 }}>{f.label}</div>
                <div style={{ fontSize:14, color:C.text, background:C.bg, borderRadius:8, padding:'8px 12px' }}>{f.value || '—'}</div>
              </div>
            ))}
          </div>
          <div style={{ background:'#fff', borderRadius:12, padding:16, marginBottom:12 }}>
            <div style={{ fontSize:13, fontWeight:700, color:'#374151', marginBottom:12, textTransform:'uppercase', letterSpacing:.6 }}>Lead Management</div>
            {[
              { label:'Follow-up Status', value: appt.follow_up?.replace(/_/g,' ') },
              { label:'Admin Notes', value: appt.admin_notes || 'No notes yet' },
            ].map((f,i) => (
              <div key={i} style={{ marginBottom:10 }}>
                <div style={{ fontSize:11, color:C.muted, marginBottom:3 }}>{f.label}</div>
                <div style={{ fontSize:14, color:C.text, background:C.bg, borderRadius:8, padding:'8px 12px' }}>{f.value || '—'}</div>
              </div>
            ))}
          </div>
          <div style={{ display:'flex', gap:8, flexWrap:'wrap' }}>
            {[
              { label:'Confirm', bg:C.green, color:'#fff' },
              { label:'Complete', bg:C.teal, color:'#fff' },
              { label:'Cancel', bg:C.redBg, color:C.red },
            ].map((btn,i) => (
              <button key={i} style={{
                flex:1, padding:'12px', borderRadius:10, border:'none',
                background:btn.bg, color:btn.color, fontSize:14, fontWeight:700, cursor:'pointer',
                minWidth:80,
              }}>{btn.label}</button>
            ))}
          </div>
        </div>
      )}

      {tab === 'quotations' && (
        <div style={{ flex:1, overflowY:'auto', padding:16 }}>
          <EmptyState icon="📄" title="No quotations yet" sub="Create a quote for this appointment" />
          <button style={{
            width:'100%', padding:'14px', background:C.teal, color:'#fff',
            border:'none', borderRadius:12, fontSize:15, fontWeight:700, cursor:'pointer',
          }}>+ Create Quotation</button>
        </div>
      )}
    </div>
  );
}

// ── Priority Leads ────────────────────────────────────────────────────
function PriorityLeadsScreen({ onSelectAppt }) {
  const [search, setSearch] = React.useState('');
  const [openSections, setOpenSections] = React.useState({ very_hot:true, hot:true, warm:false, luke_warm:false, cold:false });
  const { appointments } = MOCK_DATA;

  const groups = [
    { key:'very_hot', label:'Very Hot', icon:'🔥', leads: appointments.filter(a=>a.lead_status==='very_hot' && a.status!=='cancelled') },
    { key:'hot',      label:'Hot',      icon:'⚡', leads: appointments.filter(a=>a.lead_status==='hot'      && a.status!=='cancelled') },
    { key:'warm',     label:'Warm',     icon:'☀️', leads: appointments.filter(a=>a.lead_status==='warm'     && a.status!=='cancelled') },
    { key:'luke_warm',label:'Lukewarm', icon:'🌤', leads: appointments.filter(a=>a.lead_status==='luke_warm'&& a.status!=='cancelled') },
    { key:'cold',     label:'Cold',     icon:'❄️', leads: appointments.filter(a=>a.lead_status==='cold'     && a.status!=='cancelled') },
  ];

  const total = groups.reduce((s,g) => s+g.leads.length, 0);

  return (
    <div style={{ display:'flex', flexDirection:'column', height:'100%', overflow:'hidden' }}>
      <TopBar title="Priority Leads" subtitle={`${total} active leads`} />
      <SearchBar value={search} onChange={setSearch} placeholder="Search leads…" />

      {/* Summary chips */}
      <div style={{
        background:'#fff', padding:'8px 12px', display:'flex', gap:6,
        overflowX:'auto', scrollbarWidth:'none', borderBottom:'1px solid #f0f0f0',
      }}>
        {groups.map(g => {
          const h = HEAT[g.key];
          return (
            <button key={g.key} onClick={() => setOpenSections(s=>({...s,[g.key]:true}))} style={{
              background:h.bg, border:`1px solid ${h.dot}44`, borderRadius:20,
              padding:'5px 12px', cursor:'pointer', flexShrink:0, display:'flex', alignItems:'center', gap:5,
            }}>
              <span style={{ fontSize:12 }}>{g.icon}</span>
              <span style={{ fontSize:12, fontWeight:700, color:h.color }}>{g.label}</span>
              <span style={{ fontSize:11, fontWeight:800, color:h.color, background:`${h.dot}22`, borderRadius:10, padding:'0 5px' }}>
                {g.leads.length}
              </span>
            </button>
          );
        })}
      </div>

      <div style={{ flex:1, overflowY:'auto', background:C.bg }}>
        {groups.map(g => {
          if (g.leads.length === 0) return null;
          const h = HEAT[g.key];
          const filtered = g.leads.filter(l =>
            !search || l.customer_name.toLowerCase().includes(search.toLowerCase())
            || l.phone.includes(search)
          );
          if (filtered.length === 0 && search) return null;
          const isOpen = openSections[g.key];

          return (
            <div key={g.key} style={{ marginBottom:8 }}>
              {/* Section header */}
              <button
                onClick={() => setOpenSections(s=>({...s,[g.key]:!s[g.key]}))}
                style={{
                  width:'100%', padding:'12px 16px', background:'#fff',
                  border:'none', borderLeft:`4px solid ${h.dot}`,
                  display:'flex', alignItems:'center', gap:10, cursor:'pointer', textAlign:'left',
                }}>
                <div style={{
                  width:34, height:34, borderRadius:8, background:h.bg,
                  display:'flex', alignItems:'center', justifyContent:'center', fontSize:16, flexShrink:0,
                }}>{g.icon}</div>
                <div style={{ flex:1 }}>
                  <div style={{ fontSize:14, fontWeight:700, color:h.color }}>{g.label} Leads</div>
                  <div style={{ fontSize:12, color:C.muted }}>{g.leads.length} lead{g.leads.length!==1?'s':''}</div>
                </div>
                <span style={{ color:C.muted, fontSize:16, transform: isOpen ? 'rotate(90deg)':'rotate(0deg)', transition:'transform .2s' }}>›</span>
              </button>

              {isOpen && (
                <div style={{ background:'#fff', borderLeft:`4px solid ${h.dot}` }}>
                  {filtered.map((lead, i) => (
                    <div key={lead.id}>
                      <div
                        onClick={() => onSelectAppt(lead)}
                        style={{ padding:'12px 16px', cursor:'pointer', display:'flex', alignItems:'center', gap:12 }}
                      >
                        <Avatar name={lead.customer_name} size={46} />
                        <div style={{ flex:1, minWidth:0 }}>
                          <div style={{ fontSize:14, fontWeight:600, color:C.text }}>{lead.customer_name}</div>
                          <div style={{ fontSize:12, color:C.muted }}>{lead.service} · {lead.area}</div>
                          <div style={{ display:'flex', gap:4, marginTop:4, alignItems:'center' }}>
                            <a href={`tel:${lead.phone}`} onClick={e=>e.stopPropagation()} style={{
                              background:'#dcfce7', color:'#166534', padding:'3px 10px',
                              borderRadius:8, fontSize:11, fontWeight:700, textDecoration:'none',
                            }}>📞 Call</a>
                            <a href={`https://wa.me/${lead.phone.replace(/[\s\-+]/g,'')}`} target="_blank" rel="noreferrer" onClick={e=>e.stopPropagation()} style={{
                              background:'#dcfce7', color:'#166534', padding:'3px 10px',
                              borderRadius:8, fontSize:11, fontWeight:700, textDecoration:'none',
                            }}>💬 WA</a>
                          </div>
                        </div>
                        <div style={{ display:'flex', flexDirection:'column', alignItems:'center', gap:4 }}>
                          <ScoreDot score={lead.score} />
                          <div style={{ fontSize:9, color:C.muted, textAlign:'center' }}>score</div>
                        </div>
                      </div>
                      {i < filtered.length-1 && <ListDivider />}
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
        <div style={{ height:16 }} />
      </div>
    </div>
  );
}

// ── Follow-ups Screen ─────────────────────────────────────────────────
function FollowUpsScreen({ onSelectAppt }) {
  const { followups, appointments } = MOCK_DATA;

  return (
    <div style={{ display:'flex', flexDirection:'column', height:'100%', overflow:'hidden' }}>
      <TopBar title="Follow-ups" subtitle={`${followups.length} pending`} />
      <div style={{ flex:1, overflowY:'auto', background:C.bg }}>
        <div style={{ background:'#fff', marginBottom:8 }}>
          {followups.map((f,i) => {
            const appt = appointments.find(a=>a.id===f.appt_id);
            return (
              <div key={f.id}>
                <div
                  onClick={() => appt && onSelectAppt(appt)}
                  style={{ padding:'12px 16px', display:'flex', alignItems:'flex-start', gap:12, cursor:'pointer' }}
                >
                  <Avatar name={f.name} size={46} />
                  <div style={{ flex:1 }}>
                    <div style={{ display:'flex', justifyContent:'space-between', alignItems:'flex-start', marginBottom:3 }}>
                      <div style={{ fontSize:15, fontWeight:600, color:C.text }}>{f.name}</div>
                      <div style={{ fontSize:11, color: f.urgent ? C.red : C.muted, fontWeight: f.urgent?700:400 }}>{f.time}</div>
                    </div>
                    <div style={{ fontSize:12, color:C.muted, marginBottom:6 }}>{f.note}</div>
                    {f.urgent && (
                      <span style={{ fontSize:10, background:C.redBg, color:C.red, padding:'2px 8px', borderRadius:20, fontWeight:700 }}>
                        Needs attention
                      </span>
                    )}
                  </div>
                </div>
                {i < followups.length-1 && <ListDivider />}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ── More Screen ───────────────────────────────────────────────────────
function MoreScreen() {
  const items = [
    { icon:'🔨', label:'Job Appointments', sub:'View and manage jobs' },
    { icon:'📄', label:'Quotation Templates', sub:'Manage saved templates' },
    { icon:'📝', label:'New Quotation', sub:'Create a standalone quote' },
    { icon:'📅', label:'Calendar', sub:'View schedule calendar' },
    { icon:'⚙️', label:'Settings', sub:'App settings and config' },
    { icon:'👤', label:'Profile', sub:'Manage your account' },
  ];
  return (
    <div style={{ display:'flex', flexDirection:'column', height:'100%', overflow:'hidden' }}>
      <TopBar title="More" />
      <div style={{ flex:1, overflowY:'auto', background:C.bg }}>
        <div style={{ background:'#fff', marginTop:8 }}>
          {items.map((item,i) => (
            <div key={i}>
              <div style={{ padding:'14px 16px', display:'flex', alignItems:'center', gap:14, cursor:'pointer' }}>
                <div style={{ width:40, height:40, borderRadius:10, background:C.bg, display:'flex', alignItems:'center', justifyContent:'center', fontSize:20 }}>
                  {item.icon}
                </div>
                <div style={{ flex:1 }}>
                  <div style={{ fontSize:14, fontWeight:600, color:C.text }}>{item.label}</div>
                  <div style={{ fontSize:12, color:C.muted }}>{item.sub}</div>
                </div>
                <span style={{ color:C.muted, fontSize:18 }}>›</span>
              </div>
              {i < items.length-1 && <ListDivider />}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { DashboardScreen, AppointmentsScreen, ApptListRow, AppointmentDetailScreen, PriorityLeadsScreen, FollowUpsScreen, MoreScreen });
