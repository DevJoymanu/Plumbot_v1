// Mock data for Plumbot prototype
const MOCK_DATA = {
  appointments: [
    {
      id: 1, customer_name: 'Ahmad Yusof', phone: '+60 12-345 6789',
      service: 'Bathroom Renovation', area: 'Petaling Jaya',
      status: 'confirmed', lead_status: 'very_hot', score: 92,
      scheduled: '2026-04-23T09:00', follow_up: 'in_progress',
      description: 'Full bathroom reno — retile, new fixtures, replace pipes.',
      admin_notes: 'Serious buyer. Has budget confirmed. Site visit done.',
      type: 'site_visit', chatbot_paused: false,
      conversation: [
        { role: 'assistant', content: 'Hi Ahmad! Thanks for reaching out to Homebase Plumbers. What plumbing work can we help you with today?', time: '09:12' },
        { role: 'user', content: 'Hello, I want to renovate my bathroom. Replace all the tiles and piping.', time: '09:14' },
        { role: 'assistant', content: 'Great! That sounds like a full bathroom reno. Could you share your area and when you\'d like the work done?', time: '09:14' },
        { role: 'user', content: 'I\'m in PJ. Hoping to get it done next month.', time: '09:17' },
        { role: 'assistant', content: 'Perfect — we cover Petaling Jaya. I\'ve scheduled a site visit for April 23 at 9am. Our team will assess and provide a full quotation.', time: '09:18' },
        { role: 'user', content: 'Sounds good. What should I prepare?', time: '09:22' },
        { role: 'assistant', content: 'Just ensure access to the bathroom. Bring any inspiration photos if you have them. See you on the 23rd!', time: '09:22' },
      ]
    },
    {
      id: 2, customer_name: 'Lim Wei Chen', phone: '+60 11-234 5678',
      service: 'Pipe Leak Repair', area: 'Subang Jaya',
      status: 'confirmed', lead_status: 'hot', score: 78,
      scheduled: '2026-04-23T11:30', follow_up: 'waiting_customer',
      description: 'Leaking pipe under kitchen sink. Urgent fix needed.',
      admin_notes: 'Sent quotation. Waiting for acceptance.',
      type: 'site_visit', chatbot_paused: true,
      conversation: [
        { role: 'assistant', content: 'Hi Lim! How can Homebase Plumbers help you today?', time: '10:05' },
        { role: 'user', content: 'I have a leaking pipe under my kitchen sink. It\'s been dripping since yesterday.', time: '10:08' },
        { role: 'assistant', content: 'That needs urgent attention! We can come by today or tomorrow. Are you in SS15, Subang Jaya?', time: '10:09' },
        { role: 'user', content: 'Yes SS15. Can you come today?', time: '10:11' },
        { role: 'assistant', content: 'Our plumber can be there at 11:30am today. I\'ve sent you a preliminary quote via WhatsApp.', time: '10:12' },
      ]
    },
    {
      id: 3, customer_name: 'Raj Kumar', phone: '+60 16-789 0123',
      service: 'Water Heater Installation', area: 'Shah Alam',
      status: 'pending', lead_status: 'warm', score: 64,
      scheduled: null, follow_up: 'pending',
      description: 'Replace old water heater with a new instant heater.',
      admin_notes: '',
      type: 'site_visit', chatbot_paused: false,
      conversation: [
        { role: 'assistant', content: 'Hi Raj! Thanks for contacting Homebase Plumbers. What can we help you with?', time: '14:30' },
        { role: 'user', content: 'My water heater is old and I want to replace it. What are my options?', time: '14:35' },
        { role: 'assistant', content: 'We offer instant heaters, storage tanks, and solar options. Could you tell me your current setup and budget range?', time: '14:36' },
        { role: 'user', content: 'Currently have a storage tank. Budget around RM1500-2000.', time: '14:42' },
        { role: 'assistant', content: 'Great budget range! An instant heater with installation would come to roughly RM1,800. Shall I book a free site assessment?', time: '14:43' },
      ]
    },
    {
      id: 4, customer_name: 'Nurul Ain Binti Hassan', phone: '+60 17-456 7890',
      service: 'Drain Blockage', area: 'Ampang',
      status: 'pending', lead_status: 'warm', score: 58,
      scheduled: null, follow_up: 'pending',
      description: 'Bathroom drain completely blocked.',
      admin_notes: '',
      type: 'site_visit', chatbot_paused: false,
      conversation: [
        { role: 'assistant', content: 'Hi! How can Homebase Plumbers help today?', time: '16:00' },
        { role: 'user', content: 'My bathroom drain is completely blocked. Water won\'t drain at all.', time: '16:03' },
        { role: 'assistant', content: 'We can clear that for you — typical drain unblocking takes 30–60 mins. Are you available tomorrow?', time: '16:04' },
      ]
    },
    {
      id: 5, customer_name: 'David Tan', phone: '+60 12-890 1234',
      service: 'Full Plumbing — New House', area: 'Klang',
      status: 'pending', lead_status: 'hot', score: 82,
      scheduled: null, follow_up: 'in_progress',
      description: 'New house build — complete plumbing installation.',
      admin_notes: 'Big job. Follow up ASAP.',
      type: 'site_visit', chatbot_paused: false,
      conversation: [
        { role: 'assistant', content: 'Hi David! Thanks for reaching out. What plumbing project can we help with?', time: '08:45' },
        { role: 'user', content: 'I\'m building a new house and need full plumbing installed. 4 bedrooms, 3 bathrooms.', time: '08:50' },
        { role: 'assistant', content: 'That\'s a significant project! We specialise in new builds. Can you share the location and expected timeline?', time: '08:51' },
        { role: 'user', content: 'Klang area. Construction starts in 3 months.', time: '08:55' },
      ]
    },
    {
      id: 6, customer_name: 'Siti Rahimah', phone: '+60 19-345 6780',
      service: 'Toilet Cistern Repair', area: 'Puchong',
      status: 'cancelled', lead_status: 'cold', score: 20,
      scheduled: null, follow_up: 'closed_lost',
      description: 'Toilet not flushing properly.',
      admin_notes: 'Cancelled — went with another vendor.',
      type: 'site_visit', chatbot_paused: false,
      conversation: [
        { role: 'assistant', content: 'Hi Siti! How can we help you today?', time: '11:00' },
        { role: 'user', content: 'My toilet isn\'t flushing. The cistern is broken.', time: '11:05' },
        { role: 'assistant', content: 'We can fix that! Cistern repairs typically cost RM120–250. Are you free this week?', time: '11:06' },
        { role: 'user', content: 'Actually I already found someone else. Thanks anyway.', time: '11:20' },
      ]
    },
  ],

  followups: [
    { id: 1, name: 'Raj Kumar', note: 'Site visit done — schedule job?', urgent: true, time: '2d ago', appt_id: 3 },
    { id: 2, name: 'Lim Wei Chen', note: 'Waiting on quote approval', urgent: true, time: '3d ago', appt_id: 2 },
    { id: 3, name: 'David Tan', note: 'New house build — big job, follow up ASAP', urgent: true, time: 'Today', appt_id: 5 },
    { id: 4, name: 'Nurul Ain', note: 'Drain blockage — never replied after last msg', urgent: false, time: '5d ago', appt_id: 4 },
  ],

  // Tomorrow & this week appointments (hardcoded relative to Apr 23 2026)
  upcoming: [
    // Tomorrow Apr 24
    { id:101, customer_name:'Farid Harun',      phone:'+60 12-111 2222', service:'Toilet Fix',          area:'Bangsar',      status:'confirmed', scheduled:'2026-04-24T10:00', score:70, lead_status:'warm' },
    { id:102, customer_name:'Mei Ling Tan',     phone:'+60 11-333 4444', service:'Kitchen Sink Repair', area:'Mont Kiara',   status:'confirmed', scheduled:'2026-04-24T13:00', score:65, lead_status:'warm' },
    { id:103, customer_name:'Priya Sharma',     phone:'+60 16-555 6666', service:'Water Pressure Issue',area:'Damansara',    status:'pending',   scheduled:'2026-04-24T15:30', score:58, lead_status:'luke_warm' },
    // Wed Apr 25
    { id:104, customer_name:'Hassan Zainudin',  phone:'+60 17-777 8888', service:'Bathroom Reno Quote', area:'Cheras',       status:'confirmed', scheduled:'2026-04-25T09:30', score:80, lead_status:'hot' },
    { id:105, customer_name:'Jenny Koay',       phone:'+60 12-999 0000', service:'Pipe Leak',           area:'Kepong',       status:'confirmed', scheduled:'2026-04-25T14:00', score:72, lead_status:'warm' },
    // Thu Apr 26
    { id:106, customer_name:'Ravi Subramaniam', phone:'+60 14-222 3333', service:'Full Bathroom Reno',  area:'Setapak',      status:'pending',   scheduled:'2026-04-26T11:00', score:85, lead_status:'hot' },
    // Fri Apr 27
    { id:107, customer_name:'Aisyah Mohd',      phone:'+60 19-444 5555', service:'Water Heater',        area:'Wangsa Maju',  status:'confirmed', scheduled:'2026-04-27T10:00', score:60, lead_status:'warm' },
    { id:108, customer_name:'Brian Loh',        phone:'+60 11-666 7777', service:'Drain Clearing',      area:'Setia Alam',   status:'confirmed', scheduled:'2026-04-27T16:00', score:55, lead_status:'luke_warm' },
  ],

  jobs: [
    { id:201, customer_name:'Ahmad Yusof',   service:'Bathroom Reno — Installation', area:'Petaling Jaya', scheduled:'2026-04-23T09:00', status:'in_progress', duration:'4h' },
    { id:202, customer_name:'David Tan',     service:'New House Plumbing Phase 1',   area:'Klang',         scheduled:'2026-04-25T08:00', status:'scheduled',   duration:'6h' },
    { id:203, customer_name:'Hassan Zainudin',service:'Full Bathroom — Tiling',      area:'Cheras',        scheduled:'2026-04-26T09:00', status:'scheduled',   duration:'5h' },
  ],

  stats: {
    today_jobs: 3,
    hot_leads: 5,
    pending_followups: 4,
    total_this_week: 18,
    revenue_month: 'RM 24,800',
  }
};

Object.assign(window, { MOCK_DATA });
