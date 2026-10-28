const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../userscript/mast-token-push.user.js'), 'utf8');
const jwt = 'header.' + Buffer.from(JSON.stringify({exp: Math.floor(Date.now() / 1000) + 172800})).toString('base64url') + '.signature';

function browser(initialUserToken, priorPush) {
  class Storage {
    constructor() { this.values = new Map(); }
    getItem(key) { return this.values.get(key) ?? null; }
    setItem(key, value) { this.values.set(key, value); }
  }
  const storage = new Storage();
  storage.setItem('jwtToken', JSON.stringify(jwt));
  if (initialUserToken) storage.setItem('user', JSON.stringify({token: initialUserToken}));
  const values = new Map(priorPush ? [['lastPushedToken', jwt]] : []);
  const requests = [];
  const timers = new Map();
  let nextId = 0;
  const w = {Storage, localStorage: storage, location: {href: 'https://www.mast.co.il/'}};
  vm.runInNewContext(source, {
    unsafeWindow: w,
    window: w,
    document: {body: null, addEventListener() {}},
    URL, Date, console,
    atob: value => Buffer.from(value, 'base64').toString('binary'),
    setTimeout: callback => { const id = ++nextId; timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id),
    GM_getValue: (key, fallback) => values.get(key) ?? fallback,
    GM_setValue: (key, value) => values.set(key, value),
    GM_xmlhttpRequest: request => requests.push(request),
  });
  function flush() {
    for (let guard = 0; timers.size; guard++) {
      assert.ok(guard < 20, 'No unbounded timer loop');
      const [id, callback] = timers.entries().next().value;
      timers.delete(id);
      callback();
    }
  }
  return {w, requests, flush};
}

test('adds the renewal credential even when this JWT was pushed by the old userscript', () => {
  const b = browser('remembered-credential', true);
  b.flush();
  assert.equal(b.requests.length, 1);
  assert.deepEqual(JSON.parse(b.requests[0].data), {token: jwt, user_token: 'remembered-credential'});
  b.requests[0].onload({status: 200});
  b.flush();
  assert.equal(b.requests.length, 1);
  b.w.localStorage.setItem('user', JSON.stringify({token: 'rotated-credential'}));
  b.flush();
  assert.equal(b.requests.length, 2);
  assert.equal(JSON.parse(b.requests[1].data).user_token, 'rotated-credential');
});

test('captures user storage written after the JWT and supports JWT-only sessions', () => {
  const b = browser(null, false);
  b.flush();
  assert.deepEqual(JSON.parse(b.requests[0].data), {token: jwt});
  b.w.localStorage.setItem('user', JSON.stringify({token: 'remembered-credential'}));
  b.requests[0].onload({status: 200});
  b.flush();
  assert.equal(b.requests.length, 2);
  assert.equal(JSON.parse(b.requests[1].data).user_token, 'remembered-credential');
});
