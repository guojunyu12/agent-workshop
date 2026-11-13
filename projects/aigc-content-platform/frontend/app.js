const state = { module: 'emoji', templateId: 'emoji-default', templates: [], referenceImage: '' };
const $ = (selector) => document.querySelector(selector);
let modelCatalog = [];

function toast(message) {
  const node = $('#toast'); node.textContent = message; node.classList.add('is-visible');
  window.setTimeout(() => node.classList.remove('is-visible'), 2600);
}

function formatBytes(bytes) { return bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`; }
async function loadQuota() {
  const response = await fetch('/api/quota?user_id=demo');
  if (!response.ok) return;
  const quota = await response.json();
  $('#quota-value').textContent = `${quota.used} / ${quota.limit}`;
  $('#quota-progress').style.width = `${Math.min(100, quota.limit ? (quota.used / quota.limit) * 100 : 0)}%`;
}
function clearReferenceImage() { state.referenceImage = ''; $('#reference-image').value = ''; $('#reference-empty').classList.remove('is-hidden'); $('#reference-preview').classList.add('is-hidden'); }
function setReferenceImage(file) {
  if (!file) return;
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) return toast('参考图片仅支持 PNG、JPG 或 WebP');
  if (file.size > 10 * 1024 * 1024) return toast('参考图片不能超过 10 MB');
  const reader = new FileReader();
  reader.onload = () => { state.referenceImage = reader.result; $('#reference-preview-image').src = reader.result; $('#reference-file-name').textContent = file.name; $('#reference-file-size').textContent = formatBytes(file.size); $('#reference-empty').classList.add('is-hidden'); $('#reference-preview').classList.remove('is-hidden'); };
  reader.readAsDataURL(file);
}

function visualClass(module) { return module === 'emoji' ? 'visual-emoji' : module === 'mobile_wallpaper' ? 'visual-mobile' : 'visual-desktop'; }
function moduleName(module) { return module === 'emoji' ? '表情包' : module === 'mobile_wallpaper' ? '手机壁纸' : '电脑壁纸'; }
function modelOptionLabel(model) { return `${model.provider_name} · ${model.name}（${model.runtime_label}）`; }
function updateModelBadge() {
  const model = modelCatalog.find((item) => item.id === $('#model_id').value);
  if (model) $('#model-runtime-badge').textContent = `${model.runtime_label} · ${model.provider_name}`;
}
async function loadModels() {
  try { modelCatalog = await fetch(`/api/models?module=${encodeURIComponent(state.module)}`).then((response) => response.json()); } catch (error) {
    modelCatalog = [];
    $('#model_id').innerHTML = '<option value="">模型服务暂时不可用</option>';
    $('#model_id').disabled = true;
    $('#model-runtime-badge').textContent = '模型服务不可用';
    return;
  }
  const imageModels = modelCatalog.filter((model) => model.enabled && model.role === 'image');
  const enabledUnderstandingModel = modelCatalog.some((model) => model.enabled && model.role === 'understanding');
  const select = $('#model_id');
  if (!imageModels.length) {
    select.innerHTML = `<option value="">${enabledUnderstandingModel ? '已启用的是理解/审核模型，请启用一个生图模型' : '暂无可用生图模型，请联系管理员'}</option>`;
    select.disabled = true;
    $('#model-runtime-badge').textContent = enabledUnderstandingModel ? '当前配置不可直接生图' : '暂无可用模型';
    return;
  }
  select.disabled = false;
  select.innerHTML = imageModels.map((model) => `<option value="${model.id}">${modelOptionLabel(model)}</option>`).join('');
  updateModelBadge();
}

async function loadTemplates(module = state.module) {
  const response = await fetch(`/api/templates?module=${module}`);
  state.templates = await response.json();
  const grid = $('#template-grid');
  grid.innerHTML = state.templates.map((item, index) => `<button class="template-card ${item.id === state.templateId ? 'is-selected' : ''}" data-template="${item.id}" data-title="${item.title}" data-prompt="${item.prompt_template || ''}"><span class="template-visual ${visualClass(module)}">${index === 0 ? '✦' : '图'}</span><span class="template-meta"><small>${moduleName(module)}</small><b>${item.title}</b></span></button>`).join('');
  grid.querySelectorAll('.template-card').forEach((card) => card.addEventListener('click', () => selectTemplate(card.dataset.template, card.dataset.title, card.dataset.prompt)));
  const selected = state.templates.find((item) => item.id === state.templateId) || state.templates[0];
  if (selected) selectTemplate(selected.id, selected.title, selected.prompt_template, false);
}

function selectTemplate(id, title, promptTemplate = '', notify = true) {
  state.templateId = id; $('#selected-template-title').textContent = title;
  if (promptTemplate) $('#prompt').value = promptTemplate;
  document.querySelectorAll('.template-card').forEach((card) => card.classList.toggle('is-selected', card.dataset.template === id));
  if (notify) toast(`已选择「${title}」`);
}

function switchView(view) {
  document.querySelectorAll('.view').forEach((node) => node.classList.toggle('is-hidden', node.id !== `${view}-view`));
  document.querySelectorAll('.nav-item').forEach((node) => node.classList.toggle('is-active', node.dataset.view === view));
  $('#view-title').textContent = view === 'studio' ? '开始创作' : view === 'templates' ? '模板库' : '我的生成';
  if (view === 'templates') renderLibrary();
  if (view === 'history') loadHistory();
}

async function loadHistory() {
  const items = await fetch('/api/generations?user_id=demo').then((response) => response.json());
  const grid = $('#history-grid');
  if (!items.length) { grid.innerHTML = '<div class="history-empty"><span>↺</span><h2>每一次生成都会在这里</h2><p>先去创作台生成一张图片。</p></div>'; return; }
  grid.innerHTML = items.map((item) => `<article class="history-card"><a href="${item.output_url}" target="_blank"><img src="${item.output_url}" alt="${item.prompt || '生成结果'}" /></a><div class="history-card-body"><div><b>${moduleName(item.module)}</b><small>${item.width} × ${item.height} · ${item.runtime_mode === 'real' ? '真实调用' : '开发测试 Mock'}</small></div><div><a class="history-open" href="${item.output_url}" download>下载</a><button class="history-delete" data-delete-generation="${item.id}">删除</button></div></div><p>${item.prompt || '未填写提示词'}</p></article>`).join('');
  document.querySelectorAll('[data-delete-generation]').forEach((button) => button.addEventListener('click', () => deleteGeneration(button.dataset.deleteGeneration)));
}

async function deleteGeneration(id) {
  if (!window.confirm('确认删除这张生成图片？删除后无法恢复。')) return;
  const response = await fetch(`/api/generations/${id}?user_id=demo`, { method: 'DELETE' });
  const result = await response.json();
  if (!response.ok) return toast(result.error || '删除失败');
  toast('生成图片已删除');
  loadQuota();
  loadHistory();
}

async function renderLibrary() {
  const modules = ['emoji', 'mobile_wallpaper', 'desktop_wallpaper'];
  const groups = await Promise.all(modules.map((module) => fetch(`/api/templates?module=${module}`).then((r) => r.json())));
  $('#library-grid').innerHTML = groups.flat().map((item, index) => `<button class="template-card" data-module="${item.module}" data-template="${item.id}"><span class="template-visual ${visualClass(item.module)}">${index % 2 ? '图' : '✦'}</span><span class="template-meta"><small>${moduleName(item.module)}</small><b>${item.title}</b></span></button>`).join('');
  $('#library-grid').querySelectorAll('.template-card').forEach((card) => card.addEventListener('click', () => {
    state.module = card.dataset.module;
    document.querySelectorAll('.module-tab').forEach((tab) => tab.classList.toggle('is-active', tab.dataset.module === state.module));
    switchView('studio');
    loadTemplates(state.module).then(() => selectTemplate(card.dataset.template, card.querySelector('b').textContent));
  }));
}

async function generate() {
  if (!$('#model_id').value) { toast('暂无可用生图模型，请联系管理员'); return; }
  const payload = { module: state.module, template_id: state.templateId, prompt: $('#prompt').value, model_id: $('#model_id').value, user_id: 'demo' };
  if ($('#negative-prompt').value.trim()) payload.negative_prompt = $('#negative-prompt').value.trim();
  if (state.referenceImage) payload.reference_image = state.referenceImage;
  if ($('#size').value === 'custom') { payload.width = Number($('#width').value); payload.height = Number($('#height').value); payload.preset = false; }
  const button = $('#generate-button'); button.disabled = true; button.querySelector('span').textContent = '正在生成…';
  try {
    const response = await fetch('/api/generate', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '生成失败');
    $('#result-panel').classList.remove('is-hidden'); $('#result-title').textContent = '你的图片已经准备好了'; $('#result-meta').textContent = `${result.runtime.label} · ${result.runtime.provider_name} · ${result.runtime.model_name} · ${result.size.width} × ${result.size.height}`; $('#result-link').href = result.output_url;
    loadQuota();
    toast(result.runtime.actual_call ? `已调用${result.runtime.provider_name}真实接口` : `${result.runtime.label}：未调用真实生图接口`);
  } catch (error) { toast(error.message); } finally { button.disabled = false; button.querySelector('span').textContent = '生成图片'; }
}

document.querySelectorAll('.nav-item').forEach((item) => item.addEventListener('click', () => switchView(item.dataset.view)));
document.querySelectorAll('.module-tab').forEach((tab) => tab.addEventListener('click', () => { state.module = tab.dataset.module; document.querySelectorAll('.module-tab').forEach((node) => node.classList.toggle('is-active', node === tab)); Promise.all([loadModels(), loadTemplates()]); }));
$('#see-all').addEventListener('click', () => switchView('templates'));
$('#generate-button').addEventListener('click', generate);
$('#model_id').addEventListener('change', updateModelBadge);
$('#size').addEventListener('change', () => $('#custom-size').classList.toggle('is-visible', $('#size').value === 'custom'));
$('#clear-template').addEventListener('click', () => toast('模板是生成必要条件，请从模板库重新选择'));
$('#reference-image').addEventListener('change', (event) => setReferenceImage(event.target.files[0]));
$('#clear-reference').addEventListener('click', clearReferenceImage);
$('#reference-upload').addEventListener('dragover', (event) => { event.preventDefault(); $('#reference-upload').classList.add('is-dragging'); });
$('#reference-upload').addEventListener('dragleave', () => $('#reference-upload').classList.remove('is-dragging'));
$('#reference-upload').addEventListener('drop', (event) => { event.preventDefault(); $('#reference-upload').classList.remove('is-dragging'); setReferenceImage(event.dataTransfer.files[0]); });
loadQuota();
loadModels().then(loadTemplates);
