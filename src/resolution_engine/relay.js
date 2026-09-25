const $ = id => document.getElementById(id);
let token = '';
const show = (id, value) => { $(id).textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2); };
async function api(path, payload) {
  token = $('credential').value;
  const response = await fetch(path, { method: payload ? 'POST' : 'GET', headers: { Authorization: `Bearer ${token}`, ...(payload ? {'Content-Type':'application/json'} : {}) }, body: payload ? JSON.stringify(payload) : undefined, cache: 'no-store' });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
function run(fn) { return async () => { try { $('status').textContent = ''; await fn(); } catch (error) { $('status').textContent = error.message; } }; }
function ticket(id) { const value = Number($(id).value); if (!Number.isSafeInteger(value) || value < 1) throw new Error('Ticket ID must be a positive integer'); return value; }
async function queue() {
  const data = await api('/api/queue');
  const root = $('queue'); root.replaceChildren();
  const summary = document.createElement('p'); summary.textContent = JSON.stringify(data.summary); root.append(summary);
  const table = document.createElement('table');
  const head = document.createElement('tr'); for (const name of ['Draft ID','Status','Article','Reviewer','Created']) { const th = document.createElement('th'); th.textContent = name; head.append(th); } table.append(head);
  for (const draft of data.drafts) { const row = document.createElement('tr'); for (const key of ['draft_id','status','article_id','reviewer','created_at']) { const cell = document.createElement('td'); cell.textContent = draft[key] ?? '—'; row.append(cell); } table.append(row); }
  root.append(table);
}
$('load').addEventListener('click', run(queue));
$('prepare').addEventListener('click', run(async () => {
  const data = await api('/api/prepare', { ticket_id: ticket('prepare-ticket'), locale: $('prepare-locale').value });
  show('prepared', data); if (data.draft.draft_id) { $('review-draft').value = data.draft.draft_id; $('review-ticket').value = $('prepare-ticket').value; $('review-locale').value = $('prepare-locale').value; } await queue();
}));
$('preview').addEventListener('click', run(async () => { show('previewed', await api('/api/preview', { draft_id: $('review-draft').value, ticket_id: ticket('review-ticket'), locale: $('review-locale').value })); }));
for (const decision of ['approve','reject']) $(''+decision).addEventListener('click', run(async () => {
  if (!window.confirm(`${decision.toUpperCase()} draft ${$('review-draft').value}?`)) return;
  show('previewed', await api('/api/review', { draft_id: $('review-draft').value, decision })); await queue();
}));
