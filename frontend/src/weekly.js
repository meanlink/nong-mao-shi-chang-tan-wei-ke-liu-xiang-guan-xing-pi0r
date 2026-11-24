// 营业周封账 / 周结 / 补账 / 采样核对面板，挂载在「客流采样」页顶部。
// 规则：按营业周顺序封账；封账时已有效的本周采样计入当周；
// 封账后才有效的迟到记录按原营业周进入下一张未封周结的补账。
const STATUS_TEXT = {
  valid: '可解释', insufficient: '时段不足', conflicting: '标签冲突', excluded: '已排除',
};

const f2 = n => Number(n || 0).toLocaleString('zh-CN');
const f1 = n => Number(n || 0).toFixed(1);
const day = iso => iso ? iso.slice(5, 10) : '—';
const fullDay = iso => iso ? iso.slice(0, 10) : '—';

export async function weeklyPanel(el, { get, post, esc, role, onChanged } = {}) {
  if (!el) return;
  const canClose = role === 'admin' || role === 'analyst';
  let rec = null;
  let error = '';
  let busyWeek = '';

  async function load() {
    try {
      rec = await get('/samples/reconciliation');
      error = '';
    } catch (e) {
      error = e.message || '周结数据读取失败';
    }
  }

  function message(text, kind = 'ok') {
    const box = el.querySelector('[data-weekly-message]');
    if (!box) return;
    box.textContent = text || '';
    box.dataset.kind = kind;
  }

  async function closeWeek(weekStart, btn) {
    busyWeek = weekStart;
    message('');
    el.querySelectorAll('button[data-close-week]').forEach(b => { b.disabled = true; });
    try {
      const r = await post(`/weekly-settlements/${weekStart}/close`, {});
      const repeated = r && r.closed_at && r.status === 'closed';
      message(`营业周 ${r.week_label || weekStart} 已封账：本期 ${r.sample_count} 条 / ${f2(r.total_footfall)} 人次，补账 ${r.supplement_count} 条。重复提交不会重复入账。`);
      await load();
      render();
      if (onChanged) { try { onChanged(); } catch { /* 整页刷新会重建面板 */ } }
    } catch (e) {
      message(e.message || '封账失败', 'err');
      el.querySelectorAll('button[data-close-week]').forEach(b => { b.disabled = false; });
    } finally {
      busyWeek = '';
      if (btn) btn.blur();
    }
  }

  function itemRow(i, mode) {
    const typePill = mode === 'live'
      ? '<span class="pill">本期实时</span>'
      : i.kind === 'supplement'
        ? `<span class="pill amber">补账 · 原营业周 ${esc(i.original_week_label || '')}</span>${i.projected ? '<small class="warn">待封账时落账</small>' : ''}`
        : '<span class="pill good">本期正账</span>';
    return `<tr data-basis-row data-sample-id="${i.sample_id}">
      <td>#${i.sample_id}</td>
      <td>${i.sampled_at ? new Date(i.sampled_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'}</td>
      <td><b>${esc(i.stall_code || '')}</b><small>${esc(i.stall_name || '')}</small></td>
      <td>${esc(i.window_label || '')}</td>
      <td>${f2(i.footfall)}</td>
      <td>${f2(i.transactions)}</td>
      <td>${typePill}</td>
    </tr>`;
  }

  function weekCard(w) {
    const closed = !!w.closed;
    const live = w.live || null;
    const curCount = closed ? w.sample_count : (live ? live.sample_count : w.sample_count);
    const curFoot = closed ? w.total_footfall : (live ? live.footfall : w.total_footfall);
    const supplements = w.supplement_items || [];
    const primary = w.primary_items || [];
    const liveSamples = !closed && live ? (live.samples || []) : [];
    const groups = w.supplement_groups || [];
    const due = !!w.is_next_unclosed;

    const groupHtml = groups.length ? `<div class="week-supp-groups">
      ${groups.map(g => `<div class="supp-group" data-supplement-group="${esc(g.original_week_start || '')}">
        <span class="pill amber">原营业周 ${esc(g.original_week_label || fullDay(g.original_week_start))}</span>
        <b>${g.count} 条</b><strong>${f2(g.footfall)} 人次</strong>
      </div>`).join('')}
    </div>` : '';

    const basisRows = closed
      ? primary.map(i => itemRow(i, 'booked')).join('') + supplements.map(i => itemRow(i, 'booked')).join('')
      : liveSamples.map(i => itemRow(i, 'live')).join('') + supplements.map(i => itemRow(i, 'booked')).join('');

    const closeBtn = canClose && due && !closed
      ? `<button type="button" class="action" data-close-week="${w.week_start}" ${busyWeek === w.week_start ? 'disabled' : ''}>封账</button>`
      : '';
    const retryBtn = canClose && closed
      ? `<button type="button" class="week-retry" data-close-week="${w.week_start}" title="同一营业周重复封账不会重复入账，已封周结保留当时数字与采样依据">重试封账不重复入账</button>`
      : '';

    return `<article class="panel week-card ${closed ? 'is-closed' : 'is-open'} ${due ? 'is-due' : ''}"
        data-week-card="${w.week_start}" data-week-status="${w.status}" data-closable="${due}">
      <div class="week-card-head">
        <div>
          <p class="eyebrow">营业周 ${esc(w.week_key || w.week || '')}</p>
          <h3>${esc(w.week_label || '')}</h3>
          <small class="week-range">${esc(w.week_start)} → ${esc(w.week_end)}</small>
        </div>
        <div class="week-badges">
          ${due ? '<span class="pill amber" data-next-unclosed>下一张未封周结</span>' : ''}
          <span class="pill ${closed ? 'good' : ''}">${closed ? '已封账' : '未封周结'}</span>
        </div>
      </div>

      <div class="week-numbers">
        <div class="week-num primary">
          <small>本期客流</small>
          <b data-metric="current-footfall">${f2(curFoot)}</b>
          <span data-metric="current-count">${f2(curCount)} 条有效采样</span>
          ${closed ? '<em class="freeze">数字已封账冻结</em>' : '<em class="live">封账前实时合计</em>'}
        </div>
        <div class="week-num supplement">
          <small>补账（迟到有效记录）</small>
          <b data-metric="supplement-footfall">${f2(w.supplement_footfall)}</b>
          <span data-metric="supplement-count">${f2(w.supplement_count)} 条 · 按原营业周列示</span>
        </div>
      </div>

      ${groupHtml}

      <div class="week-actions">
        ${closeBtn}
        ${retryBtn}
        ${closed && w.closed_at ? `<small class="closed-meta">封账时间 ${new Date(w.closed_at).toLocaleString('zh-CN')}${w.closed_by_name ? ' · ' + esc(w.closed_by_name) : ''}</small>` : ''}
      </div>

      <details class="basis-details" ${due || w.supplement_count ? 'open' : ''}>
        <summary>采样依据与明细（${closed ? primary.length + supplements.length : liveSamples.length + supplements.length} 条）</summary>
        <div class="table-wrap">
          <table>
            <thead><tr><th>记录</th><th>采样时间</th><th>摊位</th><th>时段</th><th>客流</th><th>成交</th><th>入账类型 / 原营业周</th></tr></thead>
            <tbody>${basisRows || '<tr><td colspan="7" class="empty">本周暂无采样依据</td></tr>'}</tbody>
          </table>
        </div>
      </details>
    </article>`;
  }

  function suppliedSection() {
    const rows = rec.supplied_weeks || [];
    if (!rows.length) {
      return '<p class="empty" data-empty-supplement>封账后产生的迟到有效记录，会按原营业周汇总到下一张未封周结的补账里。</p>';
    }
    return `<div class="table-wrap"><table data-supplied-table>
      <thead><tr><th>补给的周结（接收周）</th><th>记录原营业周</th><th>补账条数</th><th>补账客流</th><th>补账成交</th></tr></thead>
      <tbody>${rows.map(w => (w.groups || []).map(g => `<tr data-supplied-row
          data-target-week="${esc(w.week_start)}" data-original-week="${esc(g.original_week_start || '')}">
        <td><b>${esc(w.week_label)}</b><small>${esc(w.week_start)} · ${w.status === 'closed' ? '已封账' : '未封周结'}</small></td>
        <td>${esc(g.original_week_label || fullDay(g.original_week_start))}<small>${esc(g.original_week_start || '')}</small></td>
        <td>${g.count}</td>
        <td><b>${f2(g.footfall)}</b></td>
        <td>${f2(g.transactions)}</td>
      </tr>`).join('')).join('')}</tbody>
    </table></div>`;
  }

  function ledgerSection() {
    const rows = rec.samples || [];
    return `<div class="table-wrap"><table data-ledger-table>
      <thead><tr><th>采样记录</th><th>采样时间</th><th>营业周（按采样时刻）</th><th>摊位</th><th>客流</th><th>状态</th><th>周结去向（凭记录核对）</th></tr></thead>
      <tbody>${rows.map(s => `<tr data-reconciliation-row data-sample-id="${s.sample_id}" data-disposition="${esc(s.disposition_code)}">
        <td>#${s.sample_id}</td>
        <td>${s.sampled_at ? new Date(s.sampled_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'}</td>
        <td><b>${esc(s.week_label)}</b><small>${esc(s.week_start)} · ${esc(s.week_key || '')}</small></td>
        <td>${esc(s.stall_code)}<small>${esc(s.stall_name)}</small></td>
        <td>${f2(s.footfall)}</td>
        <td><span class="pill ${s.status === 'valid' ? 'good' : 'bad'}">${STATUS_TEXT[s.status] || s.status}</span></td>
        <td data-disposition-text="${esc(s.disposition_code)}">${esc(s.disposition)}${s.settlement_id ? `<small>周结单 #${s.settlement_id}${s.settlement_week_label ? ' · ' + esc(s.settlement_week_label) : ''}</small>` : ''}</td>
      </tr>`).join('')}</tbody>
    </table></div>`;
  }

  function render() {
    if (!rec) {
      el.innerHTML = `<section class="panel weekly-panel"><p class="feedback err">${esc(error || '正在读取营业周结…')}</p></section>`;
      return;
    }
    const t = rec.totals || {};
    const weeks = rec.weeks || [];
    el.innerHTML = `
    <section class="panel weekly-hero" data-weekly-overview>
      <div class="panel-title">
        <div>
          <p class="eyebrow">WEEKLY CLOSE / 营业周核客流</p>
          <h3>营业周封账、周结与补账</h3>
        </div>
        <button type="button" class="week-refresh" data-weekly-refresh>刷新核对</button>
      </div>
      <p class="weekly-rule">市场每周核一次客流：请按营业周顺序封账。封账时已有效的本周采样计入当周；封账后补录、或经归属更正才有效的迟到记录，按其<strong>原营业周</strong>列在<strong>下一张未封周结</strong>的补账里。每份有效客流只进一张周结，重试封账不重复入账，已封周结保留当时的数字与采样依据。</p>
      <div class="weekly-summary">
        <div><small>有效采样</small><b data-summary="valid-count">${f2(t.sample_count)}</b><span>条当前有效客流</span></div>
        <div><small>本期有效客流合计</small><b data-summary="valid-footfall">${f2(t.footfall)}</b><span>人次（全部营业周）</span></div>
        <div><small>已入周结</small><b data-summary="settled">${f2(t.settled_sample_count)}</b><span>条 · 每份只进一张</span></div>
        <div><small>待封账</small><b data-summary="pending">${f2(t.pending_sample_count)}</b><span>条 · 本周封账计入</span></div>
        <div><small>迟到补账</small><b data-summary="supplement">${f2(t.supplement_sample_count)}</b><span>条 · 按原营业周补入</span></div>
      </div>
      <p class="feedback" data-weekly-message role="status"></p>
    </section>

    <div class="week-cards" data-week-cards>
      ${weeks.map(weekCard).join('') || '<section class="panel"><p class="empty">尚无营业周数据，先补录一条采样。</p></section>'}
    </div>

    <section class="panel weekly-supplied">
      <div class="panel-title"><div><p class="eyebrow">SUPPLEMENTS BY WEEK</p><h3>补给各周的客流</h3></div>
        <small>按接收周结与原营业周双向核对</small></div>
      ${suppliedSection()}
    </section>

    <section class="panel weekly-ledger">
      <div class="panel-title"><div><p class="eyebrow">SAMPLE LEDGER</p><h3>凭采样记录核对本期客流与补给去向</h3></div>
        <small>${(rec.samples || []).length} 条当前采样记录</small></div>
      ${ledgerSection()}
    </section>`;

    el.querySelector('[data-weekly-refresh]')?.addEventListener('click', async () => {
      await load(); render();
    });
    el.querySelectorAll('button[data-close-week]').forEach(btn => {
      btn.addEventListener('click', () => closeWeek(btn.dataset.closeWeek, btn));
    });
  }

  el.innerHTML = '<section class="panel weekly-panel"><p class="empty">正在读取营业周结…</p></section>';
  await load();
  render();
}
