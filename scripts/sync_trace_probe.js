/* sync-trace-probe — 阅读进度上报链路探针（只在浏览器控制台跑，不改任何逻辑）
 *
 * 用法：在阅读页打开 DevTools → Console → 整段粘贴 → 回车。
 *   滚动几屏，然后：
 *     __syncTrace.dump()     // 最近 12 次 /api/sync 的请求/响应表格
 *     __syncTrace.gates()    // 两个放行窗口的剩余毫秒数 + 当前锚
 *     __syncTrace.clear()    // 清空记录
 *
 * 判读：
 *   · dump() 一行都没有        → 请求根本没发出 = 上报链路被闸门挡死
 *   · record=true 且 hit 正常  → 链路健康
 *   · hit 是 MISS             → 后端指纹桥没匹配上（看 text 列是不是空/串行）
 *   · gates.programmatic 长期 > 0 → 程序窗口没释放（反复被延长）
 */
(function () {
  if (window.__syncTrace) { console.log('[sync-trace] 已在运行'); return; }

  const log = [];
  const origFetch = window.fetch;

  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const p = origFetch.apply(this, arguments);
    if (url.indexOf('/api/sync') < 0) return p;

    const entry = { at: new Date().toLocaleTimeString(), ms: null, req: null, res: null, err: null };
    try { entry.req = JSON.parse((init && init.body) || '{}'); } catch (_) { /* 非 JSON 不看 */ }
    log.push(entry);

    const t0 = performance.now();
    p.then((r) => r.clone().json().then((j) => { entry.res = j; })
      .catch(() => { entry.res = null; }))
      .catch((e) => { entry.err = String((e && e.message) || e); })
      .finally(() => { entry.ms = Math.round(performance.now() - t0); });
    return p;
  };

  window.__syncTrace = {
    log,
    rows(n) {
      return log.slice(-(n || 12)).map((e) => ({
        t: e.at,
        ms: e.ms,
        text: e.req ? String(e.req.pageText || '').slice(0, 26) : '(?)',
        cfi: e.req ? String(e.req.cfi || '').slice(0, 36) : '',
        rec: e.req ? e.req.record : null,
        hint: e.req ? (e.req.hintChapter + ',' + e.req.hintChunk) : '',
        hit: e.res
          ? (e.res.matched
            ? (e.res.position.chapter_index + 'ch/' + e.res.position.chunk_index + 'ck'
               + ' total=' + e.res.position.total_chunks)
            : ('MISS: ' + (e.res.reason || '')))
          : (e.err || 'pending'),
      }));
    },
    // ⚠️ console.table 在"选中→复制→粘贴"时会整块丢失（实测只剩标题行）。
    // 因此 dump() 除表格外**额外打印一行 JSON 字符串**；dumpText() 则只返回
    // 字符串、不碰表格 —— 排障请优先用它。
    dump(n) {
      const rows = this.rows(n);
      console.table(rows);
      console.log('TRACE=' + JSON.stringify(rows));
      return rows;
    },
    /** 纯字符串输出：`console.log('TRACE=' + __syncTrace.dumpText(10))` */
    dumpText(n) { return JSON.stringify(this.rows(n)); },
    gates() {
      const out = {};
      if (window.__reader && window.__reader.probeOnce) {
        const d = window.__reader.probeOnce();
        out.reporter = d.reporter;
        out.gates = d.gates;
        out.scrollerTop = d.scroller && d.scroller.top;
        out.managerScrollTop = d.managerScrollTop;
        out.viewport = d.viewport;
        out.dbPos = d.dbPos;
      } else {
        out.note = 'native 渲染未激活（__reader 不存在）';
      }
      if (window.__reader && window.__reader.diag) out.diag = window.__reader.diag();
      console.log('GATES=' + JSON.stringify(out));
      console.table([out]);
      return out;
    },
    clear() { log.length = 0; return 'cleared'; },
  };

  console.log('%c[sync-trace] 已挂载 → 滚动几屏后执行 __syncTrace.dump()', 'color:#3a9;font-weight:bold');
})();
