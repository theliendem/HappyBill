// Same origin when the backend serves this page (http://localhost:8000); otherwise talk to it on port 8000.
const API_BASE = window.HAPPYBILL_API ?? (location.port === '8000' ? '' : 'http://localhost:8000');
const MAX_PAGES = 10;

const tabs = document.querySelectorAll('.input-tab');
const uploadView = document.querySelector('#upload-view');
const manualForm = document.querySelector('#manual-view');
const fileInput = document.querySelector('#bill-file');
const dropArea = document.querySelector('#drop-area');
const fileStatus = document.querySelector('.file-status');
const extractStatus = document.querySelector('.extract-status');
const pastedText = document.querySelector('#bill-text');
const pasteToggle = document.querySelector('#paste-toggle');
const redactionPreview = document.querySelector('#redaction-preview');
const stepError = document.querySelector('#step-error');
const results = document.querySelector('#results-section');
const canvas = document.querySelector('#redacted-canvas');
const canvasFrame = document.querySelector('#canvas-frame');
const confirmRedaction = document.querySelector('#confirm-redaction');
const continueButton = document.querySelector('#continue-button');
const progressRow = document.querySelector('#progress-row');
let activeView = 'upload-view';
let sourceBitmap = null;   // uploaded photo, full resolution (exported for reading, not the small preview)
let pdfDocument = null;
let pdfPageNumber = 1;
let canvasBase = null;
let blackoutBoxes = [];
let pageBlackouts = new Map();
let dragStart = null;
let dragCurrent = null;
let formSource = null;     // the bill the manual form was filled from, so fields the form doesn't show are kept
let busy = false;

tabs.forEach(tab => tab.addEventListener('click', () => showView(tab.dataset.view)));
function showView(view) {
  activeView = view;
  tabs.forEach(item => { const on = item.dataset.view === view; item.classList.toggle('selected', on); item.setAttribute('aria-selected', String(on)); });
  uploadView.classList.toggle('hidden', activeView !== 'upload-view');
  manualForm.classList.toggle('hidden', activeView !== 'manual-view');
  stepError.classList.add('hidden');
}

fileInput.addEventListener('change', async () => { if (fileInput.files?.[0]) await processFile(fileInput.files[0]); });
['dragenter','dragover'].forEach(type => dropArea.addEventListener(type, event => { event.preventDefault(); dropArea.classList.add('over'); }));
['dragleave','drop'].forEach(type => dropArea.addEventListener(type, event => { event.preventDefault(); dropArea.classList.remove('over'); }));
dropArea.addEventListener('drop', async event => { const file = event.dataTransfer.files?.[0]; if (file) await processFile(file); });

// ---------------- opening a bill (all in the browser) ----------------

async function processFile(file) {
  pdfDocument = null; sourceBitmap = null; canvasBase = null; blackoutBoxes = []; pageBlackouts.clear();
  canvasFrame.classList.add('hidden'); redactionPreview.classList.add('hidden');
  document.querySelector('#pdf-pages').classList.add('hidden');
  document.querySelector('#canvas-toolbar').classList.add('hidden');
  confirmRedaction.checked = false;
  extractStatus.classList.add('hidden');
  fileStatus.textContent = '';
  if (file.size > 15 * 1024 * 1024) return extractionError('This file is over 15 MB. Choose a smaller file.');
  try {
    const ext = file.name.split('.').pop().toLowerCase();
    if (['txt','csv'].includes(ext) || file.type.startsWith('text/')) {
      pastedText.value = await file.text();
      pasteToggle.open = true;
      setStatus('Remove your name, address and account numbers from the text below.');
    }
    else if (ext === 'pdf' || file.type === 'application/pdf') {
      await openPdf(file);
      if (pdfDocument.numPages > MAX_PAGES) return extractionError(`This PDF has ${pdfDocument.numPages} pages. Upload just the pages with the charges (up to ${MAX_PAGES}).`);
      showCanvasTools();
    }
    else if (file.type.startsWith('image/')) {
      await openImage(file);
      showCanvasTools();
    }
    else return extractionError('This file type isn’t supported. Choose a PDF, photo, or text file.');
    fileStatus.textContent = file.name;
  } catch (error) {
    extractionError(`Couldn’t open this file. ${error.message || 'Try pasting the text, or type it in.'}`);
  }
}

function showCanvasTools() {
  redactionPreview.classList.remove('hidden');
  document.querySelector('#canvas-toolbar').classList.remove('hidden');
}
function setStatus(message) { extractStatus.classList.remove('hidden'); extractStatus.textContent = message; }
function extractionError(message) {
  setStatus(message);
  pdfDocument = null; sourceBitmap = null; canvasBase = null;
  canvasFrame.classList.add('hidden'); redactionPreview.classList.add('hidden');
}

async function openPdf(file) {
  const pdfjs = await loadPdfJs();
  pdfjs.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
  pdfDocument = await pdfjs.getDocument({ data: await file.arrayBuffer() }).promise;
  pdfPageNumber = 1;
  if (pdfDocument.numPages > 1) document.querySelector('#pdf-pages').classList.remove('hidden');
  await renderPdfPage();
}

function previewScale(page) { return Math.min(1.4, 1000 / page.getViewport({ scale: 1 }).width); }

async function renderPdfPage() {
  if (!pdfDocument) return;
  if (blackoutBoxes.length) pageBlackouts.set(pdfPageNumber, blackoutBoxes);
  const page = await pdfDocument.getPage(pdfPageNumber);
  const viewport = page.getViewport({ scale: previewScale(page) });
  canvas.width = Math.round(viewport.width); canvas.height = Math.round(viewport.height);
  await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;
  canvasBase = canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height);
  blackoutBoxes = pageBlackouts.get(pdfPageNumber) || [];
  drawBlackouts();
  canvasFrame.classList.remove('hidden');
  document.querySelector('#page-count').textContent = `Page ${pdfPageNumber} of ${pdfDocument.numPages}`;
}

async function openImage(file) {
  sourceBitmap = await createImageBitmap(file);
  const scale = Math.min(1, 1000 / sourceBitmap.width, 1500 / sourceBitmap.height);
  canvas.width = Math.round(sourceBitmap.width * scale); canvas.height = Math.round(sourceBitmap.height * scale);
  const context = canvas.getContext('2d'); context.drawImage(sourceBitmap, 0, 0, canvas.width, canvas.height);
  canvasBase = context.getImageData(0, 0, canvas.width, canvas.height);
  canvasFrame.classList.remove('hidden');
  drawBlackouts();
}

function drawBlackouts(previewBox = null) {
  if (!canvasBase) return;
  const context=canvas.getContext('2d');context.putImageData(canvasBase,0,0);
  context.fillStyle='#111';
  for(const box of blackoutBoxes) context.fillRect(box.x,box.y,box.w,box.h);
  if(previewBox){context.fillStyle='#111b';context.fillRect(previewBox.x,previewBox.y,previewBox.w,previewBox.h);}
}

canvas.addEventListener('pointerdown', event => {
  const point=canvasPoint(event);dragStart=point;dragCurrent=point;canvas.setPointerCapture(event.pointerId);
});
canvas.addEventListener('pointermove', event => {
  if(!dragStart)return;dragCurrent=canvasPoint(event);drawBlackouts(boxFromPoints(dragStart,dragCurrent));
});
canvas.addEventListener('pointerup', event => {
  if(!dragStart)return;dragCurrent=canvasPoint(event);const box=boxFromPoints(dragStart,dragCurrent);
  if(box.w>3&&box.h>3){blackoutBoxes.push(box);drawBlackouts();if(pdfDocument)pageBlackouts.set(pdfPageNumber,blackoutBoxes);}
  dragStart=null;dragCurrent=null;
});
function canvasPoint(event){const rect=canvas.getBoundingClientRect();return{x:(event.clientX-rect.left)*canvas.width/rect.width,y:(event.clientY-rect.top)*canvas.height/rect.height};}
function boxFromPoints(a,b){return{x:Math.min(a.x,b.x),y:Math.min(a.y,b.y),w:Math.abs(a.x-b.x),h:Math.abs(a.y-b.y)};}

document.querySelector('#undo-blackout').addEventListener('click',()=>{blackoutBoxes.pop();drawBlackouts();if(pdfDocument)pageBlackouts.set(pdfPageNumber,blackoutBoxes);});
document.querySelector('#clear-blackouts').addEventListener('click',()=>{blackoutBoxes=[];drawBlackouts();if(pdfDocument)pageBlackouts.set(pdfPageNumber,blackoutBoxes);});
document.querySelector('#previous-page').addEventListener('click',async()=>{if(pdfDocument&&pdfPageNumber>1){if(blackoutBoxes.length)pageBlackouts.set(pdfPageNumber,blackoutBoxes);pdfPageNumber--;blackoutBoxes=pageBlackouts.get(pdfPageNumber)||[];await renderPdfPage();}});
document.querySelector('#next-page').addEventListener('click',async()=>{if(pdfDocument&&pdfPageNumber<pdfDocument.numPages){if(blackoutBoxes.length)pageBlackouts.set(pdfPageNumber,blackoutBoxes);pdfPageNumber++;blackoutBoxes=pageBlackouts.get(pdfPageNumber)||[];await renderPdfPage();}});

function loadPdfJs() {
  if (window.pdfjsLib) return Promise.resolve(window.pdfjsLib);
  return loadScript('https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js').then(() => window.pdfjsLib);
}
function loadScript(src) {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[src="${src}"]`);
    if (existing?.dataset.loaded === 'true') return resolve();
    if (existing) { existing.addEventListener('load', resolve, { once: true }); existing.addEventListener('error', reject, { once: true }); return; }
    const script = document.createElement('script'); script.src = src; script.async = true;
    script.onload = () => { script.dataset.loaded = 'true'; resolve(); };
    script.onerror = () => reject(new Error('The PDF reader couldn’t load. Paste the text or type it in.'));
    document.head.append(script);
  });
}

document.querySelector('#clear-upload').addEventListener('click', clearUpload);
function clearUpload() {
  fileInput.value = ''; pastedText.value = ''; pasteToggle.open = false;
  fileStatus.textContent = ''; extractStatus.classList.add('hidden'); redactionPreview.classList.add('hidden');
  canvasFrame.classList.add('hidden'); document.querySelector('#canvas-toolbar').classList.add('hidden');
  confirmRedaction.checked=false;pdfDocument=null;sourceBitmap=null;canvasBase=null;blackoutBoxes=[];pageBlackouts.clear();
}

// ---------------- exporting blacked-out pages ----------------

// Pages are re-rendered at a readable size and the black boxes are painted into the pixels, so the
// exported PNG holds nothing that was covered (and no photo metadata). The PDF itself is never sent.
async function exportPages() {
  if (pdfDocument) {
    pageBlackouts.set(pdfPageNumber, blackoutBoxes);
    const pages = [];
    for (let n = 1; n <= pdfDocument.numPages; n++) {
      const page = await pdfDocument.getPage(n);
      const scale = Math.min(2.5, 1700 / page.getViewport({ scale: 1 }).width);
      const viewport = page.getViewport({ scale });
      const out = document.createElement('canvas');
      out.width = Math.round(viewport.width); out.height = Math.round(viewport.height);
      const context = out.getContext('2d');
      await page.render({ canvasContext: context, viewport }).promise;
      paintBoxes(context, pageBlackouts.get(n) || [], scale / previewScale(page));
      pages.push(await toPng(out));
    }
    return pages;
  }
  if (sourceBitmap) {
    const scale = Math.min(1, 2000 / Math.max(sourceBitmap.width, sourceBitmap.height));
    const out = document.createElement('canvas');
    out.width = Math.round(sourceBitmap.width * scale); out.height = Math.round(sourceBitmap.height * scale);
    const context = out.getContext('2d');
    context.drawImage(sourceBitmap, 0, 0, out.width, out.height);
    paintBoxes(context, blackoutBoxes, out.width / canvas.width);
    return [await toPng(out)];
  }
  return [];
}
function paintBoxes(context, boxes, k) {
  context.fillStyle = '#000';
  for (const b of boxes) context.fillRect(b.x * k - 2, b.y * k - 2, b.w * k + 4, b.h * k + 4);
}
function toPng(c) { return new Promise((resolve, reject) => c.toBlob(b => b ? resolve(b) : reject(new Error('Could not export the page.')), 'image/png')); }

// ---------------- talking to the backend ----------------

class ApiError extends Error { constructor(status, message) { super(message); this.status = status; } }

async function api(path, { json, form } = {}) {
  let response;
  try {
    const options = json ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(json) }
      : form ? { method: 'POST', body: form } : {};
    response = await fetch(API_BASE + path, options);
  } catch {
    throw new ApiError(0, 'We couldn’t reach the HappyBill server. Make sure it’s running, then try again.');
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, errorMessage(response.status, body));
  return body;
}

function errorMessage(status, body) {
  if (status === 503) return 'Our bill reader isn’t available right now. You can type the charges in instead.';
  if (status === 502) return 'We couldn’t read this bill clearly. Try a clearer photo, or type the charges in.';
  if (status === 400 && typeof body.detail === 'string') return body.detail;
  if (status === 422) return 'Some details look incomplete. Check each charge has a service and an amount.';
  return 'Something went wrong on our side. Please try again.';
}

async function readUploadedBill() {
  const pages = await exportPages();
  if (pages.length) {
    const form = new FormData();
    pages.forEach((blob, i) => form.append('files', blob, `page-${i + 1}.png`));
    return api('/api/extract', { form });
  }
  return api('/api/extract-text', { json: { text: pastedText.value } });
}

// ---------------- the typed-in form <-> Bill ----------------

function emptyBill() {
  return {
    document: { document_type: 'other', is_itemized: true, statement_date: null },
    provider: { billing_provider_name: null },
    insurance: { coverage_status: 'unknown', payer_name: null, plan_name: null, plan_type: 'unknown', network_status: 'unknown' },
    encounter: { setting: 'unknown' },
    line_items: [],
    totals: {},
  };
}

function lineEntry(values = {}, index = null) {
  const entry = document.createElement('div'); entry.className = 'line-entry';
  if (index !== null) entry.dataset.index = index;
  entry.innerHTML = `<label>Service<input name="service" placeholder="e.g. Lab work" required></label><label>Code <span class="optional">optional</span><input name="code" placeholder="e.g. 80053" autocomplete="off"></label><label>Charge<input name="charge" type="number" min="0" step="0.01" placeholder="320" required></label><button type="button" class="remove-line" aria-label="Remove this charge">×</button>`;
  for (const [name, value] of Object.entries(values)) if (value !== null && value !== undefined) entry.querySelector(`[name="${name}"]`).value = value;
  return entry;
}

function setLineEntries(entries) {
  manualForm.querySelectorAll('.line-entry').forEach(e => e.remove());
  const bottom = manualForm.querySelector('.manual-bottom');
  for (const entry of entries) manualForm.querySelector('.manual-fields').insertBefore(entry, bottom);
}

function payerLabel(insurance) { return [insurance?.payer_name, insurance?.plan_name].filter(Boolean).join(' · '); }
function field(name) { return manualForm.querySelector(`[name="${name}"]`); }
function sumOf(lines, key) { return lines.some(l => l[key] != null) ? round2(lines.reduce((s, l) => s + (l[key] || 0), 0)) : null; }
function setField(name, value) { const input = field(name); input.value = input.dataset.original = value ?? ''; }

// After a bill is read, show it in the form so the user can check and correct it.
function fillForm(bill) {
  formSource = structuredClone(bill);
  const lines = bill.line_items || [], t = bill.totals || {};
  setField('hospital', bill.provider?.billing_provider_name || '');
  setField('payer', payerLabel(bill.insurance));
  setField('paid_total', t.insurance_payments ?? sumOf(lines, 'insurance_paid'));
  setField('owed_total', t.patient_balance_due ?? sumOf(lines, 'patient_responsibility'));
  setLineEntries(lines.length ? lines.map((l, i) => lineEntry({ service: l.description_as_printed, code: l.code_as_printed, charge: l.charge_amount }, i)) : [lineEntry()]);
}

const changed = name => !formSource || field(name).value.trim() !== (field(name).dataset.original ?? '');

// The form as a Bill. Starts from the bill it was filled from, so what the form doesn't show (plan,
// revenue codes, per-line payments...) is kept unless the user changed the related field.
function billFromForm() {
  const bill = formSource ? structuredClone(formSource) : emptyBill();
  const hospital = field('hospital').value.trim(), payer = field('payer').value.trim();
  if (changed('hospital')) bill.provider = { ...bill.provider, billing_provider_name: hospital || null };
  if (changed('payer')) {
    bill.insurance = { ...bill.insurance, payer_name: payer || null, plan_name: null,
                       coverage_status: payer ? 'insured' : (formSource ? bill.insurance.coverage_status : 'unknown') };
  }
  const totalsChanged = changed('paid_total') || changed('owed_total');
  bill.line_items = [...manualForm.querySelectorAll('.line-entry')].map(entry => {
    const get = name => entry.querySelector(`[name="${name}"]`).value.trim();
    const original = entry.dataset.index !== undefined ? formSource?.line_items?.[+entry.dataset.index] : null;
    const line = original ? { ...original } : { modifiers: [], units: 1, legibility: 'clear', candidate_codes: [] };
    line.description_as_printed = get('service') || 'Medical service';
    const code = get('code') || null;
    if (code !== (original?.code_as_printed ?? null)) { line.code_as_printed = code; line.code_type_as_printed = null; line.candidate_codes = []; }
    line.charge_amount = number(get('charge'));
    if (totalsChanged || !original) { line.insurance_paid = null; line.adjustment = null; line.patient_responsibility = null; }
    return line;
  });
  bill.totals = {
    ...(bill.totals || {}),
    total_charges: round2(bill.line_items.reduce((s, l) => s + l.charge_amount, 0)),
    insurance_payments: optionalNumber(field('paid_total').value),
    patient_balance_due: optionalNumber(field('owed_total').value),
  };
  return bill;
}

document.querySelector('#add-line').addEventListener('click', () => {
  const entry = lineEntry();
  manualForm.querySelector('.manual-fields').insertBefore(entry, manualForm.querySelector('.manual-bottom'));
  entry.querySelector('input').focus();
});
manualForm.addEventListener('click', event => {
  const button = event.target.closest('.remove-line');
  if (!button) return;
  const entries = manualForm.querySelectorAll('.line-entry');
  if (entries.length > 1) button.closest('.line-entry').remove();
  else entries[0].querySelectorAll('input').forEach(input => { input.value = ''; });
});
manualForm.addEventListener('submit', event => event.preventDefault());

// ---------------- continue: read -> check prices -> results ----------------

continueButton.addEventListener('click', async () => {
  if (busy) return;
  stepError.classList.add('hidden');
  let bill, finished = false;
  try {
    if (activeView === 'manual-view') {
      if (!manualForm.reportValidity()) return;
      bill = billFromForm();
      setBusy('Checking prices…');
      setProgress('match', { skipRead: true });
    } else {
      const hasPages = Boolean(pdfDocument || sourceBitmap);
      if (!hasPages && !pastedText.value.trim()) return showError('Choose your bill first, or type it in.');
      if (!confirmRedaction.checked) return showError('Cover your personal details, then tick the box.');
      setBusy('Reading your bill…');
      setProgress('read', { skipRead: false });
      bill = await readUploadedBill();
      fillForm(bill);
      setBusy('Checking prices…');
      setProgress('match');
    }
    // /api/plan matches prices (a second or two) and then writes the script: move the indicator along.
    progressTimer = setTimeout(() => setProgress('write'), 2500);
    const result = await api('/api/plan', { json: { bill } });
    finished = true;
    renderResults(result.display);
    results.classList.remove('hidden');
    document.querySelectorAll('.progress-step:nth-of-type(n+2)').forEach(step => step.classList.add('current'));
    results.scrollIntoView({ behavior:'smooth', block:'start' });
  } catch (error) {
    showError(error instanceof ApiError ? error.message : `Something went wrong: ${error.message}`);
  } finally {
    setBusy(null);
    clearTimeout(progressTimer);
    if (finished) { setProgress('done'); setTimeout(() => progressRow.classList.add('hidden'), 1500); }
    else progressRow.classList.add('hidden');
  }
});

const continueLabel = continueButton.innerHTML;
function setBusy(message) {
  busy = Boolean(message);
  continueButton.disabled = busy;
  continueButton.style.opacity = busy ? '.75' : '';
  continueButton.innerHTML = busy ? escapeHTML(message) : continueLabel;
}

// Steps shown while the bill is read and checked: done / active / not yet.
const STEPS = ['read', 'match', 'write'];
let progressTimer = null;
function setProgress(step, { skipRead = progressRow.dataset.skipRead === 'true' } = {}) {
  progressRow.dataset.skipRead = String(skipRead);
  const current = step === 'done' ? STEPS.length : STEPS.indexOf(step);
  progressRow.querySelectorAll('.progress-item').forEach(item => {
    const i = STEPS.indexOf(item.dataset.step);
    item.classList.toggle('hidden', skipRead && item.dataset.step === 'read');
    item.classList.toggle('done', i < current);
    item.classList.toggle('active', i === current);
  });
  progressRow.classList.remove('hidden');
}

// ---------------- results (rendered from the backend's `display`) ----------------

function renderResults(d) {
  const b = d.bottom_line;
  setText('#result-context', [d.provider, d.insurance].filter(Boolean).join(' · '));
  const title = document.querySelector('#result-title');
  title.textContent = b.title;
  if (b.savings) title.innerHTML = `You could save <em>${money(b.savings)}</em>`;

  // Total billed -> fair price -> (minus insurance paid) -> you should pay
  setText('#flow-billed', money(b.billed));
  const hasFair = b.fair != null, hasPay = b.should_pay != null;
  toggle('#arrow-fair', hasFair); toggle('#flow-fair-item', hasFair);
  toggle('#arrow-pay', hasPay); toggle('#flow-pay-item', hasPay);
  document.querySelector('#result-flow').style.gridTemplateColumns = hasPay ? '' : hasFair ? '1fr auto 1fr' : '1fr';
  if (hasFair) { setText('#flow-fair-label', b.fair_label); setText('#flow-fair', money(b.fair)); }
  setText('#flow-paid', b.insurance_paid ? `− ${money(b.insurance_paid)} paid by insurance` : '');
  if (hasPay) {
    setText('#flow-should', money(b.should_pay));
    setText('#flow-asked', b.asked != null && b.savings ? `Your bill says ${money(b.asked)}` : '');
  }
  setText('#result-note', b.note || ''); toggle('#result-note', Boolean(b.note));

  document.querySelector('#next-steps').innerHTML = d.steps.map(s => `<li>${escapeHTML(s)}</li>`).join('');
  document.querySelector('#script-text').value = d.call_script.join('\n\n');

  // Charges: billed vs fair price. Only flags worth a second look (duplicates, visit level) are shown.
  const rows = d.lines.map(c => {
    const flags = c.badges.filter(x => x.tone === 'warning').map(x => `<span class="flag">${escapeHTML(x.text)}</span>`).join('');
    const note = flags && c.note ? `<span class="sub">${escapeHTML(c.note)}</span>` : '';
    const fair = c.fair_price != null ? `<span class="fair">${money(c.fair_price)}</span>` : '<span class="none" title="No published price found">—</span>';
    return `<tr><td>${escapeHTML(c.name)}${c.code ? `<span class="code">${escapeHTML(c.code)}</span>` : ''}${flags}${note}</td><td>${money(c.billed)}</td><td>${fair}</td></tr>`;
  });
  if (d.included_in_stay) {
    const items = d.included_in_stay.items;
    rows.push(`<tr><td>${escapeHTML(d.included_in_stay.title)}<span class="sub">${items.map(i => escapeHTML(i.name)).join(', ')}</span></td><td>${money(items.reduce((s, i) => s + i.billed, 0))}</td><td><span class="none">included</span></td></tr>`);
  }
  document.querySelector('#charge-rows').innerHTML = rows.join('');
  const labels = [...new Set(d.lines.map(c => c.fair_label).filter(Boolean))];
  setText('#fair-column', labels.length === 1 ? labels[0] : 'Fair price');

  document.querySelector('#how-body').innerHTML =
    (d.assumptions.length ? `<ul>${d.assumptions.map(a => `<li>${escapeHTML(a)}</li>`).join('')}</ul>` : '') +
    `<p>Prices come from ${d.sources.map(escapeHTML).join('; ')}.</p><p>${escapeHTML(d.disclaimer)}</p>`;
}

function setText(selector, text) { const el = document.querySelector(selector); if (el) el.textContent = text; }
function toggle(selector, show) { document.querySelector(selector).classList.toggle('hidden', !show); }

// ---------------- copy, edit, start over ----------------

document.querySelector('#copy-script').addEventListener('click', event => copyText(document.querySelector('#script-text').value, event.currentTarget));
async function copyText(text, button) {
  button.dataset.label ??= button.textContent;
  try { await navigator.clipboard.writeText(text); }
  catch { const temp=document.createElement('textarea');temp.value=text;document.body.append(temp);temp.select();document.execCommand('copy');temp.remove(); }
  button.textContent = 'Copied ✓';
  setTimeout(() => { button.textContent = button.dataset.label; }, 1600);
}

document.querySelector('#edit-bill').addEventListener('click', () => {
  showView('manual-view');
  document.querySelector('#bill-step').scrollIntoView({ behavior:'smooth' });
});
document.querySelector('#start-over').addEventListener('click', () => {
  results.classList.add('hidden');
  clearUpload();
  manualForm.reset();
  manualForm.querySelectorAll('[data-original]').forEach(input => delete input.dataset.original);
  formSource = null;
  setLineEntries([lineEntry()]);
  document.querySelectorAll('.progress-step:nth-of-type(n+2)').forEach(step => step.classList.remove('current'));
  showView('upload-view');
  document.querySelector('#bill-step').scrollIntoView({behavior:'smooth'});
});

function showError(message) { stepError.textContent=message; stepError.classList.remove('hidden'); }
function number(value) { const parsed=Number(String(value||'').replaceAll(',','').replace(/[^\d.]/g,'')); return Number.isFinite(parsed)?parsed:0; }
function optionalNumber(value) { return String(value ?? '').trim() === '' ? null : number(value); }
function round2(value) { return Math.round(value * 100) / 100; }
function money(value) { return new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:2}).format(value); }
function escapeHTML(value) { return String(value).replace(/[&<>"']/g, char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char])); }

// Footer: which price sources are loaded and when they last synced from the Databricks lakehouse.
fetch(API_BASE + '/api/sources').then(r => r.ok ? r.json() : null).then(data => {
  if (!data || !data.sources.length) return;
  const el = document.getElementById('data-sources');
  const sync = data.last_sync ? ` · synced from Databricks ${new Date(data.last_sync.synced_at).toLocaleDateString()}` : ' · Databricks lakehouse';
  el.textContent = `${data.sources.length} price sources${sync}`;
  el.hidden = false;
}).catch(() => {});
