/* 寻迹：路线、稿件及播放版本以服务端为准。 */
(function () {
  'use strict';
  const TOPICS = [
    ['第一次来，想看精华', '从几件代表性的器物开始认识这里。', '我是第一次来，想看代表性的精华展品。'],
    ['沿着一件工艺看', '看看材料如何变成一件器物。', '我想重点了解器物的材料与制作工艺。'],
    ['听故事，少一点术语', '让器物背后的生活慢慢展开。', '我想了解古人的生活，讲得通俗生动、有故事感。'],
    ['带孩子一起探索', '一起观察，留一点提问的空间。', '我带孩子来，希望看有趣的器物，讲解轻松易懂。'],
    ['找找器物之间的联系', '顺着生活与工艺的线索往下看。', '我想看看不同器物在生活用途和制作工艺上的联系。'],
    ['我还没有想法', '从日常生活里的一点好奇开始。', '请推荐一条认识古人日常生活与器物的入门路线。'],
  ];
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>'"]/g, (c) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;' }[c]));
  const storage = {
    get() { try { return sessionStorage.getItem('lumora_session_id'); } catch { return null; } },
    set(id) { try { sessionStorage.setItem('lumora_session_id', id); } catch {} },
    clear() { try { sessionStorage.removeItem('lumora_session_id'); } catch {} },
  };
  const state = { sessionId: storage.get(), latest: null, reply: null, museum: null, selectedTopic: null,
    view: 'welcome', sending: false, playing: false, playbackToken: 0, speech: null, progress: Promise.resolve(), feedback: {} };
  function show(name) {
    document.querySelectorAll('[data-view]').forEach((el) => el.classList.toggle('hidden', el.dataset.view !== name));
    if (state.view !== name) window.scrollTo({ top: 0, behavior: 'smooth' });
    state.view = name;
  }
  function toast(text) {
    $('toast').textContent = text; $('toast').classList.add('show');
    clearTimeout(toast.timer); toast.timer = setTimeout(() => { $('toast').classList.remove('show'); $('toast').textContent = ''; }, 4500);
  }
  function setStatus(text = '') { $('welcome-status').textContent = text; }
  function busy(flag) {
    state.sending = flag;
    document.querySelectorAll('button,input,textarea,select').forEach((el) => { el.disabled = flag; });
    $('app').setAttribute('aria-busy', String(flag));
    if (flag && state.view === 'tour') $('tour-message').textContent = '正在整理内容，请稍候……';
    if (flag && state.view === 'summary') $('summary-introduction').textContent = '正在整理你的探索手记，请稍候……';
    if (!flag && state.latest) render();
  }
  async function request(path, body) {
    const response = await fetch(path, body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    let data;
    try { data = await response.json(); } catch { throw Error('服务暂时没有响应，请稍后重试。'); }
    if (!response.ok) {
      const error = Error(data?.error?.message || '请求没有完成，请稍后重试。');
      error.status = response.status; throw error;
    }
    return data;
  }
  const current = () => state.reply?.playback?.current;
  const progressPayload = () => current() ? { guide_id: current().guide_id, completed_segments: current().completed_segments } : undefined;
  const relic = (id) => (state.museum?.relics || []).find((item) => item.relic_id === id);
  const relicName = (id) => relic(id)?.name || '展品';
  const nodeName = (id) => (state.museum?.nodes || []).find((item) => item.node_id === id)?.name || '尚未选择';
  function stopSpeech() {
    state.playbackToken += 1; state.playing = false;
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();
    state.speech = null; renderGuide();
  }
  function apply(data) {
    const oldId = current()?.guide_id;
    if (data.latest_state) state.latest = data.latest_state;
    if (data.reply_content) state.reply = data.reply_content;
    if (state.reply?.status === 'error') console.warn('导览操作未完成', state.reply.error);
    if (oldId && oldId !== current()?.guide_id) stopSpeech();
    render();
  }
  function replyProblem(data) {
    if (data?.reply_content?.status === 'error') {
      const code = data.reply_content.error?.code;
      const message = data.reply_content.message || '';
      if (/超时|连接模型|网络|代理/.test(message)) return '模型连接暂时没有完成，请检查网络后重试。';
      if (/未通过核对/.test(message)) return '这次生成的内容还不够准确，请重新尝试。';
      return (code === 'invalid_request' ? message : '这一步暂时没有完成，可以再试一次。') + (state.latest?.status === 'finished' ? ' 你的游览记录已保留。' : '');
    }
    return null;
  }
  async function event(action, payload = {}) {
    if (!state.sessionId || state.sending) return null;
    stopSpeech(); busy(true);
    try {
      await state.progress;
      if (action === 'resume_guide') payload = progressPayload();
      const data = await request('/api/event', { session_id: state.sessionId, action, payload });
      apply(data); const problem = replyProblem(data); if (problem) toast(problem);
      return data;
    } catch (error) { toast(error.message); return null; }
    finally { busy(false); }
  }
  async function chat(text, fromWelcome = false) {
    if (!state.sessionId || state.sending) return null;
    stopSpeech(); busy(true); if (!fromWelcome) addChat(text, 'user');
    try {
      await state.progress;
      const playback = progressPayload();
      const data = await request('/api/chat', { session_id: state.sessionId, text, ...(playback ? { playback } : {}) });
      apply(data); const problem = replyProblem(data);
      if (problem) { toast(problem); setStatus(problem); }
      else if (state.view === 'welcome') {
        const missing = data.reply_content?.missing_fields || [];
        setStatus(missing.includes('remaining_minutes') ? '大约能逛多久？比如“半小时”或“一个小时左右”。' : '还没有想好也没关系，选一个下面的主题就可以。');
      } else {
        setStatus(); addChat(data.reply_content?.status === 'answer_ready' ? '回答已放在讲解卡片里，可以阅读或播放。' : (data.reply_content?.message || '已经收到你的想法。'), 'assistant');
      }
      return data;
    } catch (error) { toast(error.message); setStatus(error.message); return null; }
    finally { busy(false); }
  }
  async function createSession() {
    if (state.sessionId) {
      try { apply(await request(`/api/state?session_id=${encodeURIComponent(state.sessionId)}`)); return; }
      catch (error) { if (error.status !== 404) throw error; state.sessionId = null; storage.clear(); }
    }
    const data = await request('/api/session', { pace: 'standard' });
    state.sessionId = data.latest_state.session_id; storage.set(state.sessionId); apply(data);
  }
  function render() {
    if (!state.latest) return;
    if (state.latest.status === 'finished') { renderSummary(state.reply?.summary); show('summary'); return; }
    const info = state.latest.visitor_info || {};
    const started = state.latest.visited_relics?.length || state.latest.pending_queue?.length || (info.content_interests?.length && info.remaining_minutes != null);
    if (started) { renderTour(); show('tour'); } else show('welcome');
  }
  function renderTour() {
    const minutes = state.latest.visitor_info?.remaining_minutes;
    $('remaining-time').textContent = minutes == null ? '未设置' : `${Math.max(0, Math.ceil(minutes))} 分钟`;
    $('current-location').textContent = nodeName(state.reply?.current_node_id);
    $('tour-status').textContent = minutes === 0 ? '时间已到' : state.reply?.route_dirty ? '路线待更新' : '探索中';
    $('tour-message').textContent = state.sending ? '正在整理内容，请稍候……' : state.reply?.status === 'error' ? '这一步没有完成，可以重新尝试。' : state.reply?.route_dirty ? '你的偏好或位置有变化，点击“重新规划”更新路线。' : minutes === 0 ? '可以结束游览，整理这次的探索手记。' : '';
    renderRoute(); renderGuide();
  }
  function renderRoute() {
    const visited = state.latest.visited_relics || [], pending = state.latest.pending_queue || [];
    const rows = [...visited.map((item) => ({ ...item, visited: true })), ...pending];
    $('route-empty').classList.toggle('hidden', rows.length > 0);
    $('route-empty').textContent = state.reply?.status === 'time_exhausted' ? '时间已经用完，可以把这次的发现整理成手记。' : '暂时没有合适的路线，可以多留一些时间，或换个主题试试。';
    $('route-track').innerHTML = rows.map((item, i) => {
      const next = !item.visited && i === visited.length;
      return `<div class="route-stop ${item.visited ? 'visited' : ''} ${next ? 'current' : ''}"><div class="stop-dot">${item.visited ? '✓' : i + 1}</div><div><div class="stop-name">${esc(relicName(item.relic_id))}</div><div class="stop-reason">${esc((item.recommendation_reason || (item.visited ? '讲解已备好' : '等待你的探索')).replaceAll('镇馆之宝', '演示重点展品'))}</div></div>${next ? `<div class="stop-buttons"><button class="stop-action" data-start="${esc(item.relic_id)}" ${state.reply?.route_dirty ? 'disabled' : ''}>开始讲解</button><button class="text-button skip-action" data-skip="${esc(item.relic_id)}">跳过</button></div>` : ''}</div>`;
    }).join('');
    $('route-track').querySelectorAll('[data-start]').forEach((b) => b.addEventListener('click', () => event('start_guide', { relic_id: b.dataset.start })));
    $('route-track').querySelectorAll('[data-skip]').forEach((b) => b.addEventListener('click', () => event('skip', { relic_id: b.dataset.skip })));
  }
  function renderGuide() {
    const context = current(); $('guide-card').classList.toggle('hidden', !context); if (!context) return;
    const record = state.latest?.guide_records?.find((item) => item.guide_id === context.guide_id);
    const segments = context.segments || record?.segments || [], done = context.completed_segments || 0;
    $('guide-title').textContent = `${record?.kind === 'answer' ? '追问 · ' : ''}${relicName(context.relic_id)}`;
    $('guide-counter').textContent = `${done}/${segments.length} 段已播放`; $('guide-text').textContent = segments.join('\n\n');
    $('progress-fill').style.width = `${segments.length ? done / segments.length * 100 : 0}%`;
    const canSpeak = 'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window;
    $('play-button').disabled = state.sending || !canSpeak || done >= segments.length;
    $('play-button').textContent = state.playing ? 'Ⅱ' : '▶'; $('play-button').setAttribute('aria-label', state.playing ? '暂停讲解' : '播放讲解');
    $('progress-copy').textContent = !canSpeak ? '当前浏览器不支持朗读，可以直接阅读讲解。' : done >= segments.length ? '讲解已播放完' : state.playing ? '正在朗读，随时可以暂停' : '点击朗读；暂停后从未播完的一段继续';
    $('resume-button').classList.toggle('hidden', !state.reply?.playback?.resume_stack?.length);
    $('resume-button').disabled = state.sending || done < segments.length;
    $('resume-button').textContent = done < segments.length ? '播完回答后可恢复原讲解' : '恢复原讲解';
  }
  async function speakNext(token) {
    const context = current(); if (!context || token !== state.playbackToken || !state.playing) return;
    if (context.completed_segments >= context.segments.length) { state.playing = false; renderGuide(); return; }
    const guideId = context.guide_id, completed = context.completed_segments + 1;
    const utterance = new SpeechSynthesisUtterance(context.segments[completed - 1]); utterance.lang = 'zh-CN'; state.speech = utterance;
    utterance.onerror = (error) => { if (token !== state.playbackToken) return; stopSpeech(); if (!['canceled','interrupted'].includes(error.error)) toast('浏览器暂时无法朗读，可以先阅读文字。'); };
    utterance.onend = () => {
      if (token !== state.playbackToken || current()?.guide_id !== guideId) return;
      // 只有实际朗读完成事件可以推进完整段落数。
      state.progress = state.progress.then(async () => {
        const data = await request('/api/event', { session_id: state.sessionId, action: 'playback_progress', payload: { guide_id: guideId, completed_segments: completed } });
        apply(data); if (replyProblem(data)) throw Error('播放进度没有保存，请重试。');
      }).catch((error) => { stopSpeech(); toast(error.message); });
      state.progress.then(() => speakNext(token));
    };
    window.speechSynthesis.speak(utterance); renderGuide();
  }
  function renderSummary(summary) {
    $('summary-title').textContent = summary?.title || '这次探索，先存下来';
    $('summary-introduction').textContent = summary?.introduction || '游览已结束，手记还没有生成成功。可以点击下方按钮重试。';
    $('retry-summary').classList.toggle('hidden', Boolean(summary)); if (!summary) { $('summary-content').innerHTML = ''; return; }
    const insights = (items) => items.map((item) => `<li><strong>${esc(item.title)}</strong><p>${esc(item.text)}</p></li>`).join('');
    $('summary-content').innerHTML = `<section class="summary-block"><h3>这次探索过的器物</h3>${(summary.trail || []).map((item, i) => `<div class="trail-item"><span class="trail-index">${String(i + 1).padStart(2, '0')}</span><div><strong>${esc(item.name)}</strong><small>${esc(item.recap)}</small></div></div>`).join('') || '<p>这次还没有留下讲解记录，下次从一件好奇的器物开始吧。</p>'}</section>${summary.discoveries?.length ? `<section class="summary-block"><h3>沿途发现</h3><ul>${insights(summary.discoveries)}</ul></section>` : ''}${summary.connections?.length ? `<section class="summary-block"><h3>器物之间</h3><ul>${insights(summary.connections)}</ul></section>` : ''}${summary.questions?.length ? `<section class="summary-block"><h3>你的问题</h3>${summary.questions.map((item) => `<p><strong>${esc(item.question)}</strong><br>${esc(item.answer_summary)}</p>`).join('')}</section>` : ''}${summary.next_exploration ? `<section class="summary-block"><h3>下次可以继续</h3><p>${esc(summary.next_exploration)}</p></section>` : ''}`;
  }
  function addChat(text, role) { const el = document.createElement('div'); el.className = `chat-bubble ${role}`; el.textContent = text; $('chat-log').appendChild(el); $('chat-log').scrollTop = $('chat-log').scrollHeight; }
  let previousFocus;
  function modal(title, html) { previousFocus = document.activeElement; $('modal-title').textContent = title; $('modal-body').innerHTML = html; $('modal').classList.remove('hidden'); $('modal').querySelector('.modal-close').focus(); }
  function closeModal() { $('modal').classList.add('hidden'); previousFocus?.focus(); }
  function topics() {
    $('topic-grid').innerHTML = TOPICS.map((item, i) => `<button class="topic-chip ${state.selectedTopic === i ? 'selected' : ''}" type="button" data-topic="${i}" aria-pressed="${state.selectedTopic === i}"><strong>${esc(item[0])}</strong><small>${esc(item[1])}</small></button>`).join('');
    $('topic-grid').querySelectorAll('[data-topic]').forEach((b) => b.addEventListener('click', () => { state.selectedTopic = Number(b.dataset.topic); $('wish-input').value = ''; topics(); setStatus(`已选“${TOPICS[state.selectedTopic][0]}”。告诉我大约能逛多久，就可以出发。`); $('time-input').focus(); }));
  }
  function renderRecommendations() {
    const routes = state.museum?.recommended_routes || [];
    $('recommendation-list').innerHTML = routes.map((r, i) => `<button class="recommendation-card" type="button" data-route="${i}"><span class="eyebrow">路线 ${String(i + 1).padStart(2, '0')}</span><strong>${esc(r.title)}</strong><span>${esc(r.description)}</span><small>${(r.relic_ids || []).length} 件展品 · 约 ${esc(r.estimated_minutes)} 分钟 <b>查看路线 →</b></small></button>`).join('');
    $('recommendations').classList.toggle('hidden', !routes.length);
    $('recommendation-list').querySelectorAll('[data-route]').forEach((b) => b.addEventListener('click', () => {
      const route = routes[Number(b.dataset.route)];
      modal(route.title, `<p class="form-hint">${esc(route.description)}${state.museum?.mode === 'demo' ? '（演示路线）' : ''}</p><ol class="preview-stops">${route.relic_ids.map((id) => `<li>${esc(relicName(id))}</li>`).join('')}</ol><p class="form-hint">这是一条推荐参考，出发时会按你的时间重新安排。</p><label class="field-label" for="preview-time">预计能逛多久？</label><input id="preview-time" placeholder="比如：半小时、45分钟或一个小时左右" value="${esc($('time-input').value)}"><p id="preview-status" class="status-line"></p><button class="primary-button full-button" id="start-recommendation" type="button">按我的时间安排这条路线</button>`);
      $('start-recommendation').addEventListener('click', async () => {
        const time = $('preview-time').value.trim(); if (!time) { $('preview-status').textContent = '告诉我大约有多少时间就好。'; $('preview-time').focus(); return; }
        $('time-input').value = time; closeModal(); setStatus('正在按你的时间安排路线……');
        await chat(`我对“${route.title}”这个主题感兴趣，${route.description}。${timeSentence(time)}`, true);
      });
    }));
  }
  function timeSentence(value) { return `我预计会游览${/^\d+(\.\d+)?$/.test(value) ? value + '分钟' : value}。`; }
  $('welcome-form').addEventListener('submit', async (e) => {
    e.preventDefault(); if (!state.sessionId) { setStatus('导览服务还没有连接，请稍后重试或刷新页面。'); return; }
    const wish = $('wish-input').value.trim() || (state.selectedTopic == null ? '' : TOPICS[state.selectedTopic][2]), time = $('time-input').value.trim();
    if (!time) { setStatus('先告诉我大约有多少时间，比如“一个小时左右”。'); $('time-input').focus(); return; }
    if (!wish) { setStatus('随便说一句，或选一个下面的主题，也可以直接看看推荐路线。'); return; }
    setStatus('正在听懂你的想法，安排路线……'); await chat(`${wish}\n${timeSentence(time)}`, true);
  });
  $('wish-input').addEventListener('input', () => { if (state.selectedTopic != null) { state.selectedTopic = null; topics(); setStatus(); } });
  $('chat-form').addEventListener('submit', async (e) => { e.preventDefault(); const text = $('chat-input').value.trim(); if (text && await chat(text)) $('chat-input').value = ''; });
  $('ask-button').addEventListener('click', () => { stopSpeech(); $('chat-input').focus(); });
  $('replan-button').addEventListener('click', () => event('replan'));
  $('resume-button').addEventListener('click', () => event('resume_guide', progressPayload()));
  $('retry-summary').addEventListener('click', () => event('generate_summary', { feedback: state.feedback }));
  $('play-button').addEventListener('click', async () => { if (state.playing) { stopSpeech(); return; } await state.progress; state.playing = true; speakNext(++state.playbackToken); });
  $('finish-button').addEventListener('click', () => {
    stopSpeech(); modal('给这次探索留一句话', `<p class="form-hint">两个问题都可以跳过。</p><label class="field-label" for="favorite-input">哪件展品最让你有印象？</label><select id="favorite-input"><option value="">暂时没有，先跳过</option>${(state.latest.visited_relics || []).map((item) => `<option value="${esc(item.relic_id)}">${esc(relicName(item.relic_id))}</option>`).join('')}</select><label class="field-label" for="reflection-input">这次游览有什么感想？</label><textarea id="reflection-input" rows="3" placeholder="写下一句话就好"></textarea><button class="primary-button full-button" id="confirm-finish" type="button">结束并生成手记</button>`);
    $('confirm-finish').addEventListener('click', async () => { const favorite = $('favorite-input').value, reflection = $('reflection-input').value; state.feedback = { ...(favorite ? { favorite_relic_id: favorite } : {}), ...(reflection.trim() ? { reflection } : {}) }; closeModal(); await event('finish', { feedback: state.feedback }); });
  });
  $('edit-location').addEventListener('click', () => {
    const nodes = state.museum?.nodes || []; if (!nodes.length) { toast('暂时没有可选的场馆位置。'); return; }
    modal('你现在在哪里？', `<label class="field-label" for="location-input">选择当前位置</label><select id="location-input">${nodes.map((n) => `<option value="${esc(n.node_id)}" ${n.node_id === state.reply?.current_node_id ? 'selected' : ''}>${esc(n.name)}</option>`).join('')}</select><p class="form-hint">确认后，会从这里重新安排剩余路线。</p><button class="primary-button full-button" id="confirm-location" type="button">从这里继续逛</button>`);
    $('confirm-location').addEventListener('click', async () => { const node = $('location-input').value; closeModal(); const data = await event('update_location', { current_node_id: node }); if (data && !replyProblem(data)) await event('replan'); });
  });
  $('modal').addEventListener('click', (e) => { if (e.target.matches('[data-close-modal],.modal-backdrop')) closeModal(); });
  document.addEventListener('keydown', (e) => {
    if ($('modal').classList.contains('hidden')) return;
    if (e.key === 'Escape') closeModal();
    if (e.key === 'Tab') { const controls = [...$('modal').querySelectorAll('button,input,textarea,select')].filter((el) => !el.disabled), first = controls[0], last = controls[controls.length - 1]; if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); } else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); } }
  });
  const restart = () => { stopSpeech(); storage.clear(); location.reload(); };
  $('reset-session').addEventListener('click', restart); $('restart-button').addEventListener('click', restart);
  window.addEventListener('pagehide', stopSpeech);
  topics();
  (async function init() { try { state.museum = await request('/api/museum'); $('museum-notice').textContent = state.museum.notice || state.museum.name; $('museum-notice').classList.remove('hidden'); renderRecommendations(); await createSession(); } catch (error) { setStatus(`暂时无法连接导览服务：${error.message}`); } })();
})();
