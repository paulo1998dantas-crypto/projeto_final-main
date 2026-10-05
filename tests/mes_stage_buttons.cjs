const assert = require('node:assert/strict');
const vm = require('node:vm');

async function verify(testCase) {
  const actions = [...testCase.html.matchAll(/data-stage-action="([A-Z_]+)"/g)].map(match => match[1]);
  assert.deepEqual(actions, testCase.actions);
  assert.ok(!testCase.html.includes('onclick="command('));
  for (const action of actions) {
    const buttons = actions.map(stageAction => ({
      command: '', // HTMLButtonElement.command, introduced in Chrome 135.
      dataset: { stageAction }, disabled: false,
      addEventListener(event, handler) { assert.equal(event, 'click'); this.handler = handler; },
    }));
    const elements = {
      message: { textContent: '', hidden: true }, observacoes: { value: 'Teste de interface' },
      inicio: { value: '2026-10-05T08:00' }, momento: { value: '2026-10-05T09:00' },
    };
    const requests = [];
    let reloads = 0;
    const context = vm.createContext({
      document: {
        querySelectorAll: () => buttons,
        getElementById: id => elements[id],
      },
      crypto: { randomUUID: () => 'test-button-key' },
      confirm: () => true,
      location: { reload() { reloads++; } },
      fetch: async (endpoint, options) => {
        requests.push({ endpoint, options, payload: JSON.parse(options.body) });
        return { ok: !testCase.reject, json: async () => ({ ok: !testCase.reject, error: 'Etapa alterada' }) };
      },
    });
    for (const script of testCase.html.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
      vm.runInContext(script[1], context);
    }
    await buttons.find(button => button.dataset.stageAction === action).handler();
    assert.equal(requests.length, 1, `${testCase.name}: ${action} must reach the API exactly once`);
    assert.equal(requests[0].endpoint, '/api/erp/producao/work/test-work/commands');
    assert.equal(requests[0].options.method, 'POST');
    assert.equal(requests[0].payload.action, action);
    assert.equal(requests[0].payload.stage_code, 'REVEST');
    assert.equal(requests[0].payload.expected_status, testCase.status);
    assert.equal(requests[0].payload.observacoes, 'Teste de interface');
    assert.equal(reloads, testCase.reject ? 0 : 1);
    if (testCase.reject) {
      assert.equal(elements.message.textContent, 'Etapa alterada');
      assert.equal(elements.message.hidden, false);
      assert.ok(buttons.every(button => !button.disabled));
    }
  }
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', async () => {
  try {
    // Reproduce the old inline-handler collision without any browser or network.
    assert.throws(() => new Function('button', 'with(button){command("INICIAR")}')({command: ''}), TypeError);
    const cases = JSON.parse(input);
    for (const testCase of cases) await verify(testCase);
    console.log(`${cases.length} rendered stage-button scenarios passed`);
  } catch (error) { console.error(error); process.exitCode = 1; }
});
