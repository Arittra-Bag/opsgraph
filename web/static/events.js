/* Incremental SSE framing. Transport chunks need not align with UTF-8 or event lines. */
(function (root) {
  'use strict';
  function createSSEParser(onEvent) {
    let buffer = '';
    let data = [];
    let id = '';
    let type = 'message';
    function line(value) {
      if (value === '') {
        if (data.length) onEvent({ id, type, data: data.join('\n') });
        data = []; id = ''; type = 'message';
        return;
      }
      if (value.startsWith(':')) return;
      const colon = value.indexOf(':');
      const field = colon < 0 ? value : value.slice(0, colon);
      let content = colon < 0 ? '' : value.slice(colon + 1);
      if (content.startsWith(' ')) content = content.slice(1);
      if (field === 'data') data.push(content);
      if (field === 'id' && !content.includes('\0')) id = content;
      if (field === 'event') type = content;
    }
    return {
      push(chunk) {
        buffer += chunk;
        let index;
        while ((index = buffer.indexOf('\n')) >= 0) {
          line(buffer.slice(0, index).replace(/\r$/, ''));
          buffer = buffer.slice(index + 1);
        }
      },
      finish() { buffer = ''; data = []; },
    };
  }
  const api = { createSSEParser };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.OpsGraphEvents = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
