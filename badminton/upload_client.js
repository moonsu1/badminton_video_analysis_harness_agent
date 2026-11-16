/* Badminton upload v2: keep Open WebUI's normal attachment contract. */
(() => {
  if (window.__badmintonUploadVersion) return;
  window.__badmintonUploadVersion = 2;
  const originalFetch = window.fetch.bind(window);
  let queue = Promise.resolve();
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const bytes = n => n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(2)}GB` : `${(n / 1024 ** 2).toFixed(1)}MB`;
  function progress(file, offset, suffix = '') {
    let panel = document.getElementById('badminton-upload-progress');
    if (!panel) {
      panel = document.createElement('div'); panel.id = 'badminton-upload-progress';
      panel.setAttribute('role', 'status'); panel.setAttribute('aria-live', 'polite');
      panel.style.cssText = 'position:fixed;bottom:1rem;left:50%;transform:translateX(-50%);z-index:10000;max-width:90vw;width:max-content;padding:12px 16px;border-radius:12px;background:#16352b;color:white;box-shadow:0 4px 24px #0004;font:14px system-ui;white-space:pre-line;pointer-events:none';
      document.body.appendChild(panel);
    }
    panel.textContent = `${file.name}\n영상 업로드 ${Math.floor(offset / file.size * 100)}% · ${bytes(offset)} / ${bytes(file.size)}${suffix ? '\n' + suffix : ''}`;
    return panel;
  }
  async function sha(blob) {
    const data = blob instanceof Blob ? await blob.arrayBuffer() : new TextEncoder().encode(blob);
    return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', data)), b => b.toString(16).padStart(2, '0')).join('');
  }
  async function request(url, options, signal, timeout = 120000) {
    const controller = new AbortController();
    const cancel = () => controller.abort();
    if (signal?.aborted) throw new DOMException('취소됨', 'AbortError');
    signal?.addEventListener('abort', cancel, {once:true});
    const timer = setTimeout(cancel, timeout);
    try {
      const response = await originalFetch(url, {...options, signal:controller.signal});
      let data; try { data = await response.json(); } catch { throw new Error(`서버 응답을 읽지 못했습니다(HTTP ${response.status}).`); }
      if (!response.ok) {
        const detail = data.detail;
        const error = new Error(typeof detail === 'string' ? detail : detail?.message || `전송 오류(HTTP ${response.status})`);
        error.status = response.status; throw error;
      }
      return data;
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', cancel); }
  }
  async function upload(file, options) {
    const headers = new Headers(options.headers || {});
    const base = '/api/v1/badminton/uploads';
    const jsonHeaders = new Headers(headers); jsonHeaders.set('Content-Type', 'application/json');
    let state, fingerprint, storageKey, phase = 'start';
    progress(file, 0, '전송 준비 중');
    try {
      const sample = 1024 ** 2;
      fingerprint = await sha(`${file.name}:${file.size}:${file.lastModified}:${await sha(file.slice(0, sample))}:${await sha(file.slice(Math.max(0, file.size - sample)))}`);
      storageKey = `badminton-upload-v2:${fingerprint}`;
      const meta = options.body.get('metadata');
      let metadata = {}; if (meta) metadata = JSON.parse(meta);
      state = await request(base, {method:'POST',headers:jsonHeaders,body:JSON.stringify({filename:file.name,size:file.size,fingerprint,content_type:file.type || 'video/mp4',metadata})}, options.signal);
      try { localStorage.setItem(storageKey, state.id); } catch {}
      let offset = state.offset;
      const chunkSize = state.chunk_size;
      while (offset < file.size) {
        phase = 'chunk';
        const start = offset, end = Math.min(file.size, start + chunkSize), body = file.slice(start, end);
        const checksum = await sha(body);
        let success = false;
        for (let attempt = 0; attempt < 5; attempt++) {
          try {
            progress(file, offset, attempt ? '연결을 다시 확인하고 이어서 전송 중' : '');
            const chunkHeaders = new Headers(headers);
            chunkHeaders.set('Content-Type', 'application/octet-stream'); chunkHeaders.set('Upload-Offset', String(start)); chunkHeaders.set('X-Chunk-SHA256', checksum);
            state = await request(`${base}/${state.id}`, {method:'PUT',headers:chunkHeaders,body}, options.signal);
            offset = state.offset; success = true; break;
          } catch (error) {
            if (options.signal?.aborted || [400,401,403,404,413,507].includes(error.status)) throw error;
            await wait(Math.min(8000, 1000 * 2 ** attempt));
            try {
              state = await request(`${base}/${state.id}`, {headers}, options.signal);
              if (state.offset >= end) { offset = state.offset; success = true; break; }
              if (state.offset !== start) { offset = state.offset; success = true; break; }
            } catch (statusError) { if ([401,403,404].includes(statusError.status)) throw statusError; }
          }
        }
        if (!success) throw new Error('전송 연결이 끊겼습니다. 같은 파일을 다시 선택하면 저장된 지점부터 이어서 올립니다.');
        progress(file, offset);
      }
      phase = 'complete';
      progress(file, file.size, '영상 저장·등록 확인 중');
      for (let attempt = 0; state.status !== 'completed' && attempt < 5; attempt++) {
        try { state = await request(`${base}/${state.id}/complete`, {method:'POST',headers}, options.signal, 180000); }
        catch (error) {
          if (options.signal?.aborted || [400,401,403,404,413,507].includes(error.status)) throw error;
          await wait(2000);
          state = await request(`${base}/${state.id}`, {headers}, options.signal);
        }
      }
      if (state.status !== 'completed' || !state.file?.id) throw new Error('전송한 영상의 등록을 확인하지 못했습니다. 같은 파일을 다시 선택하면 완료 여부를 확인합니다.');
      const panel = progress(file, file.size, '업로드 완료'); setTimeout(() => panel.remove(), 3500);
      try { localStorage.removeItem(storageKey); } catch {}
      return new Response(JSON.stringify(state.file), {status:200,headers:{'Content-Type':'application/json'}});
    } catch (error) {
      const message = error.status === 401 ? '로그인이 만료됐습니다. 다시 로그인한 뒤 같은 파일을 선택해 주세요.' : error.message;
      progress(file, state?.offset || 0, message);
      originalFetch('/api/v1/badminton/upload-events', {method:'POST',headers:jsonHeaders,body:JSON.stringify({phase,upload_id:state?.id,size:file.size,error:message}),keepalive:true}).catch(() => {});
      throw new Error(message);
    }
  }
  window.fetch = function(input, options = {}) {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    const file = options.body instanceof FormData ? options.body.get('file') : null;
    if (url.origin === location.origin && /^\/api\/v1\/files\/?$/.test(url.pathname) && (options.method || 'GET').toUpperCase() === 'POST' && file instanceof File && file.size >= 32 * 1024 ** 2) {
      const job = queue.then(() => upload(file, options)); queue = job.catch(() => {}); return job;
    }
    return originalFetch(input, options);
  };
})();
